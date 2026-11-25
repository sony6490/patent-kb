"""关键路径固化回归测试。

覆盖：LLM 缓存不中毒（Blocker 1）、历史脏缓存自愈、路径越界防护（Blocker 2）、
同义词召回、引用校验、切库往返检索恢复、OCR chunk 不被增量扫描覆盖。

运行：uv run pytest tests/test_regression.py -q
"""
import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from patentkb import analyzer, config, db, fts, indexer, llm, ocr, prompts
from patentkb.main import _abs_path_safe, app

_Q = "无线充电定位误差补偿"
_GOOD = json.dumps({
    "keywords": [{"text": "无线充电", "lang": "zh", "source_refs": ["C1"]}],
    "classifications": [{"code": "H02J50/80", "explanation": "感应式充电", "source_refs": ["C1"]}],
    "patent_query": {"query": 'TI:(无线充电) AND CPC:(H02J50/80)',
                     "explanation": "基于片段", "source_refs": ["C1"]},
}, ensure_ascii=False)


def _fake_openai(monkeypatch, content: str):
    """替换 openai 模块，使 LLM 调用返回固定内容（不触网）。"""
    class _Completions:
        def create(self, **kw):
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))],
                usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            )

    fake = types.ModuleType("openai")
    fake.OpenAI = lambda **kw: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=_Completions()))
    monkeypatch.setitem(sys.modules, "openai", fake)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """独立临时知识库 + 独立 config，避免测试间污染。"""
    monkeypatch.setenv("PATENTKB_CONFIG", str(tmp_path / "config.json"))
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "note.txt").write_text(
        "电动汽车无线充电线圈定位误差补偿系统。磁耦合谐振式无线供电，线圈对位与校准。",
        encoding="utf-8",
    )
    (kb / "sub").mkdir()
    (kb / "sub" / "ir.txt").write_text(
        "红外热成像探测器用于瞄准镜光轴校准，非制冷焦平面阵列。",
        encoding="utf-8",
    )
    cfg = config.load_config()
    cfg["kb_path"] = str(kb.resolve())
    cfg["llm"].update({"base_url": "http://x/v1", "api_key": "sk-t", "model": "m"})
    config.save_config(cfg)
    db.reset()
    db.init(str(kb.resolve()))
    indexer.scan(str(kb.resolve()), full=True)
    yield {"kb": kb.resolve(), "cfg": cfg}
    db.reset()


# ---------- Blocker 1：LLM 缓存污染 ----------

def test_bad_json_never_pollutes_cache(env, monkeypatch):
    """坏 JSON 不落缓存；第二次请求仍走真实调用并重新计费，不命中脏缓存。"""
    _fake_openai(monkeypatch, "NOT_A_JSON_OBJECT")
    with pytest.raises(llm.LLMError):
        analyzer.analyze(_Q)
    assert db.q("SELECT * FROM llm_cache") == [], "非法输出不得写入缓存"

    billed_before = db.q("SELECT COUNT(*) n FROM usage_log WHERE cache_hit=0")[0]["n"]
    with pytest.raises(llm.LLMError):
        analyzer.analyze(_Q)
    billed_after = db.q("SELECT COUNT(*) n FROM usage_log WHERE cache_hit=0")[0]["n"]
    assert billed_after > billed_before, "第二次请求必须重新调用 LLM（脏缓存未固化）"
    assert db.q("SELECT * FROM llm_cache") == []


def test_stale_dirty_cache_auto_invalidated(env, monkeypatch):
    """旧版本遗留的脏缓存应在校验阶段自动失效，并由正确响应覆盖。"""
    chunks = analyzer._retrieve_chunks(_Q)
    assert chunks
    key = llm._cache_key("analyze", llm.normalize_text(_Q),
                         [c["chunk_id"] for c in chunks], "m", prompts.PROMPT_VERSION)
    with db.write_lock():
        db.x("INSERT OR REPLACE INTO llm_cache VALUES(?,?,?,?,?)",
             (key, "analyze", "BAD_LEGACY", "m", "2026-01-01"))

    _fake_openai(monkeypatch, _GOOD)
    result = analyzer.analyze(_Q)
    assert result["keywords"][0]["text"] == "无线充电"
    cached = db.q("SELECT response_json FROM llm_cache WHERE cache_key=?", (key,))
    assert cached and cached[0]["response_json"] == _GOOD, "脏缓存应被合法响应替换"


