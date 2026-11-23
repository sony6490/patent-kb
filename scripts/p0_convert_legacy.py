# -*- coding: utf-8 -*-
"""P0 v2: 批量把老 Office 格式 (.doc/.xls/.ppt) 转成新格式，带进程死亡自愈。

v2 改进（针对 22:35 Word COM 崩溃事故的根因修复）：
- 检测 COM 进程死亡（RPC 错误）：自动重启 Office 应用并继续，不整批报废；
- 嫌疑文件记账：进程死亡时正在转的文件计入失败；同一文件失败 ≥2 次永久跳过
  （防止损坏文件反复杀死进程造成死循环）；
- 状态增量落盘 p0_state.json（ok/fail/poison），中断重跑不重复不丢账；
- 结束时写 p0_convert_result.json + p0_convert_done.mark（P1 OCR 驱动的接力信号）。

不变的原则：原件只读、零修改、零删除；断点续跑；单文件失败不中断整批。
"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

import pythoncom
import win32com.client

def _kb_root() -> Path:
    """知识库根目录：命令行参数 > 环境变量 PATENTKB_KB_ROOT。

    仓库内不保留任何本机路径，缺参数时明确报错而不是猜一个目录。
    """
    src = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PATENTKB_KB_ROOT", "")
    if not src:
        sys.exit("用法: python p0_convert_legacy.py <知识库根目录>\n"
                 "  或设置环境变量 PATENTKB_KB_ROOT")
    p = Path(src).expanduser().resolve()
    if not p.is_dir():
        sys.exit(f"知识库目录不存在: {p}")
    return p


KB_ROOT = _kb_root()
LIST_FILE = Path(__file__).parent / "p0_legacy_files.json"
LOG_FILE = Path(__file__).parent / "p0_convert.log"
RESULT_FILE = Path(__file__).parent / "p0_convert_result.json"
STATE_FILE = Path(__file__).parent / "p0_state.json"
DONE_MARK = Path(__file__).parent / "p0_convert_done.mark"
MAX_APP_RESTARTS = 8
MAX_FAIL_PER_FILE = 2

FMT = {
    ".doc": (".docx", 16),    # wdFormatDocumentDefault
    ".xls": (".xlsx", 51),    # xlOpenXMLWorkbook
    ".ppt": (".pptx", 24),    # ppSaveAsOpenXMLPresentation
}

RPC_ERRORS = (-2147023170, -2147023174, -2147417848, -2147418111)  # RPC 失败/不可用/被叫方拒绝/调用方已退出


def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"ok": [], "fail": {}, "restarts": 0}


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def app_alive(app) -> bool:
    try:
        _ = app.Version  # 任意轻量属性调用
        return True
    except Exception:
        return False


class AppDead(Exception):
    pass


def convert_one(app, kind: str, src: Path, dst: Path, fmt: int) -> None:
    """转换单个文件；COM 进程死亡时抛 AppDead。"""
    try:
        if kind == "word":
            doc = app.Documents.Open(str(src), ReadOnly=True, AddToRecentFiles=False,
                                     Visible=False, ConfirmConversions=False)
            doc.SaveAs2(str(dst), FileFormat=fmt)
            doc.Close(SaveChanges=0)
        elif kind == "excel":
            wb = app.Workbooks.Open(str(src), ReadOnly=True, AddToMru=False,
                                    UpdateLinks=0, CorruptLoad=1)
            wb.SaveAs(str(dst), FileFormat=fmt)
            wb.Close(SaveChanges=False)
        else:
            pres = app.Presentations.Open(str(src), ReadOnly=True,
                                          WithWindow=False, Untitled=False)
            pres.SaveAs(str(dst), fmt)
            pres.Close()
    except Exception as e:
        # RPC 类错误 = Office 进程已死
        if getattr(e, "excepinfo", None) is not None or True:
            scode = None
            if e.args and isinstance(e.args[0], int):
                scode = e.args[0]
            if scode in RPC_ERRORS or not app_alive(app):
                raise AppDead(f"{scode} on {src.name}")
        raise


def run_group(kind: str, items: list, st: dict):
    """items: [(src,dst,fmt)] 返回处理完的条目；自动重启死亡的应用。"""
    ok, fail = [], []
    prog_file = f"p0_prog_{kind}.json"
    prog = Path(__file__).parent / prog_file
    done_paths = {r.split("|")[0] for r in st["ok"] if r.startswith(kind)}
    todo = [it for it in items if str(it[0]) not in done_paths]
    log(f"== {kind}: 待转 {len(todo)} ==")
    idx = 0
    while idx < len(todo):
        src, dst, fmt = todo[idx]
        try:
            t0 = time.time()
            app = CURRENT_APP[kind]
            convert_one(app, kind, src, dst, fmt)
            st["ok"].append(f"{kind}|{src}")
            ok.append(str(src))
            idx += 1
            dt = time.time() - t0
            if dt > 20:
                log(f"  (slow {dt:.0f}s: {src.name})")
            if len(ok) % 25 == 0:
                log(f"  {kind} {idx}/{len(todo)} ok={len(ok)} fail={len(fail)}")
                save_state(st)
        except AppDead as e:
            log(f"  !! {kind} 进程死亡: {e} -> 重启应用")
            st["restarts"] += 1
            st["fail"][str(src)] = st["fail"].get(str(src), 0) + 1
            fail.append(str(src))
            save_state(st)
            restart_app(kind)
            # 不 idx+=1：当前文件若失败次数未超限，下轮重试（会因 fail>=2 被计划阶段排除）
            if st["fail"][str(src)] >= MAX_FAIL_PER_FILE:
                log(f"  !! {src.name} 连续致进程死亡，永久跳过")
                idx += 1
        except Exception as e:
            st["fail"][str(src)] = st["fail"].get(str(src), 0) + 1
            fail.append(str(src))
            log(f"  FAIL {src.name}: {str(e)[:120]}")
            idx += 1
            save_state(st)
    save_state(st)
    return ok, fail


CURRENT_APP = {}


def restart_app(kind: str) -> None:
    old = CURRENT_APP.get(kind)
    if old is not None:
        try:
            old.Quit(0 if kind == "word" else None)
        except Exception:
            pass  # 进程已死，Quit 失败属预期
        CURRENT_APP[kind] = None
    # 根治：Office 崩溃后残留 Resiliency 状态会让下次 DispatchEx 弹
    # "安全模式"模态框把脚本挂死——每次重启前必须清掉。
    import winreg

    regmap = {"word": "Word", "excel": "Excel", "ppt": "PowerPoint"}
    try:
        root = winreg.HKEY_CURRENT_USER
        with winreg.OpenKey(root, r"Software\Microsoft\Office") as office_key:
            i = 0
            while True:
                try:
                    ver = winreg.EnumKey(office_key, i)
                    i += 1
                except OSError:
                    break
                res_key = rf"Software\Microsoft\Office\{ver}\{regmap[kind]}\Resiliency"
                try:
                    winreg.DeleteKey(root, res_key + r"\DisabledItems")
                except OSError:
                    pass
                try:
                    winreg.DeleteKey(root, res_key + r"\StartupItems")
                except OSError:
                    pass
                try:
                    winreg.DeleteKey(root, res_key)
                except OSError:
                    pass
    except OSError:
        pass
    progid = {"word": "Word.Application", "excel": "Excel.Application", "ppt": "PowerPoint.Application"}[kind]
    app = win32com.client.DispatchEx(progid)
    if kind == "word":
        app.Visible = False
        app.DisplayAlerts = 0
    elif kind == "excel":
        app.Visible = False
        app.DisplayAlerts = False
    else:
        app.Visible = True  # PowerPoint 不能不可见运行
    CURRENT_APP[kind] = app
    time.sleep(2)


def main():
    rels = json.loads(LIST_FILE.read_text(encoding="utf-8"))
    st = load_state()
    groups = {"word": [], "excel": [], "ppt": []}
    missing = 0
    skipped_heavy = 0
    for rel in rels:
        src = Path(os.path.normpath(str(KB_ROOT / rel)))
        if not src.exists():
            missing += 1
            continue
        ext = src.suffix.lower()
        if ext not in FMT:
            continue
        new_ext, fmt = FMT[ext]
        dst = Path(os.path.normpath(str(src.with_suffix(new_ext))))
        if dst.exists() or st["fail"].get(str(src), 0) >= MAX_FAIL_PER_FILE:
            continue
        kind = "word" if ext == ".doc" else "excel" if ext == ".xls" else "ppt"
        groups[kind].append((src, dst, fmt))
    skipped_heavy = sum(1 for k in groups for _ in [])
    log(f"plan: word={len(groups['word'])} excel={len(groups['excel'])} ppt={len(groups['ppt'])} "
        f"missing={missing} 已累计ok={len(st['ok'])} 已累计fail={len(st['fail'])} 重启次数={st['restarts']}")

    pythoncom.CoInitialize()
    total_fail = list(st["fail"].keys())

    for kind in ("word", "excel", "ppt"):
        if not groups[kind]:
            continue
        restart_app(kind)
        _, fails = run_group(kind, groups[kind], st)
        total_fail.extend(fails)

    # 收尾：正常退出应用
    for kind, app in CURRENT_APP.items():
        if app is not None:
            try:
                app.Quit(0 if kind == "word" else None)
            except Exception:
                pass

    RESULT_FILE.write_text(json.dumps(
        {"ok": st["ok"], "fail": st["fail"], "restarts": st["restarts"]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    DONE_MARK.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    log(f"DONE ok={len(st['ok'])} fail={len(st['fail'])} restarts={st['restarts']}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("FATAL " + traceback.format_exc())
        sys.exit(1)
