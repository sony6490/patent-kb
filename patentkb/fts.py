"""FTS5 检索：jieba 预分词、查询构建（AND→OR 降级）、snippet、引用校验归一化。"""
import re
from typing import Any

import jieba

from . import config

_user_dict_loaded = False


def _ensure_dict() -> None:
    global _user_dict_loaded
    if _user_dict_loaded:
        return
    cfg = config.load_config()
    ud = (cfg.get("fts") or {}).get("user_dict", "")
    if ud:
        try:
            jieba.load_userdict(ud)
        except Exception:
            pass
    jieba.initialize()
    _user_dict_loaded = True


def tokenize(text: str) -> str:
    """jieba 分词，空格连接（FTS5 unicode61 按空格切分）。"""
    _ensure_dict()
    toks = [t.strip() for t in jieba.cut(text) if t.strip()]
    return " ".join(toks)


def _norm_word(w: str) -> str:
    """引用校验用归一化：去空格与全角符号、小写。"""
    return re.sub(r"[\s\u3000·、，。；：''\"\"()（）\[\]【】/|]+", "", w).lower()


def verify_in_text(word: str, text: str) -> bool:
    """校验 word 是否出现在 text 中：先整体归一化包含，再做去空格匹配（防中文分词空格干扰）。"""
    if not word or not text:
        return False
    nw, nt = _norm_word(word), _norm_word(text)
    if not nw:
        return False
    if nw in nt:
        return True
    # 二级降级：word 分词后逐 token 均出现（处理插入标点的情况）
    toks = [t for t in tokenize(word).split() if len(_norm_word(t)) >= 2]
    if toks and all(_norm_word(t) in nt for t in toks):
        return True
    return False


def query_terms(q: str) -> list[str]:
    """查询串 → 有效检索词列表（长度≥2 或非 ASCII 单字）。"""
    toks = tokenize(q).split()
    terms = []
    for t in toks:
        if len(t) >= 2 or any(ord(c) > 127 for c in t):
            terms.append(t)
    # 去重保序
    seen = set()
    out = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:8]  # 最多 8 词防查询爆炸


def _fts_expr(terms: list[str], op: str) -> str:
    parts = [f'"{t}"' for t in terms]
    return f" {op} ".join(parts)


def search(q: str, limit: int = 20, force_or: bool = False) -> list[dict[str, Any]]:
    """FTS 检索：多词 AND 优先，无结果降级 OR（force_or 直接用 OR）；返回带 snippet 的命中。"""
    from . import db

    terms = query_terms(q)
    if not terms:
        return []
    snip = (config.load_config().get("fts") or {}).get("snippet_max_chars", 200)

    ops = ("OR",) if force_or else ("AND", "OR")
    for op in ops:
        expr = _fts_expr(terms, op)
        sql = (
            "SELECT f.chunk_id, f.file_id, "
            "snippet(fts_chunks, 0, '»', '«', '…', 12) AS snip, rank "
            "FROM fts_chunks f WHERE fts_chunks MATCH ? ORDER BY rank LIMIT ?"
        )
        try:
            rows = db.q(sql, (expr, limit))
        except Exception:
            continue
        if rows:
            out = []
            file_ids = {r["file_id"] for r in rows}
            files = {
                f["id"]: f
                for f in db.q(
                    f"SELECT id, rel_path, filename, ext FROM files WHERE id IN ({','.join('?' * len(file_ids))})",
                    tuple(file_ids),
                )
            } if file_ids else {}
            chunks = {
                c["id"]: c
                for c in db.q(
                    f"SELECT id, anchor_json, content FROM chunks WHERE id IN ({','.join('?' * len(rows))})",
                    tuple(r["chunk_id"] for r in rows),
                )
            }
            for r in rows:
                c = chunks.get(r["chunk_id"], {})
                f = files.get(r["file_id"], {})
                content = c.get("content", "")
                out.append(
                    {
                        "chunk_id": r["chunk_id"],
                        "file_id": r["file_id"],
                        "rel_path": f.get("rel_path", ""),
                        "filename": f.get("filename", ""),
                        "ext": f.get("ext", ""),
                        "anchor": _parse_json(c.get("anchor_json", "{}")),
                        "snippet": (r["snip"] or content[:snip]),
                        "score": -r["rank"],
                        "match_op": op,
                    }
                )
            return out
    return []


def _parse_json(s: str) -> dict:
    import json

    try:
        return json.loads(s)
    except Exception:
        return {}