def test_valid_response_still_cached(env, monkeypatch):
    """正常响应仍命中缓存：第二次调用零费用、不计限额。"""
    _fake_openai(monkeypatch, _GOOD)
    r1 = analyzer.analyze(_Q)
    r2 = analyzer.analyze(_Q)
    assert r2["usage"]["cache_hit"] is True
    assert r1["keywords"][0]["text"] == r2["keywords"][0]["text"]


# ---------- Blocker 2：路径越界 ----------

def test_ocr_rejects_prefix_bypass(env, monkeypatch):
    """D:/data_evil 类前缀绕过必须被拒（ocr._safe_abs）。"""
    cfg = config.load_config()
    cfg["features"]["ocr"] = True
    config.save_config(cfg)
    evil = env["kb"].parent / "kb_evil.txt"
    evil.write_text("secret", encoding="utf-8")
    with db.write_lock():
        cur = db.x(
            "INSERT INTO files(rel_path, filename, ext, mtime, size, status) VALUES(?,?,?,?,?,?)",
            ("../kb_evil.txt", "kb_evil.txt", ".txt", 0, 6, "skipped_no_text"),
        )
    with pytest.raises(ValueError, match="越界"):
        ocr.ocr_file(cur.lastrowid)


def test_download_rejects_prefix_bypass(env):
    """既有 download 接口同步修复：同源前缀绕过返回 400。"""
    evil = env["kb"].parent / "kb_evil.txt"
    evil.write_text("secret", encoding="utf-8")
    with db.write_lock():
        db.x("INSERT INTO files(rel_path, filename, ext, mtime, size, status) VALUES(?,?,?,?,?,?)",
             ("../kb_evil.txt", "kb_evil.txt", ".txt", 0, 6, "ok"))
    row = db.q("SELECT * FROM files WHERE rel_path='../kb_evil.txt'")[0]
    with pytest.raises(HTTPException) as ei:
        _abs_path_safe(row)
    assert ei.value.status_code == 400
    # 合法路径仍放行
    row_ok = db.q("SELECT * FROM files WHERE rel_path='note.txt'")[0]
    assert _abs_path_safe(row_ok).is_file()


# ---------- 核心功能回归 ----------

def test_synonym_recall_and_toggle(env):
    """同义词召回生效且开关有效：搜"校位"（原文没有）召回"定位"。"""
    cfg = config.load_config()
    cfg["fts"]["synonyms_enabled"] = False
    config.save_config(cfg)
    off = fts.search("校位", limit=10)
    cfg = config.load_config()
    cfg["fts"]["synonyms_enabled"] = True
    config.save_config(cfg)
    on = fts.search("校位", limit=10)
    assert len(on) > len(off), f"同义词应增加命中：off={len(off)} on={len(on)}"
    assert any("定位" in h["snippet"] for h in on)


def test_verify_score_flags_inferred(env):
    """引用校验：原文可见词 verified，无关词 inferred。"""
    assert fts.verify_score("无线充电", "电动汽车无线充电系统")["verified"] is True
    assert fts.verify_score("量子纠缠", "电动汽车无线充电系统")["verified"] is False


def test_kb_switch_roundtrip(env, tmp_path):
    """切到新库检索隔离；切回原库检索结果完全恢复。"""
    c = TestClient(app, raise_server_exceptions=False)
    base = len(c.get("/api/search", params={"q": "无线充电"}).json()["hits"])
    kb_b = tmp_path / "kb_b"
    kb_b.mkdir()
    (kb_b / "b.txt").write_text("红外探测器 非制冷 热成像", encoding="utf-8")

    r = c.post("/api/kb/switch", json={"kb_path": str(kb_b)})
    assert r.status_code == 200, r.text
    assert len(c.get("/api/search", params={"q": "无线充电"}).json()["hits"]) == 0

    r = c.post("/api/kb/switch", json={"kb_path": str(env["kb"])})
    assert r.status_code == 200, r.text
    assert len(c.get("/api/search", params={"q": "无线充电"}).json()["hits"]) == base


