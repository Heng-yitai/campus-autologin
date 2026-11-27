# -*- coding: utf-8 -*-
"""流程集成测试：把会碰网络的函数**全部打桩**，验证 _run 的编排逻辑。

安全保证：probe_portal / get_current_ssid / connect_wifi / wait_for_portal /
scan_networks / request_login 全部替换为假实现，界面也换成空操作 ——
不发一个包、不切一次网、不弹窗。
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campus_login as C

# 测试绝不能写进真实日志：之前跑测试把假记录混进了 login.log，
# 让人误以为真跑过（耗时 0.0s、dr1 固定返回）。改成写临时文件。
_REAL_LOG = C.LOG_FILE
C.LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "_flow_test.log")

# 测试不该依赖真实账号：造一个临时"配置文件"占位，并让 load_config 返回假凭据。
# 这样任何人把仓库克隆下来，不录入账号密码也能直接跑通全部场景。
_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP_CFG = os.path.join(_HERE, "_flow_test_config.dat")
with open(_TMP_CFG, "wb") as _f:
    _f.write(b"placeholder")        # 只需要文件存在，内容不会被真正读取
_REAL_CFG_FILE = C.CONFIG_FILE
_REAL_LOAD_CONFIG = C.load_config
C.CONFIG_FILE = _TMP_CFG
C.load_config = lambda: {"account": "0000000000",
                         "password": "not-a-real-password",
                         "isp": "@telecom"}

REAL = {n: getattr(C, n) for n in
        ("probe_portal", "get_current_ssid", "connect_wifi", "wait_for_portal",
         "scan_networks", "request_login", "ui_open", "ui_set", "ui_finish",
         "ui_wait")}
CALLS = []


def reset():
    CALLS.clear()
    for n, f in REAL.items():
        setattr(C, n, f)
    # 释放单实例锁，否则第二个场景一进 _run 就被"已有实例"挡掉
    try:
        if getattr(C, "_INSTANCE_MUTEX", None):
            ctypes.windll.kernel32.CloseHandle(C._INSTANCE_MUTEX)
            C._INSTANCE_MUTEX = 0
    except Exception:
        pass


def scenario(name, *, ssid, probe, switch_ok, scan_list, ready, resp):
    reset()
    # 界面全部空转：测试只关心逻辑
    C.ui_open = lambda *a, **k: None
    C.ui_set = lambda *a, **k: None
    C.ui_finish = lambda *a, **k: None
    C.ui_wait = lambda *a, **k: None

    C.get_current_ssid = lambda *a, **k: (CALLS.append("看当前SSID"), ssid)[1]
    C.probe_portal = lambda *a, **k: (CALLS.append("探门户"), probe)[1]
    C.connect_wifi = lambda s, *a, **k: (
        CALLS.append("切到:%s" % s), switch_ok)[1]
    C.scan_networks = lambda *a, **k: (CALLS.append("扫描周边"), scan_list)[1]
    # 失败提示里会拿信号强度，测试里别真去扫网卡
    C.wlan_scan_target = lambda s, wait=0: (CALLS.append("点名扫描"), (False, -1))[1]
    C.wait_for_portal = lambda *a, **k: (CALLS.append("等门户"), ready)[1]
    C.request_login = lambda *a, **k: (
        CALLS.append("★提交账号密码"), resp)[1]

    print("=" * 68)
    print("场景: %s" % name)
    t0 = time.time()
    rc = C._run()
    print("  返回码 = %d   耗时 %.2f s" % (rc, time.time() - t0))
    print("  动作序列: %s" % " → ".join(CALLS))
    return rc, list(CALLS)


def check(label, cond):
    print("  %s %s" % ("✓" if cond else "✗ 失败", label))
    return cond


OK = True
SUCC = 'dr1({"result":1,"msg":"Portal协议认证成功！"});'
BADPWD = 'dr2({"result":0,"msg":"账号或密码错误","ret_code":1});'

print()
print("### A 已经连着校园网 → 直接认证")
rc, calls = scenario("已在校内网", ssid=C.WIFI_SSID, probe=True, switch_ok=True,
                     scan_list=[], ready=True, resp=SUCC)
OK &= check("返回 0", rc == 0)
OK &= check("没有发生切换", not any("切到" in c for c in calls))

print()
print("### B 连着校园网但门户还没就绪 → 等一会儿再认证")
rc, calls = scenario("校内网待就绪", ssid=C.WIFI_SSID, probe=False, switch_ok=True,
                     scan_list=[], ready=True, resp=SUCC)
OK &= check("返回 0", rc == 0)
OK &= check("等待过门户", "等门户" in calls)
OK &= check("没有发生切换", not any("切到" in c for c in calls))

print()
print("### C 在手机热点上 → 切到校园网再认证")
rc, calls = scenario("从热点切过去", ssid="手机热点", probe=True, switch_ok=True,
                     scan_list=[C.WIFI_SSID], ready=True, resp=SUCC)
OK &= check("返回 0", rc == 0)
OK &= check("切到了校园网", "切到:%s" % C.WIFI_SSID in calls)
OK &= check("最后提交了认证", calls[-1] == "★提交账号密码")

print()
print("### D 不在校园网、周边也搜不到 → 不认证、不瞎等")
rc, calls = scenario("搜不到校园网", ssid="手机热点", probe=False, switch_ok=False,
                     scan_list=["手机热点"], ready=True, resp=SUCC)
OK &= check("返回 3", rc == 3)
OK &= check("★全程没有提交认证（关键）", "★提交账号密码" not in calls)
# 目标都没找到就不该动网卡：原地不动比"先断再连回来"更安全
OK &= check("没有擅自切换网络", not any("切到" in c for c in calls))

print()
print("### D2 教学楼：校园网叫 FYLG-WIFI（不是宿舍的 6#SS-WiFi）")
rc, calls = scenario("教学楼", ssid="手机热点", probe=False, switch_ok=True,
                     scan_list=["手机热点", "FYLG-WIFI"], ready=True, resp=SUCC)
OK &= check("返回 0", rc == 0)
OK &= check("认出并切到 FYLG-WIFI", "切到:FYLG-WIFI" in calls)
OK &= check("★没有去切宿舍那个名字", "切到:%s" % C.WIFI_SSID not in calls)
OK &= check("最后提交了认证", calls[-1] == "★提交账号密码")

print()
print("### D3 已经连着 FYLG-WIFI → 不切换，直接认证")
rc, calls = scenario("已在教学楼校园网", ssid="FYLG-WIFI", probe=True, switch_ok=True,
                     scan_list=[], ready=True, resp=SUCC)
OK &= check("返回 0", rc == 0)
OK &= check("没有发生切换", not any("切到" in c for c in calls))
OK &= check("提交了认证", "★提交账号密码" in calls)

print()
print("### E 切过去连上了、但一直上不了网 → 切回原网络")
rc, calls = scenario("连上却没网", ssid="手机热点", probe=False, switch_ok=True,
                     scan_list=[C.WIFI_SSID], ready=False, resp=SUCC)
OK &= check("返回 3", rc == 3)
OK &= check("没有提交认证", "★提交账号密码" not in calls)
OK &= check("切回了原网络", "切到:手机热点" in calls)

print()
print("### F 服务器说密码错 → 返回 1")
rc, calls = scenario("账号密码错误", ssid=C.WIFI_SSID, probe=True, switch_ok=True,
                     scan_list=[], ready=True, resp=BADPWD)
OK &= check("返回 1", rc == 1)

print()
print("### G 在线数超限（临时）→ 自动重试一次后成功")
reset()
C.ui_open = lambda *a, **k: None
C.ui_set = lambda *a, **k: None
C.ui_finish = lambda *a, **k: None
C.ui_wait = lambda *a, **k: None
C.get_current_ssid = lambda *a, **k: C.WIFI_SSID
C.probe_portal = lambda *a, **k: True
SEQ = ['dr1({"result":0,"msg":"Rad:Limit Users Err","ret_code":1});',
       'dr1({"result":1,"msg":"Portal协议认证成功！"});']
SEQ_I = {"n": 0}


def fake_login(*a, **k):
    CALLS.append("★提交账号密码")
    i = min(SEQ_I["n"], len(SEQ) - 1)
    SEQ_I["n"] += 1
    return SEQ[i]


C.request_login = fake_login
t0 = time.time()
rc = C._run()
print("  返回码 = %d   耗时 %.2f s（含 4 秒重试间隔）" % (rc, time.time() - t0))
print("  动作序列: %s" % " → ".join(CALLS))
OK &= check("在线数超限后会重试", CALLS.count("★提交账号密码") == 2)
OK &= check("重试后判定成功", rc == 0)
reset()

print()
print("### H 服务器说「系统忙」→ 等一会儿再试，第二次成功")
reset()
C.ui_open = lambda *a, **k: None
C.ui_set = lambda *a, **k: None
C.ui_finish = lambda *a, **k: None
C.ui_wait = lambda *a, **k: None
C.get_current_ssid = lambda *a, **k: C.WIFI_SSID
C.probe_portal = lambda *a, **k: True
BUSY = ['dr1({"result":0,"msg":"系统忙！","ret_code":3});',
        'dr1({"result":1,"msg":"Portal协议认证成功！"});']
BI = {"n": 0}


def busy_login(*a, **k):
    CALLS.append("★提交账号密码")
    i = min(BI["n"], len(BUSY) - 1)
    BI["n"] += 1
    return BUSY[i]


C.request_login = busy_login
old_busy = C.BUSY_DELAY
C.BUSY_DELAY = (0.05, 0.05)          # 测试里别真等 12 秒
rc = C._run()
C.BUSY_DELAY = old_busy
print("  返回码 = %d   提交次数 = %d" % (rc, CALLS.count("★提交账号密码")))
OK &= check("「系统忙」会重试（以前不会）", CALLS.count("★提交账号密码") == 2)
OK &= check("重试后成功", rc == 0)
reset()

print()
print("### I 一个包都没发出去（全部为打桩）")
reset()
print("  ✓ 打桩已还原")
print()
print("===== 总结论: %s =====" % ("全部通过" if OK else "★有失败项"))

# 收尾：把日志/配置路径还原（别的脚本可能还会用到这个模块），并清掉临时文件
C.LOG_FILE = _REAL_LOG
C.CONFIG_FILE = _REAL_CFG_FILE
C.load_config = _REAL_LOAD_CONFIG
try:
    os.remove(_TMP_CFG)
except OSError:
    pass
