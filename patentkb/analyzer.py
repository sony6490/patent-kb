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


def _reuse_chunks(chunk_ids: list[int]) -> list[dict[str, Any]]:
    """追问时按 id 取回初次检索的同一批 chunk，保持 C 编号稳定；不重跑 FTS。"""
    cfg = config.load_config()
    max_chars = (cfg.get("analyzer") or {}).get("max_chunk_chars", 4000)
    out = []
    for i, cid in enumerate(chunk_ids[:15], 1):
        rows = db.q(
            "SELECT c.content, c.anchor_json, f.rel_path FROM chunks c "
            "JOIN files f ON f.id=c.file_id WHERE c.id=?", (cid,))
        if not rows:
            continue
        out.append({
            "ref": f"C{i}",
            "chunk_id": cid,
            "rel_path": rows[0]["rel_path"],
            "content": rows[0]["content"][:max_chars],
            "anchor": fts._parse_json(rows[0]["anchor_json"]),
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


def _repair_truncated_json(s: str) -> dict[str, Any] | None:
    """尝试修复因 max_tokens 截断的 JSON 尾部。"""
    s = s.strip()
    if not s.startswith("{"):
        return None
    # 逐层闭合未完成的引号、方括号与花括号
    # 先剥离尾部悬空的未闭合键值对片段
    candidate = s
    for _ in range(50):
        # 尝试闭合当前层级
        open_braces = candidate.count("{") - candidate.count("}")
        open_brackets = candidate.count("[") - candidate.count("]")
        quote_unclosed = candidate.count('"') % 2 == 1
        tail = ""
        if quote_unclosed:
            tail += '"'
        if open_brackets > 0:
            tail += "]" * open_brackets
        if open_braces > 0:
            tail += "}" * open_braces
        try:
            d = json.loads(candidate + tail)
            if isinstance(d, dict) and ("keywords" in d or "classifications" in d or "patent_query" in d):
                return d
        except json.JSONDecodeError:
            pass
        # 逐步丢掉末尾的一个字符/逗号再试
        last_comma = candidate.rfind(",")
        if last_comma <= 0:
            break
        candidate = candidate[:last_comma].rstrip()
    return None


def _extract_json(text: str) -> dict[str, Any] | None:
    """从 LLM 输出中提取 JSON 对象（容忍 markdown 代码块、前后废话及轻度截断）。"""
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
            pass
    # 尝试从首个 { 开始修复可能被 token 上限截断的尾部
    start = text.find("{")
    if start >= 0:
        repaired = _repair_truncated_json(text[start:])
        if repaired is not None:
            return repaired
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


def _valid_refs(refs: Any, valid: set[str]) -> tuple[list[str], list[str]]:
    """返回 (合法编号, 非法编号)；非法编号由调用方显式告警，不再静默丢弃。"""
    if not isinstance(refs, list):
        return [], []
    good = [r for r in refs if isinstance(r, str) and r in valid]
    bad = [str(r) for r in refs if not (isinstance(r, str) and r in valid)]
    return good, bad


def analyze(request_text: str, use_lite: bool = False,
            session: dict | None = None) -> dict[str, Any]:
    """主分析流程。session 非空时为追问：复用同一批 chunk、带历史，不重跑 FTS。"""
    cfg = config.load_config()
    llm_cfg = cfg.get("llm", {})
    if not llm_cfg.get("base_url") or not llm_cfg.get("api_key") or not llm_cfg.get("model"):
        raise llm.LLMError("未配置 LLM API，请先在设置页配置 base_url / api_key / model")

    is_followup = bool(session and session.get("chunk_ids"))
    model = _pick_model(cfg, use_lite)
    if is_followup:
        chunks = _reuse_chunks(session["chunk_ids"])
        history = (session.get("history") or [])[-(int((cfg.get("analyzer") or {}).get("followup_max_history", 3))):]
    else:
        chunks = _retrieve_chunks(request_text)
        history = []
    if not chunks:
        raise llm.LLMError("知识库中未检索到相关内容，无法基于资料分析。请先索引知识库或调整需求描述")

    valid_refs = {ch["ref"] for ch in chunks}
    chunk_by_ref = {ch["ref"]: ch for ch in chunks}

    if is_followup:
        user_prompt = prompts.build_followup_prompt(request_text, chunks, history)
    else:
        user_prompt = prompts.build_user_prompt(request_text, chunks)
    messages = [
        {"role": "system", "content": prompts.SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    norm = llm.normalize_text(request_text)
    chunk_ids = [ch["chunk_id"] for ch in chunks]
    req_type = "analyze_followup" if is_followup else "analyze"
    if is_followup:
        # 历史摘要进缓存键，防止不同会话上下文的同问题互相命中
        hist_sig = "|".join(f"{h.get('q','')}->{h.get('a','')[:40]}" for h in history)
        norm = llm.normalize_text(norm + "||" + hist_sig)

    # 校验器：坏 JSON 既不入缓存，也能让历史脏缓存自动失效（不再靠改 chunk_ids 绕键）
    def _is_json(s: str) -> bool:
        return _extract_json(s) is not None

    result = None
    for attempt in range(2):
        resp = llm.call_cached(
            model, messages, req_type, norm, chunk_ids, prompts.PROMPT_VERSION,
            validate=_is_json,
        )
        result = _extract_json(resp["content"])
        if result is not None:
            break
        messages.extend([
            {"role": "assistant", "content": resp["content"]},
            *prompts.RETRY_PROMPT("非合法 JSON"),
        ])

    if result is None:
        raise llm.LLMError("LLM 两次输出均无法解析为 JSON")

    return _assemble(result, valid_refs, chunk_by_ref, model, resp)


def _assemble(result: dict, valid_refs: set, chunk_by_ref: dict,
              model: str, resp: dict) -> dict[str, Any]:
    """引用校验与结果组装。verified=词/码在所指 chunk 原文有依据；非法编号显式告警。"""
    warnings: list[dict] = []

    def sources_for(refs: list[str], text: str) -> tuple[list[dict], dict]:
        srcs = []
        texts = []
        for r in refs:
            ch = chunk_by_ref.get(r)
            if not ch:
                continue
            texts.append(ch["content"])
            srcs.append({
                "ref": r,
                "chunk_id": ch["chunk_id"],
                "rel_path": ch["rel_path"],
                "anchor": ch["anchor"],
                "snippet": ch["content"][:200],
            })
        score = fts.verify_score(text, texts) if (srcs and text) else {
            "verified": False, "hit_chunks": 0, "token_coverage": 0.0, "bigram_overlap": 0.0}
        for s in srcs:
            s["coverage"] = score["token_coverage"]
        return srcs, score

    def note_invalid(bad: list[str], where: str) -> None:
        if bad:
            warnings.append({
                "code": "INVALID_REF",
                "message": f"{where} 引用了不存在的编号 {', '.join(bad)}，已忽略且该项标为未验证",
            })

    keywords = []
    for kw in result.get("keywords", []):
        text = str(kw.get("text", "")).strip()
        if not text:
            continue
        refs, bad = _valid_refs(kw.get("source_refs"), valid_refs)
        note_invalid(bad, f"关键词「{text}」")
        srcs, score = sources_for(refs, text)
        keywords.append({
            "text": text,
            "lang": kw.get("lang", "zh"),
            "verified": bool(score["verified"] and srcs),
            "coverage": score["token_coverage"],
            "sources": srcs,
        })

    classifications = []
    for c in result.get("classifications", []):
        code = str(c.get("code", "")).strip()
        if not code:
            continue
        refs, bad = _valid_refs(c.get("source_refs"), valid_refs)
        note_invalid(bad, f"分类号「{code}」")
        srcs, score = sources_for(refs, code)
        classifications.append({
            "code": code,
            "explanation": str(c.get("explanation", "")).strip(),
            "verified": bool(score["verified"] and srcs),
            "coverage": score["token_coverage"],
            "sources": srcs,
        })

    pq = result.get("patent_query", {}) or {}
    query = str(pq.get("query", "")).strip()
    refs, bad = _valid_refs(pq.get("source_refs"), valid_refs)
    note_invalid(bad, "检索式")
    srcs, _ = sources_for(refs, query)
    qterms = fts.query_field_terms(query)
    if qterms and srcs:
        src_texts = [chunk_by_ref[r]["content"] for r in refs if r in chunk_by_ref]
        per_term = [fts.verify_score(term, src_texts) for term in qterms]
        verified_terms = sum(1 for s in per_term if s["verified"])
        qscore = {
            "verified": verified_terms / len(per_term) >= 0.6,
            "token_coverage": round(sum(s["token_coverage"] for s in per_term) / len(per_term), 2),
            "hit_chunks": max((s["hit_chunks"] for s in per_term), default=0),
        }
    else:
        qscore = {"verified": False, "token_coverage": 0.0, "hit_chunks": 0}
    patent_query = {
        "query": query,
        "syntax_ok": _check_query_syntax(query) if query else False,
        "verified": bool(qscore["verified"] and srcs),
        "coverage": qscore["token_coverage"],
        "hit_chunks": qscore["hit_chunks"],
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
    # 本次费用（元）：缓存命中时 usage 为 None，取 0
    usage["cost"] = round((resp.get("usage") or {}).get("cost", 0.0), 6)

    return {
        "keywords": keywords,
        "classifications": classifications,
        "patent_query": patent_query,
        "warnings": warnings,
        "chunk_ids": [ch["chunk_id"] for ch in chunk_by_ref.values()],
        "usage": usage,
    }