def test_ocr_chunk_survives_incremental_scan(env):
    """标记 OCR 的 chunk 在增量扫描后不被文件名伪 chunk 覆盖（文件未变时 stable 跳过解析）。"""
    # 造一个真实存在的扫描件 PDF，使 mtime/size 与 DB 记录一致 → stable 命中
    pdf = env["kb"] / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%fake scanned pdf\n")
    st = pdf.stat()
    with db.write_lock():
        cur = db.x("INSERT INTO files(rel_path, filename, ext, mtime, size, status, meta_json) "
                   "VALUES(?,?,?,?,?,?,?)",
                   ("scan.pdf", "scan.pdf", ".pdf", st.st_mtime, st.st_size, "skipped_no_text",
                    '{"ocr": {"engine": "rapidocr", "chunks": 1}}'))
        fid = cur.lastrowid
    with db.transaction() as conn:  # 不可与 write_lock 嵌套（threading.Lock 不可重入）
        indexer._write_chunks(conn, fid, [
            {"content": "红外成像 校准 OCR 文字", "anchor": {"type": "ocr", "page": 1}}])
    before = db.q("SELECT COUNT(*) n FROM chunks WHERE file_id=?", (fid,))[0]["n"]
    report = indexer.scan(str(env["kb"]), full=False)
    assert report.get("unchanged", 0) >= 1, "未变化文件应被 stable 跳过"
    after = db.q("SELECT COUNT(*) n FROM chunks WHERE file_id=?", (fid,))[0]["n"]
    assert before == after == 1
    assert fts.search("OCR 文字", limit=5), "OCR 内容应仍可检索"


def test_query_field_terms_drops_classification_noise(env):
    """检索式溯源校验：分类号不被拆成 H02J50 / 80 噪音词。"""
    terms = fts.query_field_terms('TI:("无线充电" AND 定位) AND CPC:(H02J50/80)')
    assert "H02J50" not in terms and "80" not in terms
    assert "H02J50/80" in terms


# ---------- 需求1：检索结果展示项目名 + 完整绝对路径 ----------

def test_search_hits_have_project_and_abs_path(env):
    """检索命中应带 project（库根目录名）与 abs_path（完整绝对路径）。"""
    c = TestClient(app, raise_server_exceptions=False)
    hits = c.get("/api/search", params={"q": "无线充电"}).json()["hits"]
    assert hits
    for h in hits:
        assert h["project"] == env["kb"].name
        assert Path(h["abs_path"]).is_absolute()
        assert h["abs_path"] == str(env["kb"] / h["rel_path"])


def test_decorate_hits_rejects_traversal(env):
    """越界 rel_path 不抛异常，abs_path 置空供前端回退 rel_path。"""
    out = fts.decorate_hits([{"rel_path": "../evil.txt"}])
    assert out[0]["abs_path"] == ""
    assert out[0]["project"] == env["kb"].name
    assert fts.safe_abs_path("../evil.txt") is None


def test_search_project_changes_on_kb_switch(env, tmp_path):
    """切库后检索结果 project/abs_path 随之变化，切回恢复。"""
    c = TestClient(app, raise_server_exceptions=False)
    before = c.get("/api/search", params={"q": "无线充电"}).json()["hits"][0]
    assert before["project"] == env["kb"].name

    kb_b = tmp_path / "ProjectB"
    kb_b.mkdir()
    (kb_b / "b.txt").write_text("无线充电 线圈 定位", encoding="utf-8")
    assert c.post("/api/kb/switch", json={"kb_path": str(kb_b)}).status_code == 200
    c.post("/api/admin/index/scan", json={"full": True})
    hit_b = c.get("/api/search", params={"q": "无线充电"}).json()["hits"][0]
    assert hit_b["project"] == "ProjectB"
    assert hit_b["abs_path"].startswith(str(kb_b))

    assert c.post("/api/kb/switch", json={"kb_path": str(env["kb"])}).status_code == 200
    after = c.get("/api/search", params={"q": "无线充电"}).json()["hits"][0]
    assert after["project"] == env["kb"].name
    assert after["abs_path"] == before["abs_path"]


