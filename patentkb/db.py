"""SQLite 连接与建表。索引库位于 <kb_path>/.kbindex/index.db。"""
import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import config

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()
_kb_path: str = ""

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  rel_path TEXT UNIQUE NOT NULL,
  filename TEXT NOT NULL,
  ext TEXT NOT NULL,
  mtime REAL NOT NULL,
  size INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  error_msg TEXT,
  parsed_at TEXT,
  meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  content TEXT NOT NULL,
  anchor_json TEXT NOT NULL,
  char_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
  tokens, chunk_id UNINDEXED, file_id UNINDEXED
);

CREATE TABLE IF NOT EXISTS llm_cache (
  cache_key TEXT PRIMARY KEY,
  request_type TEXT NOT NULL,
  response_json TEXT NOT NULL,
  model TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  request_type TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  cache_hit INTEGER DEFAULT 0,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts);
"""


def init(kb_path: str) -> sqlite3.Connection:
    """初始化（或复用）数据库连接。kb_path 为知识库绝对路径。"""
    global _conn, _kb_path
    with _lock:
        if _conn is not None and _kb_path == kb_path:
            return _conn
        idx_dir = Path(kb_path) / ".kbindex"
        idx_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(idx_dir / "index.db"), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        _conn = conn
        _kb_path = kb_path
        return conn


def get() -> sqlite3.Connection:
    """获取当前连接；未初始化时按 config 中的 kb_path 初始化。"""
    global _conn
    if _conn is None:
        cfg = config.load_config()
        if not cfg.get("kb_path"):
            raise RuntimeError("知识库未初始化，请先完成设置向导")
        return init(cfg["kb_path"])
    return _conn


def write_lock() -> threading.Lock:
    return _lock


def q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """只读查询，返回 dict 列表。"""
    cur = get().execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def x(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """写操作（调用方持有 write_lock）。"""
    conn = get()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur


def transaction():
    """事务上下文：整批写完一次性 commit（索引批量写入的关键性能路径）。

    用法：with db.transaction() as conn: conn.execute(...)
    异常自动 rollback 并抛出；期间持有写锁。
    """
    import contextlib

    @contextlib.contextmanager
    def _tx():
        with _lock:
            conn = get()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    return _tx()


def reset() -> None:
    """切换知识库时关闭旧连接。"""
    global _conn, _kb_path
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
            _kb_path = ""
