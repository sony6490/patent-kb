"""FTS5 检索：jieba 预分词、查询构建（AND→OR 降级）、snippet、引用校验归一化。"""
import re
from pathlib import Path
from typing import Any

import jieba

from . import config

_user_dict_loaded = False
_syn_cache: dict[str, tuple[float, dict]] = {}  # 同义词表模块级缓存：path -> (mtime, data)

# 内置种子同义词表（专利/电学场景常见术语）；用户可在 .kbindex/synonyms.json 覆盖
_SEED_SYNONYMS: dict[str, list[str]] = {
    "无线": ["无线充电", "无线供电", "感应式"],
    "充电": ["供电", "充能"],
    "线圈": ["绕组", "感应线圈"],
    "定位": ["对位", "对准", "校位"],
    "耦合": ["藕合", "磁耦合"],
    "补偿": ["校准", "校正"],
    "异物": ["FOD", "异物检测"],
    "热成像": ["红外成像", "热像"],
    "瞄准镜": ["瞄准具", "瞄具"],
    "光轴": ["光学轴", "视轴"],
}


def synonyms() -> dict[str, list[str]]:
    """读取同义词表：配置 fts.synonyms_file 优先，否则 <kb_path>/.kbindex/synonyms.json。
    表结构 {"词": ["同义1", "同义2"]}。按 mtime 变化重载，无需重建索引。"""
    import json
    cfg = config.load_config().get("fts") or {}
    if not cfg.get("synonyms_enabled", True):
        return {}
    custom = cfg.get("synonyms_file", "")
    if custom:
        p = Path(custom)
    else:
        kb = config.load_config().get("kb_path", "")
        p = Path(kb) / ".kbindex" / "synonyms.json" if kb else Path("")
    if not p or not p.is_file():
        return dict(_SEED_SYNONYMS)  # 无自定义表时用内置种子，零配置生效
    try:
        mt = p.stat().st_mtime
        cached = _syn_cache.get(str(p))
        if cached and cached[0] == mt:
            return cached[1]
        data = json.loads(p.read_text(encoding="utf-8"))
        out = {str(k): [str(x) for x in v][:5] for k, v in data.items() if isinstance(v, list)}
        _syn_cache[str(p)] = (mt, out)
        return out
    except Exception:
        return {}


def expand_groups(terms: list[str]) -> list[list[str]]:
    """每个查询词扩展为 [原词, 同义...] 组；同义词个数受 max_synonyms_per_term 限制。
    支持双向映射：查询用同义词（如"对位"）也能召回原文词（如"定位"）。"""
    cfg = config.load_config().get("fts") or {}
    max_syn = int(cfg.get("max_synonyms_per_term", 3))
    syn = synonyms()
    reverse: dict[str, list[str]] = {}
    for word, alts in syn.items():
        for alt in alts:
            reverse.setdefault(alt, []).append(word)
            for other in alts:
                if other != alt:
                    reverse.setdefault(alt, []).append(other)
    groups = []
    for t in terms:
        extra = []
        for s in list(syn.get(t, [])) + list(reverse.get(t, [])):
            if s != t and s not in extra:
                extra.append(s)
        groups.append([t] + extra[:max_syn])
    return groups


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


def verify_score(word: str, texts: Any) -> dict[str, Any]:
    """校验 word 是否在 texts 中有原文依据，返回多维度指标（纯本地、无语义模型）。

    verified 判定：整体归一化包含，或分词覆盖率≥0.8，或归一化 bigram 重叠≥0.6。
    """
    if isinstance(texts, str):
        texts = [texts]
    nw = _norm_word(word)
    if not nw:
        return {"verified": False, "hit_chunks": 0, "token_coverage": 0.0, "bigram_overlap": 0.0}
    norm_texts = [_norm_word(t) for t in texts if t]
    hits = sum(1 for nt in norm_texts if nw in nt)
    toks = [t for t in tokenize(word).split() if len(_norm_word(t)) >= 2]
    cov = (sum(1 for t in toks if any(_norm_word(t) in nt for nt in norm_texts)) / len(toks)) if toks else 0.0
    grams = {nw[i:i+2] for i in range(len(nw) - 1)}
    ov = (sum(1 for g in grams if any(g in nt for nt in norm_texts)) / len(grams)) if grams else 0.0
    verified = hits > 0 or cov >= 0.8 or ov >= 0.6
    return {"verified": bool(verified), "hit_chunks": hits,
            "token_coverage": round(cov, 2), "bigram_overlap": round(ov, 2)}


