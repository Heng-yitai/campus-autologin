# -*- coding: utf-8 -*-
"""
校园网守护 —— 每 60 秒确认一次真的能出网，断了自动重登。

为什么要单独一个进程：
    门户的会话会"无声失效"：服务端记着这台电脑还在线，网关那边早就不放行了。
    在你自己发现"网页打不开"之前，它不会通知任何人 —— 这个进程就是替你看门。

它做什么：
    1. 每 60 秒做一次**真实出网**探测（不是 ping 网关，那个永远通）
    2. 平时连着 2 次不通才动手（单次抖动不值得折腾）；
       **启动后的第一轮例外：测到不通就立刻修**，不再干等第二趟
       （2026-11-14 实测：原来开机会白等 40 秒，见 net_guard.py.bak-20261114）
    3. 启动后 5 分钟内如果门户还不可达（WiFi 还没连上），每 3 秒看一次，
       而不是睡 60 秒 —— 开机那几十秒每 60 秒真的会错过认证窗口
    4. 动手时先注销本机 IP 的旧会话、再重新认证 —— 这才是僵尸会话的解法
    5. 修好了接着看；修不好就退避（3 分钟 → 15 分钟），绝不反复刷账号

它不做什么：
    - 不在校园网里就不管（你连热点、在家时它安静待着）
    - 你点过「校园网下线」之后它暂停 30 分钟（读 guard_pause.dat）
    - 不碰 Wi-Fi 开关、不碰 DNS、不改任何系统设置

用法：
    双击「守护-启动.cmd」启动，「守护-停止.cmd」停止，状态看 net_guard.log。
    正常使用时它会随开机自动启动，不用管。
"""

import ctypes
import os
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

import campus_login as C

PID_FILE = os.path.join(APP_DIR, "net_guard.pid")
GUARD_LOG = os.path.join(APP_DIR, "net_guard.log")
GUARD_LOG_MAX = 512 * 1024
MUTEX_NAME = "CampusNetGuard_SingleInstance"

# 节奏
TICK = 60               # 正常探测间隔
FAIL_RETRY_TICK = 5     # 探测失败但还没到 2 次时的短等待（原 20 秒，开机实测太慢）
STARTUP_FAST_SECONDS = 300  # 启动后这段时间内，门户不可达也快速重试（等 WiFi 连上）
STARTUP_FAST_TICK = 3   # 启动窗口内的重试间隔
COOLDOWN_RETRY = 180    # 修一次没修好，等 3 分钟再来
COOLDOWN_GIVEUP = 900   # 连续修 3 次都没成，等 15 分钟（避免把账号刷进限制）
MAX_CONSECUTIVE_BAD = 3  # 连续失败到这么多次就进长冷却
HEARTBEAT = 5           # 每 5 轮（约 5 分钟）写一行心跳，方便确认它还活着
VERBOSE = "--verbose" in sys.argv[1:]   # 调试用：每轮都写


