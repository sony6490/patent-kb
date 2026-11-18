"""LLM 客户端：OpenAI 兼容接口、hash 缓存、usage_log、每日限额、token 估算。"""
import datetime as _dt
import hashlib
import json
from typing import Any

from . import config, db


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


def _cache_key(request_type: str, norm_text: str, chunk_ids: list[int],
                model: str, prompt_version: str) -> str:
    raw = "|".join([request_type, norm_text, ",".join(map(str, chunk_ids)),
                    model, prompt_version])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_text(text: str) -> str:
    return "".join(text.split()).lower()


def call_cached(model: str, messages: list[dict], request_type: str,
                norm_text: str, chunk_ids: list[int],
                prompt_version: str) -> dict[str, Any]:
    """带缓存的 LLM 调用。返回 {content, usage:{...}, cache_hit}。

    - 缓存命中：免费、不计限额
    - 超每日限额：抛 DailyLimitError
    - 失败：记 usage_log 后抛 LLMError
    """
    cfg = config.load_config()
    llm_cfg = cfg.get("llm", {})
    key = _cache_key(request_type, norm_text, chunk_ids, model, prompt_version)

    row = db.q("SELECT response_json FROM llm_cache WHERE cache_key=?", (key,))
    if row:
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
    _log(request_type, model, pt, ct, cache_hit=0, error=None)

    with db.write_lock():
        db.x(
            "INSERT OR REPLACE INTO llm_cache(cache_key, request_type, response_json, model, created_at) VALUES(?,?,?,?,?)",
            (key, request_type, content, model, _dt.datetime.now().isoformat(timespec="seconds")),
        )
    return {"content": content, "usage": {"prompt_tokens": pt, "completion_tokens": ct},
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
         cache_hit: int, error: str | None) -> None:
    with db.write_lock():
        db.x(
            "INSERT INTO usage_log(ts, request_type, model, prompt_tokens, completion_tokens, cache_hit, error) VALUES(?,?,?,?,?,?,?)",
            (_dt.datetime.now().isoformat(timespec="seconds"), request_type, model,
             pt, ct, cache_hit, error),
        )


def usage_summary(days: int = 7) -> list[dict[str, Any]]:
    since = (_dt.date.today() - _dt.timedelta(days=days - 1)).isoformat()
    rows = db.q(
        "SELECT substr(ts,1,10) AS date, COUNT(*) AS calls, "
        "SUM(CASE WHEN cache_hit=0 THEN 1 ELSE 0 END) AS billed, "
        "SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, "
        "SUM(cache_hit) AS cache_hits "
        "FROM usage_log WHERE substr(ts,1,10)>=? GROUP BY date ORDER BY date",
        (since,),
    )
    return rows
