"""LLM 客户端：OpenAI 兼容接口、hash 缓存、usage_log、每日限额、token 估算。"""
import datetime as _dt
import hashlib
import json
from collections.abc import Callable
from decimal import Decimal, ROUND_CEILING
from typing import Any

from . import config, db

# DeepSeek 官方定价（2026-08-17 起），单位：元 / 百万 token。固定写入，不提供 UI 编辑。
# 三档全量记录以备追溯；本地已有 hash 缓存，输入 token 一律按「未命中价」计（保守高估）。
PRICING: dict[str, dict[str, dict[str, float]]] = {
    "deepseek-flash": {
        "cache_hit": {"off": 0.02, "peak": 0.04},
        "miss": {"off": 1.00, "peak": 2.00},
        "output": {"off": 4.00, "peak": 8.00},
    },
    "deepseek-v4-pro": {
        "cache_hit": {"off": 0.15, "peak": 0.30},
        "miss": {"off": 4.50, "peak": 9.00},
        "output": {"off": 13.50, "peak": 27.00},
    },
}

# 内置模型候选：/api/config/list-models 会与供应商返回合并去重。
BUILTIN_MODELS: tuple[str, ...] = tuple(PRICING.keys())


class LLMError(Exception):
    pass


class DailyLimitError(Exception):
    pass


def _today() -> str:
    return _dt.date.today().isoformat()


def daily_used() -> tuple[int, int]:
    """(今日已调用次数, 限额)。缓存命中不计。"""
    cfg = config.load_config()
    limit = (cfg.get("cost") or {}).get("daily_llm_call_limit", 50)
    rows = db.q(
        "SELECT COUNT(*) AS n FROM usage_log WHERE ts LIKE ? AND cache_hit=0 AND error IS NULL",
        (f"{_today()}%",),
    )
    return rows[0]["n"], limit


def estimate_tokens(text: str) -> int:
    """粗估 token：中文≈1 token/字，英文≈0.25 token/字符。"""
    cn = sum(1 for c in text if ord(c) > 127)
    other = len(text) - cn
    return int(cn + other / 4)


def _is_peak(ts: str) -> bool:
    """北京时间高峰判断：周一至周五 9:00-12:00、14:00-18:00 为高峰，其余闲时。

    ts 为 usage_log 中的本地朴素时间字符串（按本机时区=北京时间处理）。
    法定节假日无法在本地判定，按工作日处理（会在工作时段高估）。
    边界：9:00 峰、12:00 闲、14:00 峰、18:00 闲。
    """
    try:
        dt = _dt.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    if dt.weekday() >= 5:  # 5=周六, 6=周日
        return False
    t = dt.time()
    return (_dt.time(9, 0) <= t < _dt.time(12, 0)) or (
        _dt.time(14, 0) <= t < _dt.time(18, 0)
    )


def calc_cost(model: str, ts: str, pt: int, ct: int) -> float:
    """单次调用费用（元）。输入按未命中价、输出按输出价；未知模型/0 token 计 0。

    返回 6 位小数精确值；向上取整到分只在展示/汇总层做（见 ceil_cent）。
    """
    if not pt and not ct:
        return 0.0
    tier = PRICING.get(model)
    if tier is None:
        return 0.0
    band = "peak" if _is_peak(ts) else "off"
    raw = (int(pt or 0) * tier["miss"][band] + int(ct or 0) * tier["output"][band]) / 1_000_000
    return round(raw, 6)


