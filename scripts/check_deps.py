#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查主程序是否只用了标准库。

为什么需要这个脚本：README 对外承诺「运行时零第三方依赖」，
这个承诺一旦被无意的 `import requests` 打破，用户会在没装包的机器上直接崩，
而不是在 CI 里被发现。

规则：
  · 标准库        → 通过
  · 本项目自己的模块 → 通过
  · OPTIONAL 里的 → 通过，但会打印提示（必须有降级路径，见文件末尾说明）
  · 其它          → 失败，退出码 1
"""
import os
import re
import sys


def _safe_console():
    """非 UTF-8 控制台（GBK / cp1252）下打印 ✅ 会抛 UnicodeEncodeError，
    让本该通过的检查显示成失败。这里只替换编不出来的字符，不改编码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


_safe_console()

# 参与运行时检查的文件。make_icon.py / make_logout_icon.py 是需要 Pillow
# 的开发工具，不随程序运行，不在检查范围内。
RUNTIME = [
    "campus_login.py",
    "net_guard.py",
    "setup_credentials.py",
    "flow_test.py",
]

# 标准库（只列本项目实际可能用到的，够用即可；新增标准库请补这里）
STDLIB = {
    "argparse", "base64", "collections", "ctypes", "datetime", "errno",
    "functools", "glob", "hashlib", "io", "json", "logging", "math", "os",
    "pathlib", "platform", "random", "re", "shutil", "socket", "string",
    "struct", "subprocess", "sys", "tempfile", "threading", "time",
    "traceback", "typing", "urllib", "winreg",
}

# 本项目自己的模块
LOCAL = {"campus_login", "net_guard", "setup_credentials", "flow_test"}

# 可选依赖：不装也能跑，但效果降级。
#   PIL —— 只有它存在时才会做「抓屏 + 高斯模糊」的自制毛玻璃；
#          缺失时 campus_login._open_ui 里的 except 会捕获 ImportError，
#          自动退回 Windows 系统 Acrylic 材质，再不行退回纯色实底。
OPTIONAL = {"PIL"}

IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)")


def scan():
    required_bad, optional_found = [], set()

    for f in RUNTIME:
        if not os.path.exists(f):
            required_bad.append("%s : 文件不存在" % f)
            continue
        with open(f, encoding="utf-8") as fh:
            for ln, line in enumerate(fh, 1):
                m = IMPORT_RE.match(line)
                if not m:
                    continue
                mod = m.group(1).split(".")[0]
                if mod in STDLIB or mod in LOCAL:
                    continue
                if mod in OPTIONAL:
                    optional_found.add((f, ln, mod))
                    continue
                required_bad.append("%s:%d : %s" % (f, ln, mod))

    return required_bad, optional_found


def main():
    bad, opt = scan()

    if opt:
        print("可选依赖（缺失时自动降级，不影响运行）：")
        for f, ln, mod in sorted(opt):
            print("  %-22s 第%-5d %s" % (f, ln, mod))
        print()

    if bad:
        print("❌ 发现非标准库的必需依赖：")
        for b in bad:
            print("  " + b)
        print()
        print("README 承诺「运行时零第三方依赖」。")
        print("如果这个依赖是新加的且确实必需，请同时更新 README 的「快速开始」和本脚本的 STDLIB/OPTIONAL。")
        return 1

    print("✅ 运行时依赖检查通过：除可选依赖外，全部为标准库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
