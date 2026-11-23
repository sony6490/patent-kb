# -*- coding: utf-8 -*-
"""提交前脱敏自检：拦截本机特有信息（用户名 / 本机盘符路径 / 私有目录名）。

用途：这个仓库会同步到 GitHub，任何提交前都应确认没把个人或本机信息带出去。
本文件自身不含任何用户名或私有目录字面量——用户名在运行时读取。

用法：
    python scripts/check_desensitize.py            # 检查所有已跟踪文件
    python scripts/check_desensitize.py --staged   # 只检查暂存区（pre-commit 钩子调用）

退出码：0 = 干净；1 = 有命中（打印 文件:行号 [类型] 内容）。
"""
from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

# 通用规则：与具体机器无关的"本机绝对路径"形态
GENERIC_RULES = [
    (re.compile(r"[A-Za-z]:[\\/]Users[\\/]", re.I), "Windows 用户目录绝对路径"),
    (re.compile(r"[A-Za-z]:[\\/](?:Download|WorkBuddy|Desktop|Documents)\b", re.I), "本机私有目录"),
    (re.compile(r"/(?:home|Users)/[a-z0-9._-]+/"), "Unix 用户目录绝对路径"),
]

# 文档里的示例路径属正常占位符，不算泄漏
ALLOW = [
    re.compile(r"[A-Za-z]:[\\/]patents[\\/]"),
    re.compile(r"[A-Za-z]:[\\/](?:其他项目文件夹|path[\\/]to)"),
]

MAX_BYTES = 2_000_000


def current_user_tokens() -> list[str]:
    """当前机器的用户名（不写死在代码里，换机器同样生效）。"""
    toks = {os.environ.get("USERNAME"), os.environ.get("USER"),
            _safe(getpass.getuser), _safe(lambda: Path.home().name)}
    return sorted({t for t in toks if t and len(t) >= 3}, key=len, reverse=True)


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def rules() -> list[tuple[re.Pattern, str]]:
    rs = [(re.compile(re.escape(t), re.I), "本机用户名") for t in current_user_tokens()]
    return rs + GENERIC_RULES


def files_to_check(staged: bool) -> list[Path]:
    # -z + UTF-8：中文/空格文件名也要正确还原，否则会被静默跳过
    args = (["diff", "--cached", "--name-only", "--diff-filter=ACM", "-z"] if staged
            else ["ls-files", "-z"])
    out = subprocess.run(["git", "-c", "core.quotepath=false", *args],
                         cwd=ROOT, capture_output=True).stdout
    names = [n for n in out.decode("utf-8", "replace").split("\0") if n.strip()]
    return [ROOT / n for n in names]


def scan(path: Path) -> list[tuple[int, str, str]]:
    """返回 [(行号, 类型, 该行内容)]。二进制/超大文件/本文件自身跳过。"""
    if path.resolve() == SELF or not path.is_file():
        return []
    if path.stat().st_size > MAX_BYTES:
        return []
    data = path.read_bytes()
    if b"\x00" in data[:2048]:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []

    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(a.search(line) for a in ALLOW):
            continue
        for rx, desc in rules():
            if rx.search(line):
                hits.append((lineno, desc, line.strip()[:120]))
                break
    return hits


def main() -> int:
    staged = "--staged" in sys.argv
    scope = "暂存区" if staged else "已跟踪文件"
    files = files_to_check(staged)
    bad = 0
    for p in files:
        for lineno, desc, text in scan(p):
            print(f"{p.relative_to(ROOT)}:{lineno}: [{desc}] {text}")
            bad += 1
    if bad:
        print(f"\n[x] 发现 {bad} 处本机特有信息（{scope}），已阻止。请脱敏后重试。")
        return 1
    print(f"[v] 脱敏自检通过（{scope}，{len(files)} 个文件）：未发现本机特有信息")
    return 0


if __name__ == "__main__":
    sys.exit(main())
