"""SQLite 连接与建表。索引库位于 <kb_path>/.kbindex/index.db。"""
import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import config

_conn: sqlite3.Connection | None = None       # 写连接：仅由 transaction()/x() 在 _lock 内使用
_read_conn: sqlite3.Connection | None = None  # 读连接：独立于写连接，WAL 下读已提交快照，不被长事务扫描阻塞
_lock = threading.RLock()
_kb_path: str = ""

SCHEMA_VERSION = 2

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
  error TEXT,
  cost REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts);

CREATE TABLE IF NOT EXISTS auth_devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_name TEXT NOT NULL,
  token TEXT UNIQUE NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_active_at TEXT,
  last_ip TEXT,
  remark TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_devices_token ON auth_devices(token);
"""


def init(kb_path: str) -> sqlite3.Connection:
    """初始化（或复用）数据库连接。kb_path 为知识库绝对路径。

    建立两条独立连接：写连接供索引/缓存写入，读连接供 HTTP 读接口。
    WAL 模式下二者可并发：扫描长事务占用写连接时，读接口仍能查到已提交数据。
    """
    global _conn, _read_conn, _kb_path
    with _lock:
        if _conn is not None and _kb_path == kb_path:
            return _conn
        idx_dir = Path(kb_path) / ".kbindex"
        idx_dir.mkdir(parents=True, exist_ok=True)
        db_file = str(idx_dir / "index.db")

        # 先关掉旧连接（切库场景），避免泄漏与文件句柄占用
        if _conn is not None:
            _conn.close()
        if _read_conn is not None:
            _read_conn.close()

        conn = sqlite3.connect(db_file, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        # 幂等补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列。
        # 老库升级时补齐 cost 列，历史行默认 0，不追溯补算。
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(usage_log)")}
        if "cost" not in cols:
            conn.execute("ALTER TABLE usage_log ADD COLUMN cost REAL NOT NULL DEFAULT 0")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()

        # 独立读连接：只设 read 相关 PRAGMA，不参与任何写事务。
        # busy_timeout 保证建库瞬间与写连接初始化不冲突。
        rconn = sqlite3.connect(db_file, check_same_thread=False, timeout=30)
        rconn.row_factory = sqlite3.Row
        rconn.execute("PRAGMA busy_timeout=30000")

        _conn = conn
        _read_conn = rconn
        _kb_path = kb_path
        return conn


def get() -> sqlite3.Connection:
    """获取写连接；未初始化时按 config 中的 kb_path 初始化。"""
    global _conn
    if _conn is None:
        cfg = config.load_config()
        if not cfg.get("kb_path"):
            raise RuntimeError("知识库未初始化，请先完成设置向导")
        init(cfg["kb_path"])
    return _conn


def _get_read() -> sqlite3.Connection:
    """获取读连接；未初始化时按 config 中的 kb_path 初始化。"""
    global _read_conn
    if _read_conn is None:
        cfg = config.load_config()
        if not cfg.get("kb_path"):
            raise RuntimeError("知识库未初始化，请先完成设置向导")
        init(cfg["kb_path"])
    return _read_conn


def write_lock() -> threading.Lock:
    return _lock


def q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """只读查询，走独立读连接，返回 dict 列表。

    关键：绝不使用写连接——扫描线程会长时间占用写连接做批量事务，
    若读接口共用它会导致 browse/status 等全部挂死（本次修复的根因）。
    """
    cur = _get_read().execute(sql, params)
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
    """切换知识库时关闭旧连接（写连接与读连接都要关）。"""
    global _conn, _read_conn, _kb_path
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
        if _read_conn is not None:
            _read_conn.close()
            _read_conn = None
        _kb_path = ""


# ---------- 远程设备与员工权限管控 ----------

def list_devices() -> list[dict[str, Any]]:
    """获取所有已登记的授权设备列表（按添加倒序）。"""
    try:
        return q(
            "SELECT id, device_name, token, is_active, created_at, last_active_at, last_ip, remark "
            "FROM auth_devices ORDER BY id DESC"
        )
    except Exception:
        return []


def get_device_by_token(token: str) -> dict[str, Any] | None:
    """根据口令获取设备信息；库未就绪或未找到时返回 None。"""
    if not token:
        return None
    try:
        rows = q(
            "SELECT id, device_name, token, is_active, created_at, last_active_at, last_ip, remark "
            "FROM auth_devices WHERE token = ?",
            (token.strip(),)
        )
        return rows[0] if rows else None
    except Exception:
        return None


def add_device(device_name: str, token: str, remark: str = "") -> dict[str, Any]:
    """新增授权设备/员工记录。"""
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with write_lock():
        cur = x(
            "INSERT INTO auth_devices (device_name, token, is_active, created_at, remark) "
            "VALUES (?, ?, 1, ?, ?)",
            (device_name.strip(), token.strip(), now, (remark or "").strip())
        )
        dev_id = cur.lastrowid
    rows = q(
        "SELECT id, device_name, token, is_active, created_at, last_active_at, last_ip, remark "
        "FROM auth_devices WHERE id = ?",
        (dev_id,)
    )
    return rows[0]


def set_device_active(device_id: int, is_active: int) -> bool:
    """启用或禁用（离职吊销）指定设备。"""
    with write_lock():
        x("UPDATE auth_devices SET is_active = ? WHERE id = ?", (1 if is_active else 0, device_id))
    return True


def delete_device(device_id: int) -> bool:
    """删除设备授权记录。"""
    with write_lock():
        x("DELETE FROM auth_devices WHERE id = ?", (device_id,))
    return True


def touch_device_activity(device_id: int, client_ip: str) -> None:
    """更新设备最后活跃时间与最后请求 IP。"""
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with write_lock():
            x("UPDATE auth_devices SET last_active_at = ?, last_ip = ? WHERE id = ?",
              (now, client_ip, device_id))
    except Exception:
        pass


def count_devices() -> int:
    """统计登记的设备总数。"""
    try:
        rows = q("SELECT COUNT(*) as c FROM auth_devices")
        return rows[0]["c"] if rows else 0
    except Exception:
        return 0