# ---------- 需求2：API 费用计算与展示 ----------

@pytest.mark.parametrize("ts,expected", [
    ("2026-11-20T09:00:00", True),   # 周五 9:00 峰
    ("2026-11-20T14:00:00", True),   # 周五 14:00 峰
    ("2026-11-20T11:59:59", True),   # 峰段末
    ("2026-11-20T12:00:00", False),  # 12:00 闲
    ("2026-11-20T13:59:59", False),  # 午休闲
    ("2026-11-20T18:00:00", False),  # 18:00 闲
    ("2026-11-21T10:00:00", False),  # 周六闲
])
def test_is_peak_windows(ts, expected):
    assert llm._is_peak(ts) is expected


def test_calc_cost_amounts():
    """各模型各档金额：1e6 输入=miss 价，1e6 输出=output 价。"""
    off = "2026-11-21T10:00:00"  # 周六闲时
    peak = "2026-11-20T10:00:00"  # 周五峰时
    # flash：闲 miss1/output4，峰 miss2/output8
    assert llm.calc_cost("deepseek-flash", off, 1_000_000, 0) == pytest.approx(1.0)
    assert llm.calc_cost("deepseek-flash", off, 0, 1_000_000) == pytest.approx(4.0)
    assert llm.calc_cost("deepseek-flash", peak, 1_000_000, 0) == pytest.approx(2.0)
    assert llm.calc_cost("deepseek-flash", peak, 0, 1_000_000) == pytest.approx(8.0)
    # v4-pro：闲 miss4.5/output13.5，峰 miss9/output27
    assert llm.calc_cost("deepseek-v4-pro", off, 1_000_000, 0) == pytest.approx(4.5)
    assert llm.calc_cost("deepseek-v4-pro", peak, 0, 1_000_000) == pytest.approx(27.0)
    # 未知模型 / 0 token 计 0
    assert llm.calc_cost("unknown", off, 1_000_000, 1_000_000) == 0.0
    assert llm.calc_cost("deepseek-flash", off, 0, 0) == 0.0


def test_ceil_cent():
    assert llm.ceil_cent(0.0062) == 0.01
    assert llm.ceil_cent(0.01) == 0.01
    assert llm.ceil_cent(0) == 0.0
    assert llm.ceil_cent(0.031) == 0.04


def test_log_cost_and_cache_hit_zero(env, monkeypatch):
    """真实调用计费且返回 cost；缓存命中 cost=0 且 DB 一致。"""
    cfg = config.load_config()
    cfg["llm"]["model"] = "deepseek-flash"
    config.save_config(cfg)
    _fake_openai(monkeypatch, _GOOD)
    r1 = analyzer.analyze(_Q)
    assert r1["usage"]["cost"] > 0, "deepseek-flash 真实调用应产生费用"
    assert "cost" in r1["usage"]

    r2 = analyzer.analyze(_Q)
    assert r2["usage"]["cache_hit"] is True
    assert r2["usage"]["cost"] == 0, "缓存命中费用为 0"

    rows = db.q("SELECT cache_hit, cost FROM usage_log ORDER BY id")
    billed = [r for r in rows if not r["cache_hit"]]
    cached = [r for r in rows if r["cache_hit"]]
    assert billed and all(r["cost"] > 0 for r in billed)
    assert cached and all(r["cost"] == 0 for r in cached)


def test_cost_summary_and_usage_api(env):
    """cost_summary 今日累计/最近一次正确，usage API 暴露 cost 对象与每行 cost。"""
    today = llm._today()
    ts1, ts2 = f"{today}T10:00:00", f"{today}T20:00:00"
    c1 = llm.calc_cost("deepseek-flash", ts1, 1_000_000, 0)
    c2 = llm.calc_cost("deepseek-flash", ts2, 1_000_000, 0)
    llm._log("analyze", "deepseek-flash", 1_000_000, 0, 0, None, ts=ts1)
    llm._log("analyze", "deepseek-flash", 1_000_000, 0, 0, None, ts=ts2)
    s = llm.cost_summary()
    assert s["today"] == pytest.approx(c1 + c2)
    assert s["today_calls"] == 2
    assert s["last_call"] == pytest.approx(c2)
    assert s["currency"] == "CNY"

    client = TestClient(app, raise_server_exceptions=False)
    d = client.get("/api/admin/usage", params={"days": 1}).json()
    assert "cost" in d and d["cost"]["today_calls"] == 2
    assert "cost" in d["usage"][0], "usage_summary 每行应带 cost"


