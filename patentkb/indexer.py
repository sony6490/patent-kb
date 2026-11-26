"""索引器：mtime+size 增量扫描知识库文件夹，解析并写入 files/chunks/fts_chunks。"""
import datetime as _dt
import json
import threading
from pathlib import Path
from typing import Any

from . import config, db, fts
from .parsers import get_parser, is_indexable, LEGACY

_scan_guard = threading.Lock()  # 防止并发扫描、以及扫描中切换知识库


def is_scanning() -> bool:
    return _scan_guard.locked()


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def scan(kb_path: str, full: bool = False) -> dict[str, Any]:
    """扫描知识库。返回 ScanReport。full=True 时强制重解析所有文件。"""
    if not _scan_guard.acquire(blocking=False):
        return {"error": "已有扫描在进行中，请稍候"}
    try:
        return _scan_locked(kb_path, full)
    finally:
        _scan_guard.release()


def _scan_locked(kb_path: str, full: bool) -> dict[str, Any]:
    root = Path(kb_path)
    if not root.is_dir():
        return {"error": f"路径不存在: {kb_path}"}

    cfg = config.load_config()
    idx_cfg = cfg.get("indexer", {})
    exclude_dirs = set(idx_cfg.get("exclude_dirs", []))
    exclude_exts = [e.lower() for e in idx_cfg.get("exclude_exts", [])]

    report = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0,
              "failed": 0, "skipped": 0, "unsupported": 0, "total": 0,
              "finished_at": _now()}

    # 1. 现有索引快照 rel_path -> row
    existing = {r["rel_path"]: r for r in db.q("SELECT * FROM files")}
    seen: set[str] = set()

    # 2. 遍历文件系统
    for fp in root.rglob("*"):
        if not fp.is_file():
            continue
        if fp.name.startswith("~$"):
            continue  # Office 临时锁文件
        if fp.name.startswith("._"):
            continue  # macOS 资源分支文件（AppleDouble），无实际内容
        rel_parts = fp.relative_to(root).parts
        if any(p in exclude_dirs for p in rel_parts):
            continue
        if "__MACOSX" in rel_parts:
            continue  # macOS 压缩包解压产生的元数据目录，整个跳过
        rel_path = "/".join(rel_parts)  # POSIX 分隔符
        ext = fp.suffix.lower()
        if not is_indexable(fp.name, ext, exclude_exts):
            continue
        seen.add(rel_path)
        report["total"] += 1
        try:
            st = fp.stat()
        except OSError:
            continue

        old = existing.get(rel_path)
        stable = ("ok", "truncated", "skipped_no_text", "unsupported")
        if old and not full and old["mtime"] == st.st_mtime and old["size"] == st.st_size \
                and old["status"] in stable:
            report["unchanged"] += 1
            continue

        _index_one(root, rel_path, fp, ext, st, old, report)

    # 3. 清理已删除文件（单事务批删）
    removed_ids = [row["id"] for rel_path, row in existing.items() if rel_path not in seen]
    if removed_ids:
        with db.transaction() as conn:
            for rid in removed_ids:
                # file_id 在 fts5 表里是 UNINDEXED，按它删会全表扫（实测单文件 102s）；
                # rowid 与 chunks.id 一一对应，走 rowid 删除是索引操作（实测 0ms）。
                conn.execute("DELETE FROM fts_chunks WHERE rowid IN (SELECT id FROM chunks WHERE file_id=?)", (rid,))
                conn.execute("DELETE FROM chunks WHERE file_id=?", (rid,))
                conn.execute("DELETE FROM files WHERE id=?", (rid,))
            report["removed"] += len(removed_ids)

    return report


