"""分析编排：检索 top-N chunk → 编号喂数 → LLM → JSON 解析 → 引用校验 → 组装。"""
import json
import re
from typing import Any

from . import config, db, fts, llm, prompts


def _pick_model(cfg: dict, use_lite: bool) -> str:
    llm_cfg = cfg.get("llm", {})
    model = llm_cfg.get("model", "")
    if use_lite:
        model = llm_cfg.get("lite_model") or model
    return model


def _retrieve_chunks(request_text: str) -> list[dict[str, Any]]:
    """检索 top-N 相关 chunk，附加 rel_path 与引用编号。"""
    cfg = config.load_config()
    top_n = (cfg.get("analyzer") or {}).get("top_n_chunks", 15)
    max_chars = (cfg.get("analyzer") or {}).get("max_chunk_chars", 4000)

    # 检索词 = 需求文本本身（jieba 分词后在 fts.search 内处理）
    hits = fts.search(request_text, limit=top_n)
    if len(hits) < 3:
        # AND 命中过少时降级 OR，保证 LLM 有足够资料依据
        hits = fts.search(request_text, limit=top_n, force_or=True)
    # 去重：同一文件最多取 3 个 chunk，保证覆盖面
    per_file: dict[int, int] = {}
    picked: list[dict] = []
    for h in hits:
        fid = h["file_id"]
        if per_file.get(fid, 0) >= 3:
            continue
        per_file[fid] = per_file.get(fid, 0) + 1
        picked.append(h)
        if len(picked) >= top_n:
            break

    out = []
    for i, h in enumerate(picked, 1):
        rows = db.q("SELECT content, anchor_json FROM chunks WHERE id=?", (h["chunk_id"],))
        if not rows:
            continue
        content = rows[0]["content"][:max_chars]
        out.append({
            "ref": f"C{i}",
            "chunk_id": h["chunk_id"],
            "rel_path": h["rel_path"],
            "content": content,
            "anchor": h.get("anchor", {}),
        })
    return out


