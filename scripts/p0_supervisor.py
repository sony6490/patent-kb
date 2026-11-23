# -*- coding: utf-8 -*-
"""P0 监督者：跑 p0_convert_legacy.py，挂死自动救援。

判定挂死：p0_convert.log 超过 STALE 秒无新内容。
救援动作：终止转换脚本 python 进程 -> 强杀"无主窗口"的 Office 进程
（Visible=False 的实例没有 MainWindowTitle，用户自己开的 Word 有窗口，不会误杀）
-> 重新启动转换脚本（状态已落盘，自动续跑）。
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE / "p0_convert_legacy.py"
LOG = HERE / "p0_convert.log"
DONE = HERE / "p0_convert_done.mark"
STALE = 600          # 日志无进展判定阈值（秒）
MAX_ROUNDS = 40


def log(msg):
    line = f"[watchdog {time.strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(HERE / "p0_watchdog.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def stale_seconds() -> float:
    try:
        return time.time() - LOG.stat().st_mtime
    except OSError:
        return 0.0


def kill_office_windowless():
    ps = ("Get-Process WINWORD,EXCEL,POWERPNT -ErrorAction SilentlyContinue | "
          "Where-Object { $_.MainWindowTitle -eq '' } | Stop-Process -Force")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, timeout=30)


def main():
    round_no = 0
    while not DONE.exists() and round_no < MAX_ROUNDS:
        round_no += 1
        log(f"round {round_no}: 启动转换脚本")
        proc = subprocess.Popen([sys.executable, str(SCRIPT)], cwd=str(HERE))
        while proc.poll() is None:
            time.sleep(20)
            if stale_seconds() > STALE:
                log(f"round {round_no}: 日志 {STALE}s 无进展，判定挂死 -> 救援")
                proc.kill()
                proc.wait(timeout=15)
                kill_office_windowless()
                time.sleep(5)
                break
        else:
            rc = proc.returncode
            if DONE.exists():
                log(f"round {round_no}: 转换正常完成 (rc={rc})")
                break
            log(f"round {round_no}: 脚本异常退出 rc={rc}，重新启动")
            kill_office_windowless()
            time.sleep(5)
        time.sleep(10)
    log(f"watchdog END rounds={round_no} done={DONE.exists()}")


if __name__ == "__main__":
    main()
