"""FastAPI 应用组装与全部路由。"""
import json
import subprocess
import warnings
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", category=SyntaxWarning)  # jieba 在 Python 3.14 下的正则转义警告

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import analyzer, config, db, fts, indexer, llm, ocr

app = FastAPI(title="patent-kb")
_STATIC_DIR = Path(__file__).parent / "static"


# ---------- Phase 2：auth_token 中间件（支持 Header 与 URL 参数双通道） ----------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    cfg = config.load_config()
    token = (cfg.get("server") or {}).get("auth_token", "")
    if token and request.url.path.startswith("/api"):
        auth_header = request.headers.get("Authorization", "")
        bearer_token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
        query_token = request.query_params.get("token", "")
        # Header 认证或 Query 认证（方便手机扫码/URL 直达）
        if bearer_token != token and query_token != token:
            return JSONResponse({"detail": "未授权，请提供正确的访问口令"}, status_code=401)
    return await call_next(request)


def _require_init() -> dict:
    cfg = config.load_config()
    if not config.initialized(cfg):
        raise HTTPException(400, "知识库未初始化，请先完成设置向导")
    return cfg


# ---------- 向导与配置 ----------

@app.get("/api/wizard/status")
def wizard_status():
    cfg = config.load_config()
    llm_cfg = cfg.get("llm", {})
    recents = [p for p in cfg.get("recent_kb_paths", []) if p and p != cfg.get("kb_path")]
    return {
        "initialized": config.initialized(cfg),
        "kb_path": cfg.get("kb_path", ""),
        "recent_kb_paths": recents[:5],
        "scanning": indexer.is_scanning(),
        "model": llm_cfg.get("model", ""),
        "model_masked": config.mask_key(llm_cfg.get("model", "")),
        "has_llm": bool(llm_cfg.get("base_url") and llm_cfg.get("api_key")),
        "has_lite_model": bool(llm_cfg.get("lite_model")),
    }


@app.post("/api/kb/switch")
def kb_switch(body: dict):
    """切换知识库目录。校验通过才落盘；失败自动回滚到原库。"""
    p = str(body.get("kb_path", "")).strip().strip('"').strip("'")
    if not p:
        raise HTTPException(400, "请输入新的知识库路径")
    fp = Path(p)
    if not fp.exists():
        raise HTTPException(400, f"路径不存在: {p}")
    if not fp.is_dir():
        raise HTTPException(400, f"不是文件夹: {p}")
    fp = fp.resolve()
    cfg = config.load_config()
    old = cfg.get("kb_path", "")
    if str(fp) == old:
        raise HTTPException(400, "新路径与当前知识库相同，无需切换")
    if indexer.is_scanning():
        raise HTTPException(409, "索引扫描进行中，请等待完成后再切换")
    try:
        probe_dir = fp / ".kbindex"
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe = probe_dir / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise HTTPException(400, f"目录不可写（无法创建索引库）: {e}")

    cfg["kb_path"] = str(fp)
    recents = [old] + [x for x in cfg.get("recent_kb_paths", []) if x != old]
    cfg["recent_kb_paths"] = [x for x in dict.fromkeys(recents) if x and x != str(fp)][:5]
    config.save_config(cfg)
    try:
        db.reset()
        db.init(str(fp))
    except Exception as e:
        cfg["kb_path"] = old  # 回滚
        cfg["recent_kb_paths"] = [x for x in cfg["recent_kb_paths"] if x][:5]
        config.save_config(cfg)
        db.reset()
        if old:
            try:
                db.init(old)
            except Exception:
                pass
        raise HTTPException(500, f"切换失败，已回滚到原知识库: {e}")

    status = indexer.index_status()
    return {"ok": True, "kb_path": str(fp), "previous": old, "index": status}


@app.post("/api/wizard/preview")
def wizard_preview(body: dict):
    p = str(body.get("kb_path", "")).strip()
    fp = Path(p)
    if not fp.is_dir():
        raise HTTPException(400, f"路径不存在: {p}")
    return indexer.preview_files(p)


