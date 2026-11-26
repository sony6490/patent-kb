"""一键恢复：把「服务 + 看门狗 + OCR 驱动」拉到运行状态。

由「专利知识库-启动.bat」双击调用。设计要点：
- 用户手动双击时，进程从资源管理器启动，不挂在 agent 会话的 job 下，
  因此不会随会话结束被回收（这是本环境唯一可行的脱离方式）。
- 重复运行安全：已在运行的不重复拉起，避免双实例抢数据库写锁。
- watchdog 会自行检测并拉起 OCR 驱动，本脚本只管服务和 watchdog。
- 全部路径相对本文件推导，不含任何本机特有信息。
"""
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
BASE = os.environ.get("PATENTKB_BASE", "http://127.0.0.1:8322")

STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def alive(pid: int) -> bool:
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return bool(ok) and code.value == STILL_ACTIVE


def _token() -> str:
    t = os.environ.get("PATENTKB_TOKEN", "")
    if t:
        return t
    try:
        cfg = Path(os.environ.get("PATENTKB_CONFIG",
                                  Path.home() / ".patentkb" / "config.json"))
        return json.loads(cfg.read_text(encoding="utf-8")).get("server", {}).get("auth_token", "")
    except (OSError, ValueError):
        return ""


def service_ok() -> bool:
    try:
        headers = {"Authorization": "Bearer " + _token()} if _token() else {}
        req = urllib.request.Request(BASE + "/api/wizard/status", headers=headers)
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def spawn(cmd: list, logfile: Path) -> int:
    f = open(logfile, "ab")
    p = subprocess.Popen(
        cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=f, stderr=f,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    return p.pid


def watchdog_pids() -> list:
    try:
        import psutil
        out = []
        for p in psutil.process_iter(["pid", "cmdline"]):
            cl = " ".join(p.info["cmdline"] or [])
            if "watchdog.py" in cl and "-c" not in cl and "psutil" not in cl:
                out.append(p.info["pid"])
        return out
    except Exception:
        return []


def main() -> int:
    print("=== patent-kb restore ===")
    if service_ok():
        print("[service] already running, skip")
    else:
        print("[service] not responding, starting ...")
        pid = spawn(
            [str(PY), "-m", "uvicorn", "patentkb.main:app",
             "--host", "0.0.0.0", "--port", "8322", "--log-level", "warning"],
            HERE / "service.log",
        )
        for _ in range(30):
            time.sleep(2)
            if service_ok():
                print(f"[service] ready (pid={pid})")
                break
        else:
            print("[service] NOT ready after 60s, check scripts/service.log")
            return 1

    wd = watchdog_pids()
    if wd:
        print(f"[watchdog] already running (pid={wd}), skip")
    else:
        pid = spawn([str(PY), str(HERE / "watchdog.py")], HERE / "watchdog.out.log")
        print(f"[watchdog] started (pid={pid}); it will bring up the OCR driver")

    print("=== done. closing this window will NOT stop the background processes ===")
    print("logs: scripts/service.log / scripts/watchdog.log / scripts/p1_ocr.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