def ceil_cent(x: float) -> float:
    """向上取整到分（Decimal 规避浮点误差），用于展示与汇总。"""
    return float(Decimal(str(x or 0)).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def _cache_key(request_type: str, norm_text: str, chunk_ids: list[int],
                model: str, prompt_version: str) -> str:
    raw = "|".join([request_type, norm_text, ",".join(map(str, chunk_ids)),
                    model, prompt_version])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_text(text: str) -> str:
    return "".join(text.split()).lower()


def call_cached(model: str, messages: list[dict], request_type: str,
                norm_text: str, chunk_ids: list[int],
                prompt_version: str,
                validate: Callable[[str], bool] | None = None) -> dict[str, Any]:
    """带缓存的 LLM 调用。返回 {content, usage:{...}, cache_hit}。

    - 缓存命中：免费、不计限额
    - validate：内容校验器（如 JSON 合法性）。命中/写入前均校验，脏响应不入缓存、
      历史脏缓存自动失效 → 杜绝非法输出被永久固化（修复重试污染缓存问题）
    - 超每日限额：抛 DailyLimitError
    - 失败：记 usage_log 后抛 LLMError
    """
    cfg = config.load_config()
    llm_cfg = cfg.get("llm", {})
    key = _cache_key(request_type, norm_text, chunk_ids, model, prompt_version)

    row = db.q("SELECT response_json FROM llm_cache WHERE cache_key=?", (key,))
    if row and (validate is None or validate(row[0]["response_json"])):
        _log(request_type, model, 0, 0, cache_hit=1, error=None)
        return {"content": row[0]["response_json"], "usage": None, "cache_hit": True}

    used, limit = daily_used()
    if used >= limit:
        raise DailyLimitError(f"已达每日 LLM 上限 {limit} 次（今日已用 {used}）")

    try:
        from openai import OpenAI
        client = OpenAI(
            base_url=llm_cfg.get("base_url", ""),
            api_key=llm_cfg.get("api_key", ""),
            timeout=llm_cfg.get("timeout_sec", 60),
        )
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=llm_cfg.get("max_output_tokens", 2000),
            temperature=0.2,
        )
    except Exception as e:
        _log(request_type, model, 0, 0, cache_hit=0, error=str(e))
        raise LLMError(f"LLM 调用失败: {e}")

    content = resp.choices[0].message.content or ""
    pt = getattr(resp.usage, "prompt_tokens", 0) or 0
    ct = getattr(resp.usage, "completion_tokens", 0) or 0
    cost = _log(request_type, model, pt, ct, cache_hit=0, error=None)

    # 仅缓存校验通过的响应；非法输出（如坏 JSON）不落盘，避免污染后续请求
    if validate is None or validate(content):
        with db.write_lock():
            db.x(
                "INSERT OR REPLACE INTO llm_cache(cache_key, request_type, response_json, model, created_at) VALUES(?,?,?,?,?)",
                (key, request_type, content, model, _dt.datetime.now().isoformat(timespec="seconds")),
            )
    return {"content": content,
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "cost": cost},
            "cache_hit": False}


def test_connection(base_url: str, api_key: str, model: str) -> dict[str, Any]:
    """向导 [测试连接]：最小请求。"""
    import time
    from openai import OpenAI
    t0 = time.time()
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=30)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "请只回复两个字母：OK"}],
        max_tokens=8,
    )
    return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
            "model": resp.model or model,
            "reply": (resp.choices[0].message.content or "").strip()[:20]}


def _log(request_type: str, model: str, pt: int, ct: int,
         cache_hit: int, error: str | None, ts: str | None = None) -> float:
    """写 usage_log 并返回本次费用（元）。缓存命中/失败记 0。"""
    ts = ts or _dt.datetime.now().isoformat(timespec="seconds")
    cost = 0.0 if (cache_hit or error) else calc_cost(model, ts, pt, ct)
    with db.write_lock():
        db.x(
            "INSERT INTO usage_log(ts, request_type, model, prompt_tokens, completion_tokens, cache_hit, error, cost) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (ts, request_type, model, pt, ct, cache_hit, error, cost),
        )
    return cost


def usage_summary(days: int = 7) -> list[dict[str, Any]]:
    since = (_dt.date.today() - _dt.timedelta(days=days - 1)).isoformat()
    rows = db.q(
        "SELECT substr(ts,1,10) AS date, COUNT(*) AS calls, "
        "SUM(CASE WHEN cache_hit=0 THEN 1 ELSE 0 END) AS billed, "
        "SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, "
        "SUM(cache_hit) AS cache_hits, SUM(cost) AS cost "
        "FROM usage_log WHERE substr(ts,1,10)>=? GROUP BY date ORDER BY date",
        (since,),
    )
    return rows


def cost_summary() -> dict[str, Any]:
    """今日累计费用、最近一次调用费用与币种。费用为精确值，展示层负责向上取整到分。"""
    row = db.q(
        "SELECT COALESCE(SUM(cost),0) AS total, COUNT(*) AS calls "
        "FROM usage_log WHERE substr(ts,1,10)=?", (_today(),),
    )[0]
    last = db.q("SELECT cost, model FROM usage_log ORDER BY id DESC LIMIT 1")
    return {
        "today": round(row["total"], 6),
        "today_calls": row["calls"],
        "last_call": round(last[0]["cost"], 6) if last else 0.0,
        "last_call_model": last[0]["model"] if last else "",
        "currency": "CNY",
    }