@app.post("/api/wizard/setup")
def wizard_setup(body: dict):
    p = str(body.get("kb_path", "")).strip()
    fp = Path(p)
    if not fp.is_dir():
        raise HTTPException(400, f"路径不存在: {p}")
    # 可写检查：尝试创建 .kbindex
    try:
        (fp / ".kbindex").mkdir(parents=True, exist_ok=True)
        probe = fp / ".kbindex" / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise HTTPException(400, f"路径不可写: {e}")

    llm_patch = body.get("llm") or {}
    cfg = config.load_config()
    cfg["kb_path"] = str(fp)
    if llm_patch.get("base_url"):
        cfg["llm"]["base_url"] = llm_patch["base_url"].strip()
    if llm_patch.get("api_key"):
        cfg["llm"]["api_key"] = llm_patch["api_key"].strip()
    if llm_patch.get("model"):
        cfg["llm"]["model"] = llm_patch["model"].strip()
    if "lite_model" in llm_patch and llm_patch.get("lite_model"):
        cfg["llm"]["lite_model"] = llm_patch["lite_model"].strip()
    config.save_config(cfg)
    db.reset()
    db.init(str(fp))
    return {"ok": True}


@app.post("/api/config/test-llm")
def test_llm(body: dict):
    try:
        return llm.test_connection(
            str(body.get("base_url", "")).strip(),
            str(body.get("api_key", "")).strip(),
            str(body.get("model", "")).strip(),
        )
    except llm.DailyLimitError as e:
        raise HTTPException(429, str(e))
    except Exception as e:
        raise HTTPException(502, f"连接失败: {e}")


@app.post("/api/config/list-models")
def list_models(body: dict):
    """供应商模型列表 + 内置候选合并去重；接口故障软降级，保证内置模型始终可选。"""
    base_url = str(body.get("base_url", "")).strip()
    api_key = str(body.get("api_key", "")).strip()
    cfg = config.load_config()
    llm_cfg = cfg.get("llm", {})
    base_url = base_url or llm_cfg.get("base_url", "")
    api_key = api_key or llm_cfg.get("api_key", "")
    if not base_url:
        raise HTTPException(400, "请先填写 Base URL")
    if not api_key:
        raise HTTPException(400, "请先填写 API Key")
    vendor: list[str] = []
    warning = ""
    try:
        from openai import OpenAI
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=30)
        resp = client.models.list()
        vendor = [m.id for m in resp.data if getattr(m, "id", None)]
    except Exception as e:
        warning = f"供应商接口不可用，仅显示内置候选模型：{e}"
    models = sorted(set(vendor) | set(llm.BUILTIN_MODELS))
    out: dict[str, Any] = {"models": models}
    if warning:
        out["warning"] = warning
    return out


@app.get("/api/config")
def get_config():
    return config.public_config(config.load_config())


@app.put("/api/config")
def put_config(patch: dict):
    cfg = config.load_config()
    merged = config.apply_config_update(cfg, patch)
    config.save_config(merged)
    if "kb_path" in patch and patch["kb_path"] != cfg.get("kb_path"):
        db.reset()
        if config.initialized(merged):
            db.init(merged["kb_path"])
    return config.public_config(config.load_config())


# ---------- 浏览 ----------