def _index_one(root: Path, rel_path: str, fp: Path, ext: str,
               st: Any, old: dict | None, report: dict) -> None:
    """解析单文件并写库。"""
    status = "pending"
    error_msg = None
    meta: dict = {}
    chunks: list[dict] = []

    if ext in LEGACY:
        status = "unsupported"
        error_msg = "老格式，建议另存为 docx/xlsx/pptx 后重新扫描"
        report["unsupported"] += 1
    else:
        parser = get_parser(ext)
        if parser is None:
            report["unsupported"] += 1
            return  # 未知扩展名本不应进入（is_indexable 已过滤），防御
        result = parser(fp)
        status = result["status"]
        error_msg = result["error"]
        meta = result.get("meta", {})
        chunks = result["chunks"]
        if status == "failed":
            report["failed"] += 1
        elif status == "skipped_no_text":
            report["skipped"] += 1
            # 扫描件/空文件：伪 chunk 只含文件名，保证可检索
            chunks = [{"content": f"[无文字层文件] {fp.name}（路径: {rel_path}）",
                       "anchor": {"type": "file", "filename_only": True}}]
        else:
            pass  # ok / truncated 计数并入 added/updated

    with db.transaction() as conn:
        if old:
            conn.execute("DELETE FROM fts_chunks WHERE rowid IN (SELECT id FROM chunks WHERE file_id=?)", (old["id"],))
            conn.execute("DELETE FROM chunks WHERE file_id=?", (old["id"],))
            conn.execute("DELETE FROM files WHERE id=?", (old["id"],))
            report["updated"] += 1
        else:
            report["added"] += 1
        cur = conn.execute(
            "INSERT INTO files(rel_path, filename, ext, mtime, size, status, error_msg, parsed_at, meta_json) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (rel_path, fp.name, ext, st.st_mtime, st.st_size,
             status, error_msg, _now(), json.dumps(meta, ensure_ascii=False)),
        )
        file_id = cur.lastrowid
        _write_chunks(conn, file_id, chunks)


def _write_chunks(conn, file_id: int, chunks: list[dict]) -> None:
    """写 chunks + fts_chunks（调用方持有事务）。OCR 与主索引共用，防止 SQL 漂移。"""
    for i, ch in enumerate(chunks):
        ccur = conn.execute(
            "INSERT INTO chunks(file_id, chunk_index, content, anchor_json, char_count) VALUES(?,?,?,?,?)",
            (file_id, i, ch["content"],
             json.dumps(ch["anchor"], ensure_ascii=False), len(ch["content"])),
        )
        conn.execute(
            "INSERT INTO fts_chunks(tokens, chunk_id, file_id) VALUES(?,?,?)",
            (fts.tokenize(ch["content"]), ccur.lastrowid, file_id),
        )


def index_status() -> dict[str, Any]:
    rows = db.q("SELECT status, COUNT(*) AS n FROM files GROUP BY status")
    by = {r["status"]: r["n"] for r in rows}
    last = db.q("SELECT MAX(parsed_at) AS t FROM files")[0]["t"]
    return {
        "total": sum(by.values()),
        "ok": by.get("ok", 0) + by.get("truncated", 0),
        "failed": by.get("failed", 0),
        "skipped": by.get("skipped_no_text", 0),
        "unsupported": by.get("unsupported", 0),
        "last_scan_at": last,
    }


def preview_files(kb_path: str) -> dict[str, Any]:
    """向导第 1 步：预览可索引文件统计。"""
    root = Path(kb_path)
    cfg = config.load_config()
    exclude_dirs = set(cfg.get("indexer", {}).get("exclude_dirs", []))
    exts: dict[str, int] = {}
    total = 0
    for fp in root.rglob("*"):
        if not fp.is_file():
            continue
        if fp.name.startswith("._"):
            continue  # macOS 资源分支文件
        rel_parts = fp.relative_to(root).parts
        if any(p in exclude_dirs for p in rel_parts):
            continue
        if "__MACOSX" in rel_parts:
            continue  # macOS 元数据目录
        ext = fp.suffix.lower()
        if not is_indexable(fp.name, ext, []):
            continue
        exts[ext] = exts.get(ext, 0) + 1
        total += 1
    return {"total": total, "by_ext": sorted(exts.items(), key=lambda x: -x[1])}
