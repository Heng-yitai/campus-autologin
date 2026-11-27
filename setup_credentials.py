# -*- coding: utf-8 -*-
"""
录入 / 修改校园网账号密码（加密保存）

用法：右键本文件 -> 用 Python 运行，或在命令行执行：
    python setup_credentials.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_login

ISP_CHOICES = {
    "1": ("中国电信", "@telecom"),
    "2": ("中国移动", "@cmcc"),
    "3": ("中国联通", "@unicom"),
    "4": ("办公网 / 校园网直连", ""),
}


def main() -> int:
    print("=" * 56)
    print("  校园网一键登录 —— 账号配置")
    print("=" * 56)
    print("密码将以 Windows 加密形式保存在本机，不会明文存储。")
    print("（加密密钥与当前 Windows 账户绑定，拷走文件也打不开）\n")

    account = input("校园网账号（学号）：").strip()
    if not account:
        print("！账号不能为空")
        return 1

    password = input("密码：").strip()
    if not password:
        print("！密码不能为空")
        return 1

    print("\n运营商：")
    for k, (name, _) in ISP_CHOICES.items():
        tag = "（默认）" if k == "1" else ""
        print("  %s. %s%s" % (k, name, tag))
    choice = input("请选择 [1]：").strip() or "1"
    isp_name, isp = ISP_CHOICES.get(choice, ISP_CHOICES["1"])

    campus_login.save_config(account, password, isp)

    print("\n" + "-" * 56)
    print("配置已加密保存到：")
    print("  %s" % campus_login.CONFIG_FILE)
    print("运营商：%s" % isp_name)
    print("-" * 56)
    print("可以关闭本窗口了，双击桌面图标即可一键登录。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("\n配置失败：%r" % (exc,))
        input("按回车退出...")
        sys.exit(1)