def test_schema_migration_idempotent(tmp_path, monkeypatch):
    """老库（无 cost 列）升级补列且可重复运行；新 INSERT 不需指定 cost。"""
    monkeypatch.setenv("PATENTKB_CONFIG", str(tmp_path / "cfg.json"))
    kb = tmp_path / "oldkb"
    kb.mkdir()
    idx = kb / ".kbindex"
    idx.mkdir()
    import sqlite3
    raw = sqlite3.connect(str(idx / "index.db"))
    raw.execute("CREATE TABLE usage_log (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, "
                "request_type TEXT NOT NULL, model TEXT NOT NULL, prompt_tokens INTEGER, "
                "completion_tokens INTEGER, cache_hit INTEGER DEFAULT 0, error TEXT)")
    raw.execute("INSERT INTO usage_log(ts, request_type, model, prompt_tokens, completion_tokens, cache_hit, error) "
                "VALUES('2026-01-01T10:00:00','analyze','deepseek-flash',10,5,0,NULL)")
    raw.commit()
    raw.close()
    db.reset()
    try:
        db.init(str(kb.resolve()))
        db.init(str(kb.resolve()))  # 幂等：第二次不报错
        cols = {r["name"] for r in db.q("PRAGMA table_info(usage_log)")}
        assert "cost" in cols
        old = db.q("SELECT cost FROM usage_log")[0]
        assert old["cost"] == 0, "历史行 cost 默认 0，不补算"
        with db.write_lock():
            db.x("INSERT INTO usage_log(ts, request_type, model, cache_hit) VALUES(?,?,?,?)",
                 ("2026-01-02T10:00:00", "analyze", "deepseek-flash", 0))
        assert len(db.q("SELECT * FROM usage_log")) == 2
    finally:
        db.reset()


# ---------- 需求3：模型列表补全两个内置模型 ----------

def test_list_models_merges_builtin(env, monkeypatch):
    """供应商仅返回一个模型时，响应仍含两个内置模型且去重排序。"""
    fake = types.ModuleType("openai")

    class _Completions:
        def create(self, **kw):
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="OK"))],
                model="deepseek-flash",
            )

    fake.OpenAI = lambda **kw: types.SimpleNamespace(
        models=types.SimpleNamespace(list=lambda: types.SimpleNamespace(
            data=[types.SimpleNamespace(id="deepseek-flash"),
                  types.SimpleNamespace(id="deepseek-flash")])))
    monkeypatch.setitem(sys.modules, "openai", fake)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/config/list-models", json={"base_url": "http://x/v1", "api_key": "sk-t"})
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    assert "deepseek-flash" in models
    assert "deepseek-v4-pro" in models
    assert models == sorted(set(models)), "去重且排序"


def test_list_models_fallback_warning(env, monkeypatch):
    """供应商接口故障时软降级为 200 + warning，内置模型仍可选。"""
    fake = types.ModuleType("openai")
    fake.OpenAI = lambda **kw: (_ for _ in ()).throw(RuntimeError("net down"))
    monkeypatch.setitem(sys.modules, "openai", fake)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/config/list-models", json={"base_url": "http://x/v1", "api_key": "sk-t"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert "deepseek-flash" in d["models"] and "deepseek-v4-pro" in d["models"]
    assert d["warning"], "接口不可用应给 warning 提示"


# ---------- 需求2补充：读写分离（扫描长事务不得堵死读接口） ----------