@app.get("/api/browse")
def browse(path: str = ""):
    _require_init()
    cfg = config.load_config()
    root = Path(cfg["kb_path"]).resolve()
    exclude = set(cfg.get("indexer", {}).get("exclude_dirs", []))
    rel = (path or "").strip("/")
    target = (root / rel).resolve() if rel else root
    try:
        target.relative_to(root)      # 比 startswith 严格：/a/b 不会放过 /a/bc
    except ValueError:
        raise HTTPException(400, "非法路径")
    if not target.is_dir():
        raise HTTPException(404, "目录不存在")

    dirs, files = [], []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            # .kbindex* 是索引库及其备份，不是用户资料，浏览时不展示
            if entry.name in exclude or entry.name.startswith(".kbindex"):
                continue
            if entry.is_dir():
                dirs.append({"name": entry.name,
                             "rel_path": entry.relative_to(root).as_posix()})
            elif entry.is_file():
                files.append({"name": entry.name,
                              "rel_path": entry.relative_to(root).as_posix(),
                              "size": entry.stat().st_size})
    except OSError as e:
        raise HTTPException(500, str(e))

    # 只取这一层的索引状态：原来每次切目录都全表拉 6 万行再建 dict
    if rel:
        esc = rel.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        meta = db.q("SELECT id, rel_path, ext, status FROM files "
                    "WHERE rel_path LIKE ? ESCAPE '\\'", (esc + "/%",))
    else:
        meta = db.q("SELECT id, rel_path, ext, status FROM files "
                    "WHERE instr(rel_path, '/') = 0")
    fmeta = {r["rel_path"]: r for r in meta}

    out_files = []
    for f in files:
        m = fmeta.get(f["rel_path"])
        out_files.append({
            **f,
            "file_id": m["id"] if m else None,
            "ext": m["ext"] if m else Path(f["name"]).suffix.lower(),
            "status": m["status"] if m else "not_indexed",
        })

    # 面包屑由后端给出，前端不必再猜路径拼接规则
    segs = [s for s in rel.split("/") if s]
    crumbs = [{"name": "知识库", "rel_path": ""}]
    acc: list[str] = []
    for s in segs:
        acc.append(s)
        crumbs.append({"name": s, "rel_path": "/".join(acc)})

    return {
        "current": rel,
        "parent": "/".join(segs[:-1]) if segs else None,
        "crumbs": crumbs,
        "dirs": dirs,
        "files": out_files,
    }


# ---------- 文件 ----------

def _file_row(file_id: int) -> dict:
    rows = db.q("SELECT * FROM files WHERE id=?", (file_id,))
    if not rows:
        raise HTTPException(404, "文件不存在")
    return rows[0]


def _abs_path_safe(row: dict) -> Path:
    # 委托 fts.safe_abs_path 统一校验（路径段比较，避免前缀绕过）；越界仍抛 400
    fp = fts.safe_abs_path(row["rel_path"])
    if fp is None:
        raise HTTPException(400, "非法路径")
    return fp


@app.get("/api/file/chunks")
def file_chunks(file_id: int):
    _require_init()
    row = _file_row(file_id)
    chunks = db.q(
        "SELECT id, chunk_index, content, anchor_json, char_count FROM chunks WHERE file_id=? ORDER BY chunk_index",
        (file_id,),
    )
    for c in chunks:
        try:
            c["anchor"] = json.loads(c.pop("anchor_json"))
        except Exception:
            c["anchor"] = {}
    return {"file": {k: row[k] for k in ("id", "rel_path", "filename", "ext", "status", "error_msg")},
            "chunks": chunks}


@app.get("/api/file/download")
def file_download(file_id: int):
    _require_init()
    row = _file_row(file_id)
    fp = _abs_path_safe(row)
    if not fp.is_file():
        raise HTTPException(404, "文件已不存在")
    return FileResponse(str(fp), filename=row["filename"],
                        content_disposition_type="attachment")


@app.post("/api/file/reveal")
def file_reveal(body: dict):
    _require_init()
    row = _file_row(body.get("file_id", 0))
    fp = _abs_path_safe(row)
    if not fp.is_file():
        raise HTTPException(404, "文件已不存在")
    try:
        subprocess.run(["explorer", "/select,", str(fp)], check=False)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"打开资源管理器失败: {e}")


# ---------- 检索 ----------

@app.get("/api/search")
def search(q: str = Query(..., min_length=1), limit: int = 20, path: str = ""):
    _require_init()
    if limit > 100:
        limit = 100
    scope = (path or "").strip().strip("/")
    return {
        "hits": fts.decorate_hits(fts.search(q, limit, scope=scope)),
        "scope": scope,
    }


# ---------- 分析 ----------

@app.post("/api/analyze/estimate")
def analyze_estimate(body: dict):
    _require_init()
    text = str(body.get("request_text", "")).strip()
    if not text:
        raise HTTPException(400, "需求描述不能为空")
    return analyzer.estimate(text, bool(body.get("use_lite")))