def verify_in_text(word: str, text: str) -> bool:
    """向后兼容的布尔包装。"""
    return verify_score(word, text)["verified"]


def query_field_terms(query: str) -> list[str]:
    """从智慧芽检索式中提取用于溯源校验的词与分类号（剥离字段/逻辑符/括号/引号/通配符）。"""
    s = re.sub(r"\b(?:TI|ABST|CLMS|TAC|PN|PA|IN|IPC|CPC|FAM_COUNTRY)\s*:", " ", query, flags=re.I)
    s = re.sub(r"\b(?:AND|OR|NOT)\b", " ", s, flags=re.I)
    s = s.replace("(", " ").replace(")", " ").replace('"', " ").replace("“", " ").replace("”", " ")
    s = re.sub(r"[?*]", " ", s)
    codes = re.findall(r"[A-H]\d{2}[A-Z]?\d*/\d+", s, flags=re.I)  # IPC/CPC 分类号
    for c in codes:  # 从文本移除分类号，避免 jieba 再拆成 H02J50 / 80 噪音词
        s = s.replace(c, " ")
    terms = query_terms(s)
    seen: set[str] = set()
    out = []
    for t in codes + terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:12]


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


def _fts_expr_groups(groups: list[list[str]], op: str) -> str:
    """每组内 OR（原词+同义），组间按 op（AND/OR）连接。"""
    parts = []
    for g in groups:
        inner = " OR ".join(f'"{t}"' for t in g)
        parts.append(f"({inner})" if len(g) > 1 else inner)
    return f" {op} ".join(parts)


def search(q: str, limit: int = 20, force_or: bool = False,
           scope: str = "") -> list[dict[str, Any]]:
    """FTS 检索：多词 AND 优先，无结果降级 OR（force_or 直接用 OR）；返回带 snippet 的命中。

    scope 为相对目录（如 "项目A/2025"），非空时只在该目录及其子目录内召回。
    过滤放在 SQL 层而非结果层，避免"先取 Top-N 再筛"造成的漏召回。
    """
    from . import db

    terms = query_terms(q)
    if not terms:
        return []
    snip = (config.load_config().get("fts") or {}).get("snippet_max_chars", 200)

    groups = expand_groups(terms)  # 同义词查询时扩展，无需重建索引
    ops = ("OR",) if force_or else ("AND", "OR")

    scope = (scope or "").strip().strip("/")
    if scope:
        # LIKE 通配符转义，防止目录名里的 _ 或 % 被当成模式
        scope_like = (scope.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                      + "/%")
        scope_sql = " AND f.file_id IN (SELECT id FROM files WHERE rel_path LIKE ? ESCAPE '\\')"
    else:
        scope_like, scope_sql = None, ""

    for op in ops:
        expr = _fts_expr_groups(groups, op)
        sql = (
            "SELECT f.chunk_id, f.file_id, "
            "snippet(fts_chunks, 0, '»', '«', '…', 12) AS snip, rank "
            "FROM fts_chunks f WHERE fts_chunks MATCH ?" + scope_sql +
            " ORDER BY rank LIMIT ?"
        )
        params = (expr, scope_like, limit) if scope_like else (expr, limit)
        try:
            rows = db.q(sql, params)
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


# ---------- 检索结果来源展示：项目名 + 完整绝对路径 ----------

def project_name() -> str:
    """项目名 = 当前知识库根目录(kb_path)的目录名；随切库自动变化。"""
    return Path(config.load_config()["kb_path"]).resolve().name


def safe_abs_path(rel_path: str) -> Path | None:
    """把 rel_path 安全还原为绝对路径；越界/异常返回 None（不抛异常）。

    与 main._abs_path_safe 同源校验，但展示层不允许单条脏数据中断整页。
    """
    try:
        root = Path(config.load_config()["kb_path"]).resolve()
        fp = (root / rel_path).resolve()
    except (OSError, ValueError, TypeError):
        return None
    return fp if fp.is_relative_to(root) else None


def decorate_hits(hits: list[dict]) -> list[dict]:
    """为命中项附加 project 与 abs_path 字段（就地修改并返回）。"""
    proj = project_name()
    for h in hits:
        fp = safe_abs_path(h.get("rel_path", ""))
        h["project"] = proj
        h["abs_path"] = str(fp) if fp is not None else ""
    return hits