def test_read_connection_isolated_from_write_transaction(env):
    """写连接被长事务占用时，db.q() 走独立读连接仍能秒级返回。

    复现本次线上事故：扫描长事务独占连接 → browse/status 等读接口全部挂死。
    修复后读连接与写连接物理分离，WAL 下互不阻塞。
    """
    entered = threading.Event()
    release = threading.Event()

    def long_writer():
        # 持 write_lock + 写连接，开一个事务并保持 2 秒
        with db.write_lock():
            conn = db.get()
            conn.execute("BEGIN")
            conn.execute("INSERT INTO files(rel_path, filename, ext, mtime, size, status) "
                         "VALUES('__lock_probe__.txt','x','.txt',0,1,'ok')")
            entered.set()
            release.wait(timeout=3)
            conn.rollback()

    t = threading.Thread(target=long_writer, daemon=True)
    t.start()
    assert entered.wait(timeout=2), "写线程未进入事务"

    # 读接口此刻必须不被阻塞（读连接在 WAL 下看到已提交快照）
    t0 = time.time()
    rows = db.q("SELECT COUNT(*) AS n FROM files")
    elapsed = time.time() - t0
    release.set()
    t.join(timeout=3)
    assert elapsed < 1.0, f"读查询被写事务阻塞了 {elapsed:.2f}s（读写未分离）"
    assert rows[0]["n"] >= 0
    assert db.get() is not db._get_read(), "读写必须是两条独立连接"


def test_browse_api_returns_during_scan_like_lock(env):
    """ browSE 接口在写事务占用期间仍返回 200（端到端验证，非仅 db 层）。"""
    entered = threading.Event()
    release = threading.Event()

    def long_writer():
        with db.write_lock():
            conn = db.get()
            conn.execute("BEGIN")
            conn.execute("INSERT INTO files(rel_path, filename, ext, mtime, size, status) "
                         "VALUES('__lock_probe2__.txt','x','.txt',0,1,'ok')")
            entered.set()
            release.wait(timeout=3)
            conn.rollback()

    t = threading.Thread(target=long_writer, daemon=True)
    t.start()
    assert entered.wait(timeout=2)

    c = TestClient(app, raise_server_exceptions=False)
    t0 = time.time()
    r = c.get("/api/browse", params={"path": ""})
    elapsed = time.time() - t0
    release.set()
    t.join(timeout=3)
    assert r.status_code == 200, r.text
    assert elapsed < 1.5, f"browse 被写事务阻塞了 {elapsed:.2f}s"


# ---------- 检索页整合：目录浏览 + 目录范围检索 ----------

def test_browse_returns_crumbs_and_parent(env):
    """面包屑与上级由后端给出，前端不再自己拆路径（深路径/特殊字符更稳）。"""
    c = TestClient(app)
    root = c.get("/api/browse", params={"path": ""}).json()
    assert root["current"] == ""
    assert root["parent"] is None
    assert [x["name"] for x in root["crumbs"]] == ["知识库"]
    assert {d["name"] for d in root["dirs"]} == {"sub"}

    sub = c.get("/api/browse", params={"path": "sub"}).json()
    assert sub["current"] == "sub"
    assert sub["parent"] == ""
    assert [(x["name"], x["rel_path"]) for x in sub["crumbs"]] == [("知识库", ""), ("sub", "sub")]
    assert [f["rel_path"] for f in sub["files"]] == ["sub/ir.txt"]


def test_browse_skips_kbindex_directories(env):
    """索引库及其备份不是用户资料，不应出现在目录浏览里。"""
    (env["kb"] / ".kbindex.bak-probe").mkdir()
    c = TestClient(app)
    dirs = [d["name"] for d in c.get("/api/browse", params={"path": ""}).json()["dirs"]]
    assert ".kbindex.bak-probe" not in dirs


def test_browse_rejects_traversal(env):
    c = TestClient(app)
    assert c.get("/api/browse", params={"path": "../../etc"}).status_code == 400


def test_search_scope_limits_to_subtree(env):
    """path 限定只召回该目录及其子目录，且 scope 原样回显给前端。"""
    c = TestClient(app)
    every = c.get("/api/search", params={"q": "校准", "limit": 20}).json()
    assert every["scope"] == ""
    assert every["hits"], "样本库应有命中"

    only = c.get("/api/search", params={"q": "校准", "limit": 20, "path": "sub"}).json()
    assert only["scope"] == "sub"
    assert only["hits"]
    assert all(h["rel_path"].startswith("sub/") for h in only["hits"])

    assert c.get("/api/search", params={"q": "无线充电", "limit": 20, "path": "sub"}).json()["hits"] == []


