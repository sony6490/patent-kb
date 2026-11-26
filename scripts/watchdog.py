"""patent-kb 看门狗：服务或 OCR 驱动掉线时自动拉起。

用法（作为后台任务运行）: python scripts/watchdog.py

- 服务连续 3 次探活失败（约 3 分钟）→ 重新拉起
- 驱动 PID 文件指向的进程已不存在 → 重新拉起
- 驱动日志出现 "P1 OCR driver END"（全部跑完）→ 看门狗自行退出

启动方式受限说明：本环境禁用 schtasks / Start-Process / WMI 进程创建，
无法把进程托管给系统服务，因此用"看门狗 + 后台任务"实现自愈。
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
HERE = Path(__file__).parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
BASE = os.environ.get("PATENTKB_BASE", "http://127.0.0.1:8322")

LOG = HERE / "p1_ocr.log"
WD_LOG = HERE / "watchdog.log"
PID_FILE = HERE / "p1_driver.pid"

CHECK_INTERVAL = 60
SERVICE_FAIL_LIMIT = 3
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(WD_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def alive(pid: int) -> bool:
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return bool(ok) and code.value == STILL_ACTIVE


def auth_headers() -> dict:
    """服务端开启 auth_token 时探活需带口令，否则 401 会被误判为服务掉线。
    从环境变量或配置文件读取，不硬编码。"""
    token = os.environ.get("PATENTKB_TOKEN", "")
    if not token:
        try:
            cfg_path = Path(os.environ.get("PATENTKB_CONFIG",
                                           Path.home() / ".patentkb" / "config.json"))
            token = (json.loads(cfg_path.read_text(encoding="utf-8"))
                     .get("server", {}).get("auth_token", ""))
        except (OSError, ValueError):
            token = ""
    return {"Authorization": "Bearer " + token} if token else {}


def service_ok() -> bool:
    try:
        req = urllib.request.Request(BASE + "/api/admin/index/status", headers=auth_headers())
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


def spawn(cmd: list, env_extra: dict = None, logfile: Path = None) -> int:
    f = open(logfile, "ab") if logfile else subprocess.DEVNULL
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    p = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=f,
        stderr=f,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    return p.pid


def driver_running() -> bool:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return alive(pid)


def driver_finished() -> bool:
    try:
        tail = LOG.read_text(encoding="utf-8", errors="ignore")[-2000:]
    except OSError:
        return False
    return "P1 OCR driver END" in tail


def main() -> int:
    log("watchdog start")
    fails = 0
    while True:
        if driver_finished():
            log("OCR 驱动已正常跑完全部队列，看门狗退出")
            return 0

        if service_ok():
            fails = 0
        else:
            fails += 1
            log(f"服务探活失败 ({fails}/{SERVICE_FAIL_LIMIT})")
            if fails >= SERVICE_FAIL_LIMIT:
                pid = spawn(
                    [str(PY), "-m", "uvicorn", "patentkb.main:app",
                     "--host", "0.0.0.0", "--port", "8322", "--log-level", "warning"],
                    logfile=HERE / "service.log",
                )
                log(f"已重启服务 pid={pid}")
                fails = 0
                time.sleep(20)
                continue

        if not driver_running():
            if service_ok():
                pid = spawn(
                    [str(PY), str(HERE / "p1_ocr_driver.py")],
                    env_extra={"P1_SKIP_SCAN_WAIT": "1"},
                    logfile=HERE / "p1_driver.out.log",
                )
                log(f"驱动未运行，已重启 pid={pid}")
                time.sleep(20)
                continue
            log("驱动未运行，但服务尚未就绪，等待服务恢复")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
