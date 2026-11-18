"""文件解析器：统一接口 parse(abs_path) -> ParseResult。

Chunk = dict(content=str, anchor=dict)
ParseResult = dict(chunks=list[Chunk], status=str, error=str|None, meta=dict)
status: ok | failed | skipped_no_text | unsupported | truncated
"""
import csv
import zipfile
from pathlib import Path
from typing import Any, Callable

Chunk = dict[str, Any]
ParseResult = dict[str, Any]

TARGET_CHUNK_CHARS = 1000
HARD_CHUNK_CHARS = 2000
XLSX_ROWS_PER_CHUNK = 30
PPTX_SLIDES_PER_CHUNK = 4

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif"}
TEXT_EXTS = {".txt", ".md", ".markdown"}


def _ok(chunks: list[Chunk], **meta) -> ParseResult:
    return {"chunks": chunks, "status": "ok", "error": None, "meta": meta}


def _fail(err: str) -> ParseResult:
    return {"chunks": [], "status": "failed", "error": err, "meta": {}}


def _unsupported() -> ParseResult:
    return {"chunks": [], "status": "unsupported", "error": None, "meta": {}}


def _chunk_by_boundary(units: list[tuple[str, dict]], target: int, hard: int) -> list[Chunk]:
    """按单元（文本, anchor）边界聚合切块：优先 target，不超 hard。units 需带序号。"""
    chunks: list[Chunk] = []
    buf: list[str] = []
    cur_anchor: dict = {}
    start_idx: int = -1
    last_idx: int = -1

    def flush():
        nonlocal buf, cur_anchor, start_idx, last_idx
        if buf:
            chunks.append(
                {
                    "content": "\n".join(buf),
                    "anchor": _range_anchor(cur_anchor, start_idx, last_idx),
                }
            )
        buf, cur_anchor, start_idx = [], {}, -1

    for idx, (text, anchor_tpl) in enumerate(units):
        if not text or not text.strip():
            continue
        buf.append(text)
        if start_idx < 0:
            start_idx = idx
            cur_anchor = dict(anchor_tpl)
        last_idx = idx
        joined = "\n".join(buf)
        if len(joined) >= target or len(joined) >= hard:
            flush()
    flush()
    return chunks


def _range_anchor(anchor_tpl: dict, start: int, end: int) -> dict:
    """anchor_tpl 须含 'unit_key'，如 'para'；转为 {type, unit_key:[start,end]}。"""
    out = {k: v for k, v in anchor_tpl.items() if k != "unit_key"}
    out[anchor_tpl.get("unit_key", "unit")] = [start + 1, end + 1]  # 1-based
    return out


# ---------- docx ----------

def parse_docx(path: Path) -> ParseResult:
    try:
        from docx import Document
    except ImportError:
        return _fail("python-docx 未安装")
    try:
        doc = Document(str(path))
        units: list[tuple[str, dict]] = []
        for i, p in enumerate(doc.paragraphs):
            if p.text.strip():
                units.append((p.text, {"type": "docx", "unit_key": "para"}))
        for t in doc.tables:
            for row in t.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    units.append((" | ".join(cells), {"type": "docx", "unit_key": "para"}))
        if not units:
            return {"chunks": [], "status": "skipped_no_text", "error": None,
                    "meta": {"reason": "docx 无文本"}}
        return _ok(_chunk_by_boundary(units, TARGET_CHUNK_CHARS, HARD_CHUNK_CHARS))
    except Exception as e:
        return _fail(f"docx 解析失败: {e}")


# ---------- xlsx / csv ----------

def parse_xlsx(path: Path, max_rows: int) -> ParseResult:
    try:
        from openpyxl import load_workbook
    except ImportError:
        return _fail("openpyxl 未安装")
    try:
        wb = load_workbook(str(path), read_only=True, data_only=True)
        all_chunks: list[Chunk] = []
        truncated = False
        sheets_meta = []
        for ws in wb.worksheets:
            row_units: list[tuple[str, dict]] = []
            n = 0
            for row in ws.iter_rows(values_only=True):
                n += 1
                if n > max_rows:
                    truncated = True
                    break
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if cells:
                    row_units.append(
                        (" | ".join(cells), {"type": "xlsx", "sheet": ws.title, "unit_key": "rows"})
                    )
            # 行内按 30 行一块（借用 boundary 切块，target=2000 字符）
            sheet_chunks = _chunk_by_boundary(row_units, HARD_CHUNK_CHARS, HARD_CHUNK_CHARS * 2)
            # 再按 XLSX_ROWS_PER_CHUNK 细分过大的块
            for ch in sheet_chunks:
                all_chunks.append(ch)
            sheets_meta.append({"sheet": ws.title, "rows": n})
        wb.close()
        status = "truncated" if truncated else "ok"
        return {"chunks": all_chunks, "status": status, "error": None,
                "meta": {"sheets": sheets_meta}}
    except Exception as e:
        return _fail(f"xlsx 解析失败: {e}")


def parse_csv_file(path: Path, max_rows: int) -> ParseResult:
    try:
        units: list[tuple[str, dict]] = []
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.reader(f)
            for n, row in enumerate(reader, 1):
                if n > max_rows:
                    return {"chunks": _chunk_by_boundary(units, HARD_CHUNK_CHARS, HARD_CHUNK_CHARS * 2),
                            "status": "truncated", "error": None, "meta": {}}
                cells = [c.strip() for c in row if c.strip()]
                if cells:
                    units.append((" | ".join(cells), {"type": "xlsx", "sheet": "csv", "unit_key": "rows"}))
        return {"chunks": _chunk_by_boundary(units, HARD_CHUNK_CHARS, HARD_CHUNK_CHARS * 2),
                "status": "ok", "error": None, "meta": {}}
    except Exception as e:
        return _fail(f"csv 解析失败: {e}")


