# -*- coding: utf-8 -*-
"""P1: 扫描件 PDF/图片 OCR 批处理驱动。

设计：
1. 等待索引重建扫描完成（scanning=false），避免 OCR 成果被全量遍历覆盖；
2. 等待 P0 转换完成（p0_convert_result.json 存在），触发一次增量扫描收录新副本；
3. 循环：从新索引库取 status=skipped_no_text 且未 OCR 过的文件，分批调
   /api/admin/ocr（工具内置通道，结果直接写入 chunks/fts_chunks，检索可用）；
4. 断点续跑：ocr_file 成功后会在 meta_json 写 "ocr" 标记，脚本据此跳过；
5. 失败重试上限 2 次，之后记入 p1_ocr_giveup.json 不再重试；
6. 全程日志 p1_ocr.log，可随时中断（Ctrl-C / 杀进程），重跑自动续。
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("PATENTKB_BASE", "http://127.0.0.1:8322")


def _kb_root() -> Path:
    """知识库根目录：命令行参数 > 环境变量 PATENTKB_KB_ROOT > 运行中服务上报的 kb_path。

    仓库内不保留任何本机路径；服务可用时无需任何手工配置。
    """
    src = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PATENTKB_KB_ROOT", "")
    if not src:
        last = None
        for _ in range(12):  # 服务可能刚启动，最多等约 2 分钟再放弃
            try:
                with urllib.request.urlopen(BASE + "/api/wizard/status", timeout=10) as r:
                    src = json.loads(r.read().decode("utf-8")).get("kb_path") or ""
                if src:
                    break
            except Exception as e:
                last = e
            time.sleep(10)
        if not src:
            sys.exit(f"无法从服务 {BASE} 获取 kb_path（{last}）\n"
                     f"请先启动服务，或显式传入知识库根目录")
    if not src:
        sys.exit("未拿到知识库根目录：请传入参数或设置 PATENTKB_KB_ROOT")
    p = Path(src).expanduser().resolve()
    if not p.is_dir():
        sys.exit(f"知识库目录不存在: {p}")
    return p


KB_ROOT = _kb_root()
DB = KB_ROOT / ".kbindex" / "index.db"
HERE = Path(__file__).parent
LOG = HERE / "p1_ocr.log"
GIVEUP = HERE / "p1_ocr_giveup.json"
P0_RESULT = HERE / "p0_convert_result.json"
P0_DONE_MARK = HERE / "p0_convert_done.mark"
# 2026-11-22 优化：原 BATCH=8 + 超时40分钟导致每批超时、服务端线程堆积
# （线程数一度达80、数据库写锁死锁）。改为小批+长超时+跳过扫描等待。
BATCH = 2
MAX_ATTEMPTS = 2
OCR_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")


def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def api_get(path: str, timeout: int = 15):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def api_post(path: str, body: dict, timeout: int = 3600):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_scan_idle(what: str) -> None:
    while True:
        try:
            d = api_get("/api/wizard/status")
            if not d.get("scanning"):
                log(f"[wait] {what}: 扫描已空闲")
                return
            log(f"[wait] {what}: 扫描进行中，60 秒后再查")
        except Exception as e:
            log(f"[wait] {what}: 服务暂不可达({e})，60 秒后重试")
        time.sleep(60)


def wait_p0() -> None:
    if P0_RESULT.exists() or P0_DONE_MARK.exists():
        return
    log("[wait] P0 转换未完成，10 分钟后再查")
    while not (P0_RESULT.exists() or P0_DONE_MARK.exists()):
        time.sleep(600)
    log("[wait] P0 已完成")


def next_ids(giveup: dict | None = None) -> list[dict]:
    """取待 OCR 的文件（按目录/路径排序，稳定推进）。

    2026-11-23 修复：giveup 的排除必须在 SQL 里做，不能在取回后过滤。
    原实现 `next_ids()` 取 LIMIT=BATCH 个再过滤 giveup，若队首恰好都是已放弃
    文件，过滤后为空 → 误判"全库完成"提前退出（实测 14986 件待处理时误退）。
    """
    conn = sqlite3.connect(str(DB), timeout=10)
    conn.execute("PRAGMA busy_timeout=8000")
    ph = ",".join("?" * len(OCR_EXTS))
    sql = (
        f"SELECT id, rel_path, size FROM files "
        f"WHERE status='skipped_no_text' AND ext IN ({ph}) "
        f"AND (meta_json IS NULL OR meta_json NOT LIKE '%\"ocr\"%') "
        f"AND size > 0"
    )
    params: list = list(OCR_EXTS)
    skip = [int(k) for k in (giveup or {}) if str(k).isdigit()]
    if skip:
        sql += f" AND id NOT IN ({','.join('?' * len(skip))})"
        params += skip
    sql += " ORDER BY rel_path LIMIT ?"
    params.append(BATCH)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [{"id": r[0], "rel": r[1], "size": r[2]} for r in rows]


def pending_count(giveup: dict | None = None) -> int:
    """与 next_ids 同口径的剩余待处理计数（含 ext 过滤、排除 giveup）。

    修正原 done 分支：它没加 ext 过滤，把 docx/txt/pptx 等也算进来，报出
    15073 与队列表述矛盾，容易误判。
    """
    conn = sqlite3.connect(str(DB), timeout=10)
    conn.execute("PRAGMA busy_timeout=8000")
    ph = ",".join("?" * len(OCR_EXTS))
    sql = (
        f"SELECT COUNT(*) FROM files "
        f"WHERE status='skipped_no_text' AND ext IN ({ph}) "
        f"AND (meta_json IS NULL OR meta_json NOT LIKE '%\"ocr\"%') "
        f"AND size > 0"
    )
    params: list = list(OCR_EXTS)
    skip = [int(k) for k in (giveup or {}) if str(k).isdigit()]
    if skip:
        sql += f" AND id NOT IN ({','.join('?' * len(skip))})"
        params += skip
    n = conn.execute(sql, params).fetchone()[0]
    conn.close()
    return n


def load_giveup() -> dict:
    if GIVEUP.exists():
        return json.loads(GIVEUP.read_text(encoding="utf-8"))
    return {}


def save_giveup(d: dict) -> None:
    GIVEUP.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    log("== P1 OCR driver start ==")
    if os.environ.get("P1_SKIP_SCAN_WAIT", "") == "1":
        log("[step] P1_SKIP_SCAN_WAIT=1，全量扫描已完成，跳过阶段1等待")
    else:
        wait_scan_idle("阶段1")
    wait_p0()

    # 触发增量扫描收录 P0 新副本，并等它结束
    # 2026-11-22: 增量扫描因反复重试 138 个伪装后缀的 failed 文件而停滞数小时，
    # 严重阻塞 OCR。提供 P1_SKIP_SCAN_WAIT 开关：设 1 时跳过等待直接进入 OCR。
    # 代价：P0 新副本的全文收录暂缓，之后可单独补跑；OCR 与增量扫描共用写连接，
    # db.transaction() 串行化，不会并发冲突。
    if os.environ.get("P1_SKIP_SCAN_WAIT", "") == "1":
        log("[step] P1_SKIP_SCAN_WAIT=1，跳过增量扫描等待，直接启动 OCR")
    else:
        log("[step] 触发增量扫描收录 P0 新副本")
        try:
            api_post("/api/admin/index/scan", {"full": False}, timeout=600)
        except Exception as e:
            log(f"[step] 增量扫描请求返回({e})——可能仍在后台进行，继续等待")
        time.sleep(20)
        wait_scan_idle("阶段2")

    giveup = load_giveup()
    done = 0
    while True:
        items = next_ids(giveup)
        if not items:
            # giveup 已在 SQL 中排除，此时确为真·无待处理文件
            n = pending_count(giveup)
            if n > 0:
                # 兜底：仍有剩余却取不到，说明异常，不贸然退出
                log(f"[warn] 取不到待处理文件但库中仍有 {n} 件，120 秒后重试")
                time.sleep(120)
                continue
            log(f"[done] 无待 OCR 文件（与队列同口径剩余: {n}，已放弃 {len(giveup)} 件）")
            break
        ids = [x["id"] for x in items]
        log(f"[batch] ids={ids[:8]} 首文件: {items[0]['rel'][:60]}")
        try:
            resp = api_post("/api/admin/ocr", {"file_ids": ids})
        except Exception as e:
            log(f"[batch] 请求失败: {e}，120 秒后重试")
            time.sleep(120)
            continue
        for r in resp.get("results", []):
            fid = str(r.get("file_id"))
            if r.get("ok"):
                done += 1
                if done % 10 == 0:
                    log(f"[progress] 累计成功 {done}")
            else:
                giveup[fid] = giveup.get(fid, 0) + 1
                if giveup[fid] >= MAX_ATTEMPTS:
                    save_giveup(giveup)
                log(f"[fail] file_id={fid}: {str(r.get('error'))[:100]}")
    save_giveup(giveup)
    log(f"== P1 OCR driver END, ok={done}, giveup={len(giveup)} ==")


if __name__ == "__main__":
    pid_file = HERE / "p1_driver.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    try:
        main()
    except KeyboardInterrupt:
        log("== interrupted by user ==")
    except Exception as e:
        import traceback

        log("FATAL " + traceback.format_exc())
        sys.exit(1)
    finally:
        try:
            pid_file.unlink()
        except OSError:
            pass
