"""按需 OCR：对扫描 PDF / 图片提取文字，写入现有 chunks/fts_chunks。
默认关闭（features.ocr），仅由用户显式指定 file_ids 触发，绝不批跑全库。
"""
from pathlib import Path
from typing import Any

from . import config, db, fts, indexer

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def _paths() -> tuple[Path, Path]:
    cfg = config.load_config()
    kb = Path(cfg["kb_path"]).resolve()
    return cfg, kb


def _safe_abs(rel_path: str) -> Path:
    _, kb = _paths()
    fp = (kb / rel_path).resolve()
    # 用路径段比较防前缀绕过（D:/data_evil 不再能通过 D:/data 的 startswith 检查）
    if not fp.is_relative_to(kb):
        raise ValueError("非法路径（越界）")
    return fp


def ocr_file(file_id: int) -> dict[str, Any]:
    """对一个文件做 OCR 并写库。保持 files.status 不变，只在 meta_json 记录 OCR 信息，
    使 mtime+size 增量扫描视其为 stable，不会覆盖 OCR chunk。"""
    cfg, _ = _paths()
    if not (cfg.get("features") or {}).get("ocr"):
        raise PermissionError("OCR 未启用（features.ocr=false）")
    ocr_cfg = cfg.get("ocr") or {}
    max_pages = int(ocr_cfg.get("max_pages_per_file", 30))
    min_conf = float(ocr_cfg.get("min_confidence", 0.5))
    dpi = int(ocr_cfg.get("dpi", 200))

    rows = db.q("SELECT id, rel_path, ext, status, meta_json FROM files WHERE id=?", (file_id,))
    if not rows:
        raise ValueError("文件不存在")
    row = rows[0]
    fp = _safe_abs(row["rel_path"])
    if not fp.is_file():
        raise ValueError("文件已不存在")

    import json
    import numpy as np
    import fitz
    from PIL import Image

    engine = _get_engine()
    chunks: list[dict] = []
    if row["ext"] in (".pdf",):
        doc = fitz.open(str(fp))
        try:
            for pno in range(min(len(doc), max_pages)):
                pix = doc[pno].get_pixmap(dpi=dpi)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                if pix.n == 4:
                    img = img[:, :, :3]
                result, _ = engine(img)
                text = _join(result, min_conf)
                if text:
                    chunks.append({"content": text, "anchor": {"type": "ocr", "page": pno + 1, "engine": "rapidocr"}})
        finally:
            doc.close()
    else:  # 图片
        img = np.array(Image.open(fp).convert("RGB"))
        result, _ = engine(img)
        text = _join(result, min_conf)
        if text:
            chunks.append({"content": text, "anchor": {"type": "ocr", "page": 1, "engine": "rapidocr"}})

    if not chunks:
        raise ValueError("OCR 未识别出文字（可能为非文字图像或清晰度不足）")

    meta = json.loads(row["meta_json"] or "{}")
    meta["ocr"] = {"engine": "rapidocr", "chunks": len(chunks)}
    with db.transaction() as conn:
        conn.execute("DELETE FROM fts_chunks WHERE file_id=?", (file_id,))
        conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        indexer._write_chunks(conn, file_id, chunks)
        conn.execute("UPDATE files SET meta_json=? WHERE id=?",
                     (json.dumps(meta, ensure_ascii=False), file_id))
    return {"ok": True, "file_id": file_id, "chunks": len(chunks)}


def _join(result, min_conf: float) -> str:
    if not result:
        return ""
    lines = [str(item[1]).strip() for item in result
             if len(item) >= 3 and float(item[2]) >= min_conf and str(item[1]).strip()]
    return "\n".join(lines)