def estimate(request_text: str, use_lite: bool = False) -> dict[str, Any]:
    """免费预估：检索 + token 估算，不调 LLM。"""
    cfg = config.load_config()
    chunks = _retrieve_chunks(request_text)
    prompt_chars = len(prompts.SYSTEM_PROMPT) + len(request_text)
    for ch in chunks:
        prompt_chars += len(ch["content"]) + len(ch["rel_path"]) + 10
    est = llm.estimate_tokens("x" * prompt_chars) + 800  # 预留输出
    used, limit = llm.daily_used()
    warn = (cfg.get("cost") or {}).get("warn_tokens_per_call", 8000)
    return {
        "est_tokens": est,
        "top_chunk_count": len(chunks),
        "daily_used": f"{used}/{limit}",
        "need_confirm": est > warn,
        "has_llm": bool((cfg.get("llm") or {}).get("base_url") and (cfg.get("llm") or {}).get("api_key")),
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    """从 LLM 输出中提取 JSON 对象（容忍 markdown 代码块与前后废话）。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _check_query_syntax(q: str) -> bool:
    """括号/引号配对自检。"""
    stack = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for ch in q:
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack[-1] != pairs[ch]:
                return False
            stack.pop()
        elif ch in "\"'“”":
            pass  # 引号不做严格配对
    return not stack


def _valid_refs(refs: Any, valid: set[str]) -> list[str]:
    if not isinstance(refs, list):
        return []
    return [r for r in refs if isinstance(r, str) and r in valid]


def analyze(request_text: str, use_lite: bool = False) -> dict[str, Any]:
    """主分析流程。"""
    cfg = config.load_config()
    llm_cfg = cfg.get("llm", {})
    if not llm_cfg.get("base_url") or not llm_cfg.get("api_key") or not llm_cfg.get("model"):
        raise llm.LLMError("未配置 LLM API，请先在设置页配置 base_url / api_key / model")

    model = _pick_model(cfg, use_lite)
    chunks = _retrieve_chunks(request_text)
    if not chunks:
        raise llm.LLMError("知识库中未检索到相关内容，无法基于资料分析。请先索引知识库或调整需求描述")

    valid_refs = {ch["ref"] for ch in chunks}
    chunk_by_ref = {ch["ref"]: ch for ch in chunks}

    messages = [
        {"role": "system", "content": prompts.SYSTEM_PROMPT},
        {"role": "user", "content": prompts.build_user_prompt(request_text, chunks)},
    ]
    norm = llm.normalize_text(request_text)
    chunk_ids = [ch["chunk_id"] for ch in chunks]

    result = None
    for attempt in range(2):
        resp = llm.call_cached(
            model, messages, "analyze", norm, chunk_ids, prompts.PROMPT_VERSION
        )
        result = _extract_json(resp["content"])
        if result is not None:
            break
        messages.extend([
            {"role": "assistant", "content": resp["content"]},
            *prompts.RETRY_PROMPT("非合法 JSON"),
        ])
        # 重试不重复计缓存（call_cached 同 key 会命中缓存），改为直接调用一次未缓存路径：
        # 简化处理：重试通过临时改变 chunk_ids 顺序绕开缓存键
        chunk_ids = chunk_ids + [0] if attempt == 0 else chunk_ids

    if result is None:
        raise llm.LLMError("LLM 两次输出均无法解析为 JSON")

    return _assemble(result, valid_refs, chunk_by_ref, model, resp)


def _assemble(result: dict, valid_refs: set, chunk_by_ref: dict,
              model: str, resp: dict) -> dict[str, Any]:
    """引用校验与结果组装。verified=词/码出现在所指 chunk 原文。"""

    def sources_for(refs: list[str], text: str) -> list[dict]:
        srcs, verified_any = [], False
        for r in refs:
            ch = chunk_by_ref.get(r)
            if not ch:
                continue
            content = ch["content"]
            srcs.append({
                "ref": r,
                "chunk_id": ch["chunk_id"],
                "rel_path": ch["rel_path"],
                "anchor": ch["anchor"],
                "snippet": content[:200],
            })
            if fts.verify_in_text(text, content):
                verified_any = True
        return srcs, verified_any

    keywords = []
    for kw in result.get("keywords", []):
        text = str(kw.get("text", "")).strip()
        if not text:
            continue
        refs = _valid_refs(kw.get("source_refs"), valid_refs)
        srcs, verified = sources_for(refs, text)
        keywords.append({
            "text": text,
            "lang": kw.get("lang", "zh"),
            "verified": verified and bool(srcs),
            "sources": srcs,
        })

    classifications = []
    for c in result.get("classifications", []):
        code = str(c.get("code", "")).strip()
        if not code:
            continue
        refs = _valid_refs(c.get("source_refs"), valid_refs)
        srcs, verified = sources_for(refs, code)
        classifications.append({
            "code": code,
            "explanation": str(c.get("explanation", "")).strip(),
            "verified": verified and bool(srcs),
            "sources": srcs,
        })

    pq = result.get("patent_query", {}) or {}
    query = str(pq.get("query", "")).strip()
    refs = _valid_refs(pq.get("source_refs"), valid_refs)
    srcs, _ = sources_for(refs, query)
    patent_query = {
        "query": query,
        "syntax_ok": _check_query_syntax(query) if query else False,
        "explanation": str(pq.get("explanation", "")).strip(),
        "sources": srcs,
    }

    used, limit = llm.daily_used()
    usage = {"model": model, "cache_hit": resp["cache_hit"], "daily_used": f"{used}/{limit}"}
    if resp.get("usage"):
        usage["prompt_tokens"] = resp["usage"]["prompt_tokens"]
        usage["completion_tokens"] = resp["usage"]["completion_tokens"]
    else:
        usage["prompt_tokens"] = 0
        usage["completion_tokens"] = 0

    return {
        "keywords": keywords,
        "classifications": classifications,
        "patent_query": patent_query,
        "usage": usage,
    }