def g(text):
    """写守护日志。超过 512KB 就滚一份，只留最近两代。"""
    try:
        if os.path.exists(GUARD_LOG) and os.path.getsize(GUARD_LOG) > GUARD_LOG_MAX:
            old = GUARD_LOG + ".1"
            try:
                if os.path.exists(old):
                    os.remove(old)
                os.replace(GUARD_LOG, old)
            except Exception:
                pass
        with open(GUARD_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


def _create_mutex(name):
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        h = k32.CreateMutexW(None, False, name)
        if not h:
            return True, None
        return (k32.GetLastError() != 183), h      # 183 = ERROR_ALREADY_EXISTS
    except Exception:
        return True, None


def pause_until() -> float:
    """用户主动下线时 campus_login 会写下到期时间戳，这里读出来。"""
    try:
        with open(os.path.join(APP_DIR, "guard_pause.dat"), "r", encoding="utf-8") as f:
            return float(f.read().strip() or 0)
    except Exception:
        return 0.0


def write_pid():
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def _do_repair_locked(cfg):
    """修一次：注销本机 IP 的旧会话 → 重新认证 → 用真实出网验收。

    返回 True/False。整个过程不弹窗口，只写日志。
    """
    # 动手之前先做一次更严格的复核。
    # 为什么：探测为了快用的是 4 秒超时，而校园网偶尔会出现 3~4 秒的
    # 瞬时延迟，2026-11-08 实测 12 次里有 1 次被误判成"不通"。
    # 误判的代价是不对等的 —— 误判成"通"只是晚一轮再测，
    # 误判成"不通"却会白注销一次、白断一次网。所以这里宁可多花 5 秒。
    if C.internet_ok(5.0, 2):
        g("复核发现网络其实是通的（刚才只是慢），取消本次修复")
        return True

    # 和手动登录互斥：用户正好在点登录时，这边让开
    if not C.acquire_single_instance():
        g("手动登录正在进行，本轮让开")
        return False

    if not C.probe_portal(2.0):
        g("认证服务器不可达，判定为已离开校园网，不处理")
        return False

    try:
        ip = C.get_local_ip()
    except Exception as e:
        g("取本机 IP 失败 %r" % e)
        return False

    # ① 先清掉旧会话。失败也不阻塞 —— 直接重登多数也能覆盖掉。
    try:
        raw = C.request_logout(ip)
        lok, ltxt = C.judge_logout(raw)
        g("注销 %s -> %s | %s" % (ip, "成功" if lok else "失败", ltxt[:120]))
    except Exception as e:
        g("注销异常 %r（继续重登）" % e)

    time.sleep(1.5)

    # ② 重新认证
    try:
        raw = C.request_login(cfg["account"], cfg["password"], cfg.get("isp", "@telecom"))
    except Exception as e:
        g("登录请求异常 %r" % e)
        return False

    ok, text, pending = C.judge(raw)
    g("登录响应 ok=%s pending=%s | %s | %s" % (ok, pending, text[:120], raw.strip()[:200]))

    if not ok:
        return False

    # ③ 验收：不管服务器怎么说，只看真实出网
    if C.internet_ok(3.0, 2):
        g("验收通过：真实出网正常")
        return True

    g("验收失败：服务器说成功，但真实出网仍不通")
    return False


def do_repair(cfg):
    """包一层，保证不管从哪条路返回都把单实例锁交出去。

    2026-11-24 故障复盘：_do_repair_locked 里抢了
    CampusAutoLogin_SingleInstance 互斥体来实现「手动登录时让开」，
    但它有 6 个 return 出口，全都没关句柄。守护进程常驻不退，
    锁就被永久占住 —— 桌面「校园网一键登录」双击后一律判定
    "已有实例正在运行"并静默退出，面板再也打不开。

    所以这里用 try/finally 兜住：进入前谁抢的，退出时必须还。
    """
    try:
        return _do_repair_locked(cfg)
    finally:
        try:
            C.release_single_instance()
        except Exception:
            pass


def main():
    ok, _h = _create_mutex(MUTEX_NAME)
    if not ok:
        g("已有一个守护进程在跑，本次退出")
        return 0

    write_pid()
    started = time.time()
    g("守护启动 pid=%d 探测间隔=%ds（启动首轮立即修）" % (os.getpid(), TICK))

    try:
        cfg = C.load_config()
    except Exception as e:
        g("配置读取失败 %r，守护退出（请重新运行一次登录程序录入账号）" % e)
        return 2

    bad_streak = 0        # 连续探测失败次数
    repair_streak = 0     # 连续修复失败次数
    tick = 0              # 轮次，用于心跳
    first_round = True    # 启动后的第一轮：不通就立刻修，不等第二次
    announced_ready = False  # 是否已经记过"出网正常"的时间戳

    while True:
        tick += 1
        if VERBOSE or tick % HEARTBEAT == 1:
            g("看护中：第 %d 轮" % tick)
        try:
            # 用户主动下线 -> 暂停，别把人家的操作立刻覆盖掉
            pu = pause_until()
            if pu > time.time():
                left = int(pu - time.time())
                g("处于主动下线暂停期，还剩 %d 分钟，跳过" % (left // 60 + 1))
                time.sleep(min(TICK, 60))
                continue

            if not C.probe_portal(2.0):
                # 不在校园网（热点 / 家里 / 校外）：什么都不做
                if bad_streak or repair_streak:
                    g("已不在校园网，状态清零")
                bad_streak = 0
                repair_streak = 0
                # 刚启动时多半是 WiFi 还没连上，快速重试；过了这个窗口才退回 60 秒
                if time.time() - started < STARTUP_FAST_SECONDS:
                    time.sleep(STARTUP_FAST_TICK)
                else:
                    time.sleep(TICK)
                continue

            if C.internet_ok(3.0, 1):
                if bad_streak:
                    g("网络已恢复正常")
                if not announced_ready:
                    announced_ready = True
                    g("启动后 %d 秒确认出网正常" % int(time.time() - started))
                bad_streak = 0
                repair_streak = 0
                first_round = False
                time.sleep(TICK)
                continue

            # ---- 探测到不通 ----
            bad_streak += 1
            need = 1 if first_round else 2
            g("探测失败 %d 次（门户可达但出不了网）%s"
              % (bad_streak, "｜启动首轮，立即修复" if need == 1 else ""))
            if bad_streak < need:
                time.sleep(FAIL_RETRY_TICK)
                continue

            g("连续 %d 次不通，开始修复（第 %d 次尝试）" % (bad_streak, repair_streak + 1))
            fixed = do_repair(cfg)
            bad_streak = 0
            first_round = False

            if fixed:
                g("修复成功，恢复看护")
                repair_streak = 0
                time.sleep(TICK)
            else:
                repair_streak += 1
                if repair_streak >= MAX_CONSECUTIVE_BAD:
                    g("连续修复 %d 次都失败，进入长冷却 %d 分钟（避免把账号刷进限制状态）"
                      % (repair_streak, COOLDOWN_GIVEUP // 60))
                    time.sleep(COOLDOWN_GIVEUP)
                    repair_streak = 0
                else:
                    g("修复失败，%d 分钟后再试" % (COOLDOWN_RETRY // 60))
                    time.sleep(COOLDOWN_RETRY)

        except KeyboardInterrupt:
            g("收到中断，守护退出")
            return 0
        except Exception as e:
            g("主循环异常 %r，60 秒后继续" % e)
            time.sleep(TICK)


def stop():
    """读 pid 文件结束守护进程。"""
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except Exception:
        print("没找到 net_guard.pid，守护可能没在运行")
        return 1

    k32 = ctypes.windll.kernel32
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        print("PID %d 不存在（守护已经退出）" % pid)
        try:
            os.remove(PID_FILE)
        except Exception:
            pass
        return 1

    # 确认这个 pid 真的是 python，别误杀复用了同一 pid 的别的程序
    buf = ctypes.create_unicode_buffer(1024)
    size = ctypes.c_ulong(1024)
    name = ""
    if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
        name = os.path.basename(buf.value).lower()
    k32.CloseHandle(h)

    if "python" not in name:
        print("PID %d 现在是 %r，不是守护进程，已放弃" % (pid, name or "未知"))
        return 1

    h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        print("没有权限结束 PID %d" % pid)
        return 1
    k32.TerminateProcess(h, 0)
    k32.CloseHandle(h)
    print("已停止守护进程 PID %d" % pid)
    try:
        os.remove(PID_FILE)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    if "--stop" in sys.argv[1:]:
        sys.exit(stop())
    try:
        sys.exit(main())
    except Exception as exc:
        try:
            g("未捕获异常，守护退出：%r" % exc)
        except Exception:
            pass
        sys.exit(9)