@app.post("/api/analyze")
def do_analyze(body: dict):
    _require_init()
    text = str(body.get("request_text", "")).strip()
    if not text:
        raise HTTPException(400, "需求描述不能为空")
    try:
        return analyzer.analyze(text, bool(body.get("use_lite")), body.get("session"))
    except llm.DailyLimitError as e:
        raise HTTPException(429, str(e))
    except llm.LLMError as e:
        raise HTTPException(502, str(e))


@app.post("/api/analyze/image")
def analyze_image(body: dict):
    cfg = config.load_config()
    if not (cfg.get("features") or {}).get("vision", False):
        raise HTTPException(403, "图片识别未开启。可在设置页将 features.vision 设为 true（将产生按次 API 费用）")
    _require_init()
    row = _file_row(int(body.get("file_id", 0)))
    raise HTTPException(501, "图片识别为 Phase 2 功能，当前版本仅索引文件名")


# ---------- 索引与用量管理 ----------

@app.get("/api/admin/index/status")
def idx_status():
    _require_init()
    return indexer.index_status()


@app.post("/api/admin/index/scan")
def idx_scan(body: dict | None = None):
    cfg = _require_init()
    full = bool((body or {}).get("full", False))
    try:
        return indexer.scan(cfg["kb_path"], full=full)
    except Exception as e:
        raise HTTPException(500, f"扫描失败: {e}")


@app.post("/api/admin/ocr")
def admin_ocr(body: dict):
    """按需 OCR：必须显式传 file_ids（单个或少量），拒绝空列表与全库批跑。"""
    cfg = _require_init()
    if not (cfg.get("features") or {}).get("ocr"):
        raise HTTPException(403, "OCR 未启用。请在设置页开启 features.ocr（本地 CPU，无 API 费用）")
    ids = body.get("file_ids") or ([body["file_id"]] if body.get("file_id") else [])
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "必须显式指定 file_ids")
    if len(ids) > 20:
        raise HTTPException(400, "单次最多 20 个文件，请分批处理（避免长时间占用 CPU）")
    out = []
    for fid in ids:
        try:
            out.append(ocr.ocr_file(int(fid)))
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except (ValueError, RuntimeError) as e:
            out.append({"file_id": fid, "ok": False, "error": str(e)})
        except Exception as e:
            out.append({"file_id": fid, "ok": False, "error": f"OCR 失败: {e}"})
    return {"results": out}


@app.get("/api/admin/files")
def admin_files(status: str = "", limit: int = 200):
    _require_init()
    rows = db.q(
        "SELECT id, rel_path, filename, ext, status, error_msg, size, parsed_at FROM files "
        "WHERE (?='' OR status=?) ORDER BY status, rel_path LIMIT ?",
        (status, status, limit),
    )
    return {"files": rows}


@app.get("/api/admin/usage")
def admin_usage(days: int = 7):
    _require_init()
    days = min(days, 90)
    return {"usage": llm.usage_summary(days), "cost": llm.cost_summary()}


@app.get("/api/chunk/{chunk_id}")
def get_chunk(chunk_id: int):
    """来源浮层：按 chunk_id 取原文与所属文件。"""
    _require_init()
    rows = db.q(
        "SELECT c.id AS chunk_id, c.content, c.anchor_json, c.file_id, "
        "f.rel_path, f.filename, f.ext FROM chunks c JOIN files f ON f.id=c.file_id WHERE c.id=?",
        (chunk_id,),
    )
    if not rows:
        raise HTTPException(404, "片段不存在")
    r = rows[0]
    try:
        r["anchor"] = json.loads(r.pop("anchor_json"))
    except Exception:
        r["anchor"] = {}
    return r


# ---------- 静态前端与入口 ----------

app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")


def run() -> None:
    """console script 入口。"""
    import webbrowser

    cfg = config.load_config()
    host = (cfg.get("server") or {}).get("host", "127.0.0.1")
    port = (cfg.get("server") or {}).get("port", 8322)
    url = f"http://{host}:{port}"
    print(f"patent-kb 启动中: {url}")
    if host in ("127.0.0.1", "localhost"):
        webbrowser.open(url)
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")