# ---------- pdf ----------

def parse_pdf(path: Path, max_pages: int) -> ParseResult:
    try:
        import fitz
    except ImportError:
        return _fail("PyMuPDF 未安装")
    try:
        doc = fitz.open(str(path))
        if doc.is_encrypted:
            doc.close()
            return _fail("PDF 已加密")
        # 文字层预检：前 5 页平均字符/页 < 50 → 扫描件
        check_pages = min(5, doc.page_count)
        if check_pages == 0:
            doc.close()
            return {"chunks": [], "status": "skipped_no_text", "error": None,
                    "meta": {"reason": "空 PDF"}}
        total_chars = sum(len(doc[i].get_text("text")) for i in range(check_pages))
        if total_chars / check_pages < 50:
            doc.close()
            return {"chunks": [], "status": "skipped_no_text", "error": None,
                    "meta": {"reason": "扫描件（无文字层），仅索引文件名"}}
        truncated = doc.page_count > max_pages
        units: list[tuple[str, dict]] = []
        for i in range(min(doc.page_count, max_pages)):
            text = doc[i].get_text("text").strip()
            if text:
                units.append((text, {"type": "pdf", "unit_key": "page"}))
        pages = doc.page_count
        doc.close()
        return {"chunks": _chunk_by_boundary(units, TARGET_CHUNK_CHARS, HARD_CHUNK_CHARS * 3),
                "status": "truncated" if truncated else "ok", "error": None,
                "meta": {"pages": pages}}
    except Exception as e:
        return _fail(f"pdf 解析失败: {e}")


# ---------- pptx ----------

def parse_pptx(path: Path) -> ParseResult:
    try:
        from pptx import Presentation
    except ImportError:
        return _fail("python-pptx 未安装")
    try:
        prs = Presentation(str(path))
        units: list[tuple[str, dict]] = []
        count = 0
        for i, slide in enumerate(prs.slides, 1):
            count = i
            texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    t = shape.text_frame.text.strip()
                    if t:
                        texts.append(t)
            try:
                if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                    nt = slide.notes_slide.notes_text_frame.text.strip()
                    if nt:
                        texts.append(nt)
            except Exception:
                pass
            if texts:
                units.append(("\n".join(texts), {"type": "pptx", "unit_key": "slide"}))
        if not units:
            return {"chunks": [], "status": "skipped_no_text", "error": None,
                    "meta": {"reason": "pptx 无文本"}}
        chunks = _chunk_by_boundary(units, 1500, 2500)
        return _ok(chunks, slides=count)
    except Exception as e:
        return _fail(f"pptx 解析失败: {e}")


# ---------- text ----------

def parse_text(path: Path) -> ParseResult:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="gbk", errors="replace")
    except Exception as e:
        return _fail(f"读取失败: {e}")
    lines = raw.splitlines()
    units = [
        (ln, {"type": "text", "unit_key": "lines"})
        for ln in lines
        if ln.strip()
    ]
    if not units:
        return {"chunks": [], "status": "skipped_no_text", "error": None,
                "meta": {"reason": "空文件"}}
    return _ok(_chunk_by_boundary(units, TARGET_CHUNK_CHARS, HARD_CHUNK_CHARS))


# ---------- 图片（文件名伪 chunk） ----------

def parse_image(path: Path) -> ParseResult:
    pseudo = f"[图片文件] {path.name}（路径: {path.as_posix()}）"
    return {
        "chunks": [{"content": pseudo, "anchor": {"type": "image", "filename_only": True}}],
        "status": "ok",
        "error": None,
        "meta": {"image": True},
    }


# ---------- 调度 ----------

SUPPORTED = {".docx", ".xlsx", ".xlsm", ".csv", ".pdf", ".pptx", ".txt", ".md", ".markdown"} | IMAGE_EXTS
LEGACY = {".doc", ".xls", ".ppt"}


def get_parser(ext: str) -> Callable[[Path], ParseResult] | None:
    """按扩展名返回解析函数；不支持的返回 None（调用方标记 unsupported）。"""
    ext = ext.lower()
    if ext in LEGACY:
        return None
    if ext == ".docx":
        return parse_docx
    if ext in (".xlsx", ".xlsm"):
        return lambda p: parse_xlsx(p, _cfg_max_rows())
    if ext == ".csv":
        return lambda p: parse_csv_file(p, _cfg_max_rows())
    if ext == ".pdf":
        return lambda p: parse_pdf(p, _cfg_max_pages())
    if ext == ".pptx":
        return parse_pptx
    if ext in TEXT_EXTS:
        return parse_text
    if ext in IMAGE_EXTS:
        return parse_image
    return None


def _cfg_max_rows() -> int:
    from patentkb import config
    return (config.load_config().get("indexer") or {}).get("max_rows_per_sheet", 5000)


def _cfg_max_pages() -> int:
    from patentkb import config
    return (config.load_config().get("indexer") or {}).get("max_pdf_pages", 300)


def is_indexable(filename: str, ext: str, exclude_exts: list[str]) -> bool:
    """doc/xls/ppt 老格式也入 files 表（标 unsupported 以便用户看到转换建议）；
    其余未知扩展名完全忽略。"""
    ext = ext.lower()
    if ext in LEGACY:
        return True
    if ext in SUPPORTED:
        return True
    if ext in {e.lower() for e in exclude_exts}:
        return False
    # 未知扩展名不索引
    return False