def test_search_scope_prefix_boundary_and_like_escape(env):
    """sub 不能召回 sub2；目录名里的 _ 不能当 LIKE 通配符。"""
    kb = env["kb"]
    for d in ("sub2", "a_b", "axb"):
        (kb / d).mkdir()
        (kb / d / "y.txt").write_text("红外校准", encoding="utf-8")
    indexer.scan(str(kb), full=False)
    c = TestClient(app)

    hits = c.get("/api/search", params={"q": "校准", "limit": 50, "path": "sub"}).json()["hits"]
    assert hits and all(h["rel_path"].startswith("sub/") for h in hits)
    assert not any(h["rel_path"].startswith("sub2/") for h in hits), "同级相似目录被误召回"

    under = c.get("/api/search", params={"q": "校准", "limit": 50, "path": "a_b"}).json()["hits"]
    assert under and all(h["rel_path"].startswith("a_b/") for h in under), "_ 被当成通配符"


# ---------- 检索页整合：目录浏览 + 目录范围检索 ----------

def test_browse_returns_crumbs_and_parent(env):
    """面包屑与上级由后端给出，前端不再自己拆路径（深路径/特殊字符更稳）。"""
    c = TestClient(app)
    root = c.get("/api/browse", params={"path": ""}).json()
    assert root["current"] == ""
    assert root["parent"] is None
    assert [x["name"] for x in root["crumbs"]] == ["知识库"]
    assert {d["name"] for d in root["dirs"]} == {"sub"}

    sub = c.get("/api/browse", params={"path": "sub"}).json()
    assert sub["current"] == "sub"
    assert sub["parent"] == ""
    assert [(x["name"], x["rel_path"]) for x in sub["crumbs"]] == [("知识库", ""), ("sub", "sub")]
    assert [f["rel_path"] for f in sub["files"]] == ["sub/ir.txt"]


def test_browse_skips_kbindex_directories(env):
    """索引库及其备份不是用户资料，不应出现在目录浏览里。"""
    (env["kb"] / ".kbindex.bak-probe").mkdir()
    c = TestClient(app)
    dirs = [d["name"] for d in c.get("/api/browse", params={"path": ""}).json()["dirs"]]
    assert ".kbindex.bak-probe" not in dirs


def test_browse_rejects_traversal(env):
    c = TestClient(app)
    assert c.get("/api/browse", params={"path": "../../etc"}).status_code == 400


def test_search_scope_limits_to_subtree(env):
    """path 限定只召回该目录及其子目录，且 scope 原样回显给前端。"""
    c = TestClient(app)
    every = c.get("/api/search", params={"q": "校准", "limit": 20}).json()
    assert every["scope"] == ""
    assert every["hits"], "样本库应有命中"

    only = c.get("/api/search", params={"q": "校准", "limit": 20, "path": "sub"}).json()
    assert only["scope"] == "sub"
    assert only["hits"]
    assert all(h["rel_path"].startswith("sub/") for h in only["hits"])

    assert c.get("/api/search", params={"q": "无线充电", "limit": 20, "path": "sub"}).json()["hits"] == []


def test_search_scope_prefix_boundary_and_like_escape(env):
    """sub 不能召回 sub2；目录名里的 _ 不能当 LIKE 通配符。"""
    kb = env["kb"]
    for d in ("sub2", "a_b", "axb"):
        (kb / d).mkdir()
        (kb / d / "y.txt").write_text("红外校准", encoding="utf-8")
    indexer.scan(str(kb), full=False)
    c = TestClient(app)

    hits = c.get("/api/search", params={"q": "校准", "limit": 50, "path": "sub"}).json()["hits"]
    assert hits and all(h["rel_path"].startswith("sub/") for h in hits)
    assert not any(h["rel_path"].startswith("sub2/") for h in hits), "同级相似目录被误召回"

    under = c.get("/api/search", params={"q": "校准", "limit": 50, "path": "a_b"}).json()["hits"]
    assert under and all(h["rel_path"].startswith("a_b/") for h in under), "_ 被当成通配符"
