"""patent-kb 配置管理：~/.patentkb/config.json 读写与掩码回显。"""
import json
import os
from pathlib import Path
from typing import Any

CONFIG_ENV = "PATENTKB_CONFIG"

DEFAULTS: dict[str, Any] = {
    "kb_path": "",
    "recent_kb_paths": [],
    "server": {"host": "127.0.0.1", "port": 8322, "auth_token": ""},
    "llm": {
        "base_url": "",
        "api_key": "",
        "model": "",
        "lite_model": "",
        "timeout_sec": 60,
        "max_output_tokens": 2000,
    },
    "fts": {"user_dict": "", "snippet_max_chars": 200,
            "synonyms_enabled": True, "synonyms_file": "", "max_synonyms_per_term": 3},
    "analyzer": {"top_n_chunks": 15, "max_chunk_chars": 4000, "followup_max_history": 3},
    "cost": {"daily_llm_call_limit": 50, "warn_tokens_per_call": 8000},
    "features": {"embedding": False, "vision": False, "ocr": False},
    "ocr": {"engine": "rapidocr", "dpi": 200, "max_pages_per_file": 30, "min_confidence": 0.5},
    "indexer": {
        "exclude_dirs": [
            ".kbindex", ".git", "node_modules", "__pycache__",
            "$RECYCLE.BIN", "System Volume Information",
            # 工具/软件目录，非专利业务资料：避免把代码库和缓存灌进知识库
            "patent-kb", ".venv", "venv", ".obsidian", ".workbuddy",
            "vault-audit", "v2rayN-Core", ".idea", ".vscode",
        ],
        "exclude_exts": [".tmp", ".ini", ".lnk", ".db"],
        "max_rows_per_sheet": 5000,
        "max_pdf_pages": 300,
    },
}


def config_path() -> Path:
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env)
    return Path.home() / ".patentkb" / "config.json"


def _merge(base: dict, override: dict) -> dict:
    """深度合并：override 中的非 None 值覆盖 base，缺失键沿用 base 默认。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def load_config() -> dict[str, Any]:
    p = config_path()
    if not p.exists():
        return json.loads(json.dumps(DEFAULTS))  # deep copy
    try:
        with open(p, "r", encoding="utf-8") as f:
            user = json.load(f)
    except (json.JSONDecodeError, OSError):
        user = {}
    return _merge(DEFAULTS, user)


def save_config(cfg: dict[str, Any]) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def mask_key(key: str) -> str:
    """api_key 掩码回显：sk-***abc。空串原样返回。"""
    if not key:
        return ""
    if len(key) <= 6:
        return "***"
    return f"{key[:3]}***{key[-3:]}"


def public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """对外回显：掩码 api_key。"""
    out = json.loads(json.dumps(cfg))
    if "llm" in out:
        out["llm"]["api_key"] = mask_key(cfg.get("llm", {}).get("api_key", ""))
    return out


def apply_config_update(cfg: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """PUT /config：子集更新。api_key 传空串或 '***' 开头表示保持不变。"""
    merged = _merge(cfg, patch)
    if not (patch.get("llm", {}) or {}).get("api_key"):
        merged["llm"]["api_key"] = cfg.get("llm", {}).get("api_key", "")
    return merged


def initialized(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("kb_path")) and Path(cfg["kb_path"]).is_dir()
