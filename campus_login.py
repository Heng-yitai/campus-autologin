# -*- coding: utf-8 -*-
"""
校园网一键登录

用法：
    双击桌面上的「校园网一键登录」图标即可。
    首次使用请先运行同目录下的 setup_credentials.py 录入账号密码。

安全说明：
    账号与密码使用 Windows DPAPI 加密后保存在 config.dat 中，
    加密密钥与当前 Windows 登录账户绑定，复制到其他电脑或其他账户下均无法解密。
    密码在任何时候都不会以明文形式写入磁盘或日志。
"""

import ctypes
import json
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes

# --------------------------------------------------------------------------
# 基本配置
# --------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    # 打包成 exe 之后 __file__ 指向临时解压目录，
    # 配置和日志必须跟着 exe 本身走，否则关掉程序就丢
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(APP_DIR, "config.dat")
LOG_FILE = os.path.join(APP_DIR, "login.log")

# 主动下线之后，让守护进程安静一段时间。
# 用户点「下线」通常是为了把名额让给手机，守护要是在 60 秒内又登回去，
# 这个动作就白做了，手机上还会提示"在线数已满"。
GUARD_PAUSE_FILE = os.path.join(APP_DIR, "guard_pause.dat")
GUARD_PAUSE_SECONDS = 30 * 60

PORTAL_HOST = "192.168.0.252"          # 认证服务器
PORTAL_PORT = 801                      # 认证接口端口
LOGIN_PATH = "/eportal/portal/login"   # 登录接口
LOGOUT_PATH = "/eportal/portal/logout" # 注销接口（按 IP 注销，固定凭据 drcom/123）
JS_VERSION = "4.2"
# 门户偶尔很慢：2026-09-18 实测单次请求耗到 10.6 秒才算完，
# 原来 6 秒会把它当成"没响应"，反而误判成故障。
TIMEOUT = 12                           # 秒

# 重试节奏：普通超时 4s/8s；服务器自己说"系统忙"时 12s/25s —— 忙的时候
# 连着敲没用，得给它喘口气（认证高峰通常是整点、中午）。
RETRY_DELAY = (4.0, 8.0)
BUSY_DELAY = (12.0, 25.0)
BUSY_HINTS = ("系统忙", "繁忙", "busy", "Busy", '"ret_code":3', '"ret_code": 3')
LOG_MAX_SIZE = 200 * 1024              # 日志超过 200KB 自动清空

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------
# Windows DPAPI 加解密（仅当前 Windows 用户可解密）
# --------------------------------------------------------------------------
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.windll.crypt32
_kernel32 = ctypes.windll.kernel32


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def dpapi_encrypt(data: bytes) -> bytes:
    src, _keep = _blob(data)
    out = _BLOB()
    if not _crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None, 0,
                                     ctypes.byref(out)):
        raise OSError("CryptProtectData 调用失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(out.pbData)


def dpapi_decrypt(data: bytes) -> bytes:
    src, _keep = _blob(data)
    out = _BLOB()
    if not _crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0,
                                       ctypes.byref(out)):
        raise OSError("CryptUnprotectData 调用失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(out.pbData)


def save_config(account: str, password: str, isp_suffix: str = "@telecom") -> None:
    payload = json.dumps({"account": account,
                          "password": password,
                          "isp": isp_suffix}, ensure_ascii=False).encode("utf-8")
    with open(CONFIG_FILE, "wb") as f:
        f.write(dpapi_encrypt(payload))


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError("尚未配置账号密码")
    with open(CONFIG_FILE, "rb") as f:
        return json.loads(dpapi_decrypt(f.read()).decode("utf-8"))


# --------------------------------------------------------------------------
# 首次配置向导（打包成 exe 后没有 setup_credentials.py，改用控制台录入）
# --------------------------------------------------------------------------
ISP_CHOICES = [("中国电信", "@telecom"), ("中国移动", "@cmcc"),
               ("中国联通", "@unicom"), ("办公网 / 校园直连", "")]


def console_setup() -> bool:
    """弹出控制台窗口让用户录入账号密码；成功返回 True。"""
    k32 = ctypes.windll.kernel32
    if not k32.AllocConsole():
        return False
    # 控制台分配后必须重新绑定标准流，否则 input()/print() 用不了
    sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
    sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
    sys.stderr = sys.stdout

    ok = False
    try:
        k32.SetConsoleTitleW("校园网一键登录 —— 首次配置")
        print("=" * 52)
        print("     校园网一键登录 —— 首次配置")
        print("=" * 52)
        print("密码将以 Windows 加密形式保存在本机，不会明文存储；")
        print("加密密钥与本机账户绑定，文件拷到别处也解不开。")
        print()
        account = input("校园网账号（学号）：").strip()
        if not account:
            print("\n！账号不能为空")
        else:
            password = input("密码：").strip()
            if not password:
                print("\n！密码不能为空")
            else:
                print()
                for i, (name, _) in enumerate(ISP_CHOICES, 1):
                    tail = "（默认）" if i == 1 else ""
                    print("  %d. %s%s" % (i, name, tail))
                choice = input("请选择运营商 [1]：").strip() or "1"
                try:
                    isp_name, isp = ISP_CHOICES[int(choice) - 1]
                except (ValueError, IndexError):
                    isp_name, isp = ISP_CHOICES[0]
                save_config(account, password, isp)
                print()
                print("已保存到：%s" % CONFIG_FILE)
                print("运营商：%s" % isp_name)
                ok = True
        print()
        input("按回车键关闭本窗口...")
    except Exception as exc:
        print("配置失败：%r" % (exc,))
        try:
            input("按回车键关闭...")
        except Exception:
            pass
    finally:
        try:
            k32.FreeConsole()
        except Exception:
            pass
    return ok


# --------------------------------------------------------------------------
# 界面：一块常驻的"玻璃"状态面板（无边框、置顶、不抢焦点）
#
# 设计要点（每一条都是被实际反馈逼出来的）：
#   1. **只有一个窗口**，从点下去那一刻出现，一直到出结果后才关闭 ——
#      状态在原来的位置原地更新，不再"弹一个消失、再弹一个消失"。
#   2. 玻璃质感：DWM 亚克力（Acrylic）材质做真背景模糊 + Win11 圆角；
#      任何一步不被支持就退回深色实底，绝不因为特效把主流程搞挂。
#   3. 出结果后至少停留 HOLD_OK / HOLD_ERR 毫秒 —— 看得清，又不会赖着不走。
#   4. 整个界面跑在独立线程里，主流程零等待。
# --------------------------------------------------------------------------
_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_dwmapi = ctypes.windll.dwmapi


def _enable_dpi_awareness() -> None:
    """声明本进程 DPI 感知。**必须在建任何窗口之前调用。**

    不声明的话，系统会按缩放比例骗程序：1920x1080 的屏在 125% 缩放下，
    GetSystemMetrics 会返回 1536x864，然后系统把整个窗口放大 1.25 倍 ——
    文字被拉伸就发虚（本机实测就是这样，面板上的字明显发软）。
    """
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_dpi_awareness()

try:
    _UI_DPI = _user32.GetDpiForSystem()
except Exception:
    _UI_DPI = 96
UI_SCALE = _UI_DPI / 96.0            # 100% 缩放 = 1.0


def _sc(v):
    """把"按 96 DPI 设计"的尺寸换算成当前缩放下的实际像素。"""
    return int(round(v * UI_SCALE))


WM_PAINT, WM_TIMER, WM_DESTROY = 0x000F, 0x0113, 0x0002
WM_LBUTTONDOWN, WM_CLOSE, WM_ERASEBKGND = 0x0201, 0x0010, 0x0014
WS_POPUP, WS_EX_TOPMOST = 0x80000000, 0x00000008
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000

CARD_W, CARD_H = _sc(460), _sc(210)   # 玻璃卡片本体尺寸（随系统缩放走）
GLASS_PAD = _sc(16)                   # 卡片外留白，用来放投影
WIN_W, WIN_H = CARD_W + GLASS_PAD * 2, CARD_H + GLASS_PAD * 2   # 实际窗口尺寸
CORNER_R = _sc(18)                    # 圆角半径
HOLD_OK, HOLD_ERR = 5200, 9500        # 结果停留时长（毫秒）
UI_CLASS = "CampusLoginGlassWnd"


def _rgb(r, g, b):
    """GDI 用 BGR 顺序，直接写 0xRRGGBB 会红蓝互换 —— 统一从这里生成。"""
    return (b << 16) | (g << 8) | r


ACCENT = {
    "work": _rgb(96, 170, 255),     # 进行中：蓝
    "ok":   _rgb(88, 224, 132),     # 成功：绿
    "err":  _rgb(255, 110, 110),    # 失败：红
}
C_TITLE = _rgb(245, 247, 250)
C_BODY = _rgb(196, 203, 214)
C_TRACK = _rgb(86, 94, 110)
C_FALLBACK_BG = _rgb(32, 30, 34)    # 亚克力不可用时的兜底底色
C_BORDER = _rgb(118, 128, 150)
C_META = _rgb(126, 135, 151)      # 次要文字：最近记录、按钮


class ACCENTPOLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int)]


class WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t)]


ACCENT_ENABLE_ACRYLICBLURBEHIND = 4     # Win10 1803+ / Win11 都支持
WCA_ACCENT_POLICY = 19
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
DWMWA_SYSTEMBACKDROP_TYPE = 38          # Win11 22H2+ 的原生材质开关
DWMSBT_TRANSIENTWINDOW = 3              # 亚克力

# 这几个是"怎么上玻璃"的可调项，默认值由实机截图扫出来的最佳组合决定
ACRYLIC_STATE = ACCENT_ENABLE_ACRYLICBLURBEHIND
ACRYLIC_FLAGS = 2
ACRYLIC_BACKDROP = 0                    # >0 时额外设置 DWMWA_SYSTEMBACKDROP_TYPE

LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC),
                ("fErase", wintypes.BOOL),
                ("rcPaint", wintypes.RECT),
                ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


# 面板状态。全局只有一块面板，但仍然加锁 —— 主线程写、界面线程读。
_ui = {
    "hwnd": None,
    "title": "",
    "body": "",
    "kind": "work",
    "hold_until": 0.0,        # 到点自动关窗的时间戳；0 表示还没出结果
    "pinned": False,          # 点了「下线」之后钉住，别在确认途中自己消失
    "confirm_until": 0.0,     # 「确认下线」的有效期，过了就退回普通状态
    "logout_busy": False,     # 正在执行下线
    "anim": 0,                # 动画相位 0~99
    "opaque": False,          # 玻璃做不出来时的兜底：走纯色实底
    "base_bmp": None,         # 预先糊好的背景位图（做毛玻璃用）
    "base_dc": None,
    "closed": threading.Event(),
}
_ui["closed"].set()           # 没开过面板时 ui_wait 立刻返回，不会干等
_ui_lock = threading.Lock()
_ui_thread = None
_ui_proc_ref = None           # 保持回调引用，防止被 GC 回收
_ui_class_ready = False       # 窗口类全进程只注册一次


# 显式声明参数类型：64 位下不声明会把指针当 int 传，触发 OverflowError
_P = ctypes.c_void_p
_kernel32.GetModuleHandleW.restype = _P

_user32.CreateWindowExW.restype = _P
_user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                    wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, _P, _P, _P, _P]
_user32.DefWindowProcW.restype = LRESULT
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
_user32.BeginPaint.restype = _P
_user32.BeginPaint.argtypes = [_P, ctypes.POINTER(PAINTSTRUCT)]
_user32.EndPaint.argtypes = [_P, ctypes.POINTER(PAINTSTRUCT)]
_user32.GetClientRect.argtypes = [_P, ctypes.POINTER(wintypes.RECT)]
_user32.ShowWindow.argtypes = [_P, ctypes.c_int]
_user32.SetTimer.argtypes = [_P, ctypes.c_ulonglong, wintypes.UINT, _P]
_user32.KillTimer.argtypes = [_P, ctypes.c_ulonglong]
_user32.DestroyWindow.argtypes = [_P]
_user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
_user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, _P]
_user32.MessageBoxW.argtypes = [_P, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), _P,
                                wintypes.UINT, wintypes.UINT]
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.FillRect.argtypes = [_P, ctypes.POINTER(wintypes.RECT), _P]
_user32.DrawTextW.argtypes = [_P, wintypes.LPCWSTR, ctypes.c_int,
                              ctypes.POINTER(wintypes.RECT), wintypes.UINT]

_gdi32.CreateSolidBrush.restype = _P
_gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
_gdi32.CreateFontW.restype = _P
_gdi32.SelectObject.restype = _P
_gdi32.SelectObject.argtypes = [_P, _P]
_gdi32.DeleteObject.argtypes = [_P]
_gdi32.SetBkMode.argtypes = [_P, ctypes.c_int]
_gdi32.SetTextColor.argtypes = [_P, wintypes.DWORD]
_gdi32.Ellipse.argtypes = [_P, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
_gdi32.CreateRoundRectRgn.restype = _P
_gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
_user32.SetWindowRgn.argtypes = [_P, _P, wintypes.BOOL]
_user32.InvalidateRect.argtypes = [_P, ctypes.POINTER(wintypes.RECT), wintypes.BOOL]
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
_dwmapi.DwmSetWindowAttribute.argtypes = [_P, wintypes.DWORD, _P, wintypes.DWORD]


def _apply_acrylic(hwnd) -> bool:
    """给窗口铺一层亚克力（Acrylic）玻璃材质：真背景模糊 + 暗色调。

    这是 Win10 1803 起就有的能力，Win11 上依然可用。设置失败不抛异常，
    由调用方退回深色实底 —— 界面特效永远不能拖垮主流程。
    """
    ok = False
    try:
        setter = _user32.SetWindowCompositionAttribute
        setter.restype = wintypes.BOOL
        setter.argtypes = [_P, ctypes.POINTER(WINCOMPATTRDATA)]

        policy = ACCENTPOLICY()
        policy.AccentState = ACRYLIC_STATE
        policy.AccentFlags = ACRYLIC_FLAGS
        # ABGR：alpha=C0（半透明），颜色取偏冷的深灰
        policy.GradientColor = 0xC0201C18
        policy.AnimationId = 0

        data = WINCOMPATTRDATA()
        data.Attribute = WCA_ACCENT_POLICY
        data.Data = ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p)
        data.SizeOfData = ctypes.sizeof(policy)
        ok = bool(setter(hwnd, ctypes.byref(data)))
    except Exception:
        ok = False

    if ACRYLIC_BACKDROP:
        try:
            v = ctypes.c_int(ACRYLIC_BACKDROP)
            _dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
                                          ctypes.byref(v), ctypes.sizeof(v))
            ok = True
        except Exception:
            pass
    return ok


def _apply_round_corners(hwnd, w, h) -> bool:
    """先交给 DWM 做圆角（Win11 边缘有抗锯齿）；不行就用区域裁剪兜底。"""
    try:
        pref = ctypes.c_int(DWMWCP_ROUND)
        hr = _dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref),
            ctypes.sizeof(pref))
        if hr == 0:
            return True
    except Exception:
        pass
    try:
        rgn = _gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, CORNER_R, CORNER_R)
        if rgn:
            _user32.SetWindowRgn(hwnd, rgn, True)
            return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------
# 自己动手做「毛玻璃」
#
# 为什么不用系统材质：实机截图实测，本机 Win11 22631 上
# ACCENT_ENABLE_ACRYLICBLURBEHIND(4) 只会上色、**根本不模糊** ——
# 面板底下的彩色条纹一点都透不上来（改 state=3/5 又只是变透明，也不糊）。
# 所以改为自己实现：显示前先抓一次窗口背后的屏幕 → 高斯模糊 + 加暗
# → 当作窗口背景刷上去。糊多大、透多少、圆角多少全在自己手里，
# 效果也比系统材质更接近"真毛玻璃"。
# --------------------------------------------------------------------------
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]


_gdi32.CreateCompatibleDC.restype = _P
_gdi32.CreateCompatibleDC.argtypes = [_P]
_gdi32.CreateCompatibleBitmap.restype = _P
_gdi32.CreateCompatibleBitmap.argtypes = [_P, ctypes.c_int, ctypes.c_int]
_gdi32.CreateDIBSection.restype = _P
_gdi32.CreateDIBSection.argtypes = [_P, ctypes.POINTER(_BITMAPINFO),
                                    wintypes.UINT, ctypes.POINTER(_P),
                                    _P, wintypes.DWORD]
_gdi32.BitBlt.argtypes = [_P, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, _P, ctypes.c_int, ctypes.c_int,
                          wintypes.DWORD]
_gdi32.CreatePen.restype = _P
_gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
_gdi32.RoundRect.argtypes = [_P, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int]
_gdi32.GetStockObject.restype = _P
_gdi32.GetStockObject.argtypes = [ctypes.c_int]


_gdi32.GetDIBits.restype = ctypes.c_int
_gdi32.GetDIBits.argtypes = [_P, _P, wintypes.UINT, wintypes.UINT, _P,
                             ctypes.POINTER(_BITMAPINFO), wintypes.UINT]
_gdi32.DeleteDC.argtypes = [_P]
_user32.GetDC.restype = _P
_user32.GetDC.argtypes = [_P]
_user32.ReleaseDC.argtypes = [_P, _P]


def _grab_screen(x, y, w, h):
    """抓屏幕一块矩形 → PIL RGB 图。只在面板显示**之前**调用。"""
    from PIL import Image
    hdc = _user32.GetDC(None)
    mem = _gdi32.CreateCompatibleDC(hdc)
    bmp = _gdi32.CreateCompatibleBitmap(hdc, w, h)
    _gdi32.SelectObject(mem, bmp)
    _gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, 0x00CC0020)          # SRCCOPY
    bi = _BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h                                      # 负数 = 自上而下
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    _gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).copy()
    _gdi32.DeleteObject(bmp)
    _gdi32.DeleteDC(mem)
    _user32.ReleaseDC(None, hdc)
    return img


def _make_dib(img):
    """PIL 图 → 32 位 DIB，并挂到一个内存 DC 上，方便后面 BitBlt。"""
    from PIL import Image
    r, g, b = img.convert("RGB").split()
    a = img.convert("L").point(lambda _: 255)
    # DIB 的字节序是 B,G,R,A —— 这里换成 BGRA，否则颜色会红蓝颠倒
    bgra = Image.merge("RGBA", (b, g, r, a)).tobytes()
    w, h = img.size
    bi = _BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    ppv = ctypes.c_void_p()
    hbmp = _gdi32.CreateDIBSection(None, ctypes.byref(bi), 0,
                                   ctypes.byref(ppv), None, 0)
    if not hbmp:
        return None, None
    ctypes.memmove(ppv, bgra, len(bgra))
    memdc = _gdi32.CreateCompatibleDC(None)
    _gdi32.SelectObject(memdc, hbmp)
    return hbmp, memdc


def _build_glass_base(x, y, out_w, out_h, pad, card_w, card_h, radius):
    """抓屏 → 模糊 → 投影 → 卡片加暗，合成一张可直接当背景贴的图。"""
    from PIL import Image, ImageDraw, ImageFilter
    cap = _grab_screen(x, y, out_w, out_h)

    base = cap.filter(ImageFilter.GaussianBlur(max(2, _sc(4)))).convert("RGBA")

    # 投影：两层叠加 —— 里层贴边深、外层铺得开，才有自然的衰减
    for grow, blur, alpha in ((_sc(4), _sc(9), 155), (_sc(14), _sc(15), 95)):
        sh = Image.new("L", (out_w, out_h), 0)
        ImageDraw.Draw(sh).rounded_rectangle(
            [pad - grow, pad - grow + _sc(5),
             pad + card_w + grow, pad + card_h + grow + _sc(5)],
            radius=radius + grow, fill=alpha)
        sh = sh.filter(ImageFilter.GaussianBlur(max(3, blur)))
        dark = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
        dark.putalpha(sh)
        base = Image.alpha_composite(base, dark)

    # 卡片本体：更重的模糊 + 偏冷的深色玻璃色调
    # 注意先裁出卡片那一块再合成 —— 尺寸不一致 PIL 会直接报 images do not match
    card = cap.crop((pad, pad, pad + card_w, pad + card_h))
    inner = card.filter(ImageFilter.GaussianBlur(max(4, _sc(17)))).convert("RGBA")
    inner = Image.alpha_composite(
        inner, Image.new("RGBA", (card_w, card_h), (17, 19, 27, 170)))
    mask = Image.new("L", (card_w, card_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, card_w - 1, card_h - 1], radius=radius, fill=255)
    inner.putalpha(mask)
    base.alpha_composite(inner, (pad, pad))
    return base.convert("RGB")


def _recent_records(limit: int = 3):
    """从 login.log 尾巴里取最近几条"判定成功/判定失败"，给面板显示用。

    日志行格式：`2026-10-23 21:13:53  判定成功: 认证通过`
    含换行的长文案只会把第一行留在时间戳那一行，所以这里只取到换行为止。
    """
    out = []
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-500:]
    except Exception:
        return out
    for line in reversed(lines):
        if len(line) < 22:
            continue
        stamp, text = line[5:16], line[21:].strip()
        if "判定成功" in text:
            out.append("%s   成功" % stamp)
        elif "判定失败" in text:
            reason = text.split("判定失败:", 1)[-1].strip().split("\n")[0][:20]
            out.append("%s   失败 · %s" % (stamp, reason))
        if len(out) >= limit:
            break
    return out


def _logout_btn_rect():
    """「下线」按钮的矩形。绘制和命中判定共用这一份，避免画一个位置、点另一个位置。"""
    ox = GLASS_PAD
    return (ox + CARD_W - _sc(22) - _sc(62), ox + _sc(20),
            ox + CARD_W - _sc(22), ox + _sc(48))


def _hit_logout(x: int, y: int) -> bool:
    bx1, by1, bx2, by2 = _logout_btn_rect()
    return bx1 <= x <= bx2 and by1 <= y <= by2


def _on_logout_click() -> None:
    """第一次点：变「确认?」并钉住面板；第二次点：真的下线。

    下线会断掉这台电脑的网，所以做成两步，不给用户误触的机会。
    """
    if _ui["logout_busy"]:
        return
    if time.time() >= _ui["confirm_until"]:
        _ui["confirm_until"] = time.time() + 5.0
        _ui["pinned"] = True                 # 别在确认途中自己消失
        _ui["hold_until"] = max(_ui["hold_until"], time.time() + 30.0)
        _ui["body"] = "再点一次「确认?」就会让这台电脑下线\n（下线后这台电脑会断网，手机才能顶上）"
        _ui_refresh()
        return

    _ui["confirm_until"] = 0.0
    _ui["logout_busy"] = True
    _ui["kind"] = "work"
    _ui["title"] = "正在下线"
    _ui["body"] = "正在释放这台电脑的校园网会话…"
    _ui["hold_until"] = time.time() + 30.0
    _ui_refresh()
    threading.Thread(target=_do_logout_bg, daemon=True).start()


def _do_logout_bg() -> None:
    try:
        ip = get_local_ip()
        raw = request_logout(ip)
        ok, text = judge_logout(raw)
        _ui["title"] = "已经下线" if ok else "下线失败"
        _ui["body"] = text
        _ui["kind"] = "ok" if ok else "err"
    except Exception as e:
        _ui["title"] = "下线失败"
        _ui["body"] = "请求异常：%r" % (e,)
        _ui["kind"] = "err"
    finally:
        _ui["logout_busy"] = False
        _ui["pinned"] = False
        _ui["hold_until"] = time.time() + (4.0 if _ui["kind"] == "ok" else 6.0)
        _ui_refresh()


def _ui_paint(hwnd) -> None:
    """画面板内容。

    **不填充整块背景** —— 背景交给亚克力材质来画，填了就没有玻璃感了。
    只有亚克力不可用时才铺一层兜底底色。
    """
    ps = PAINTSTRUCT()
    hdc = _user32.BeginPaint(hwnd, ctypes.byref(ps))
    rc = wintypes.RECT()
    _user32.GetClientRect(hwnd, ctypes.byref(rc))
    w, h = rc.right, rc.bottom
    accent = ACCENT.get(_ui["kind"], ACCENT["work"])
    ox = GLASS_PAD                            # 卡片在窗口里的偏移

    def fill(x1, y1, x2, y2, color):
        b = _gdi32.CreateSolidBrush(color)
        r = wintypes.RECT(x1, y1, x2, y2)
        _user32.FillRect(hdc, ctypes.byref(r), b)
        _gdi32.DeleteObject(b)

    if _ui["base_dc"]:
        # 毛玻璃背景：直接把预先糊好的那张图贴上去（整块，含投影）
        _gdi32.BitBlt(hdc, 0, 0, w, h, _ui["base_dc"], 0, 0, 0x00CC0020)
    elif _ui["opaque"]:
        fill(0, 0, w, h, C_FALLBACK_BG)
    else:
        fill(0, 0, w, max(1, _sc(1)), C_BORDER)
        fill(0, h - max(1, _sc(1)), w, h, C_BORDER)

    _gdi32.SetBkMode(hdc, 1)                     # TRANSPARENT

    # 卡片描边：1 像素的浅色内边，让"玻璃"有厚度
    if _ui["base_dc"]:
        pen = _gdi32.CreatePen(0, max(1, _sc(1)), C_BORDER)
        oldpen = _gdi32.SelectObject(hdc, pen)
        nullbrush = _gdi32.GetStockObject(5)     # NULL_BRUSH
        oldbr = _gdi32.SelectObject(hdc, nullbrush)
        _gdi32.RoundRect(hdc, ox, ox, ox + CARD_W, ox + CARD_H,
                         CORNER_R, CORNER_R)
        _gdi32.SelectObject(hdc, oldbr)
        _gdi32.SelectObject(hdc, oldpen)
        _gdi32.DeleteObject(pen)

    fill(ox, ox + _sc(14), ox + _sc(3), ox + CARD_H - _sc(14), accent)   # 左侧强调条

    dot = _gdi32.CreateSolidBrush(accent)        # 状态圆点
    oldb = _gdi32.SelectObject(hdc, dot)
    _gdi32.Ellipse(hdc, ox + _sc(26), ox + _sc(30),
                   ox + _sc(40), ox + _sc(44))
    _gdi32.SelectObject(hdc, oldb)
    _gdi32.DeleteObject(dot)

    font = _gdi32.CreateFontW(_sc(23), 0, 0, 0, 700, 0, 0, 0, 134, 0, 0, 0, 0,
                              "Microsoft YaHei")
    old = _gdi32.SelectObject(hdc, font)
    _gdi32.SetTextColor(hdc, C_TITLE)
    r1 = wintypes.RECT(ox + _sc(52), ox + _sc(26),
                       _logout_btn_rect()[0] - _sc(10), ox + _sc(58))
    _user32.DrawTextW(hdc, _ui["title"], -1, ctypes.byref(r1), 0x00000020)
    _gdi32.SelectObject(hdc, old)
    _gdi32.DeleteObject(font)

    font2 = _gdi32.CreateFontW(_sc(16), 0, 0, 0, 400, 0, 0, 0, 134, 0, 0, 0, 0,
                               "Microsoft YaHei")
    old2 = _gdi32.SelectObject(hdc, font2)
    _gdi32.SetTextColor(hdc, C_BODY)
    r2 = wintypes.RECT(ox + _sc(52), ox + _sc(62),
                       ox + CARD_W - _sc(22), ox + _sc(128))
    _user32.DrawTextW(hdc, _ui["body"], -1, ctypes.byref(r2), 0x00000010)
    _gdi32.SelectObject(hdc, old2)
    _gdi32.DeleteObject(font2)

    # 最近三条认证记录：一眼能看出"上次是什么时候、成没成"
    records = _recent_records(3)
    if records:
        font3 = _gdi32.CreateFontW(_sc(12), 0, 0, 0, 400, 0, 0, 0, 134, 0, 0, 0, 0,
                                   "Microsoft YaHei")
        old3 = _gdi32.SelectObject(hdc, font3)
        _gdi32.SetTextColor(hdc, C_META)
        ry = ox + _sc(132)
        for line in records:
            r3 = wintypes.RECT(ox + _sc(22), ry, ox + CARD_W - _sc(22), ry + _sc(17))
            _user32.DrawTextW(hdc, line, -1, ctypes.byref(r3), 0x00000020)
            ry += _sc(17)
        _gdi32.SelectObject(hdc, old3)
        _gdi32.DeleteObject(font3)

    # 右上角「下线」按钮：点一下变「确认」，再点才真的下线
    bx1, by1, bx2, by2 = _logout_btn_rect()
    confirming = time.time() < _ui["confirm_until"]
    busy = _ui["logout_busy"]
    btn_color = accent if (confirming or busy) else C_META
    pen = _gdi32.CreatePen(0, max(1, _sc(1)), btn_color)
    oldpen = _gdi32.SelectObject(hdc, pen)
    nullbrush = _gdi32.GetStockObject(5)          # NULL_BRUSH
    oldbr = _gdi32.SelectObject(hdc, nullbrush)
    _gdi32.RoundRect(hdc, bx1, by1, bx2, by2, _sc(10), _sc(10))
    _gdi32.SelectObject(hdc, oldbr)
    _gdi32.SelectObject(hdc, oldpen)
    _gdi32.DeleteObject(pen)

    label = "下线中" if busy else ("确认?" if confirming else "下线")
    font4 = _gdi32.CreateFontW(_sc(12), 0, 0, 0, 600, 0, 0, 0, 134, 0, 0, 0, 0,
                               "Microsoft YaHei")
    old4 = _gdi32.SelectObject(hdc, font4)
    _gdi32.SetTextColor(hdc, btn_color)
    r4 = wintypes.RECT(bx1, by1, bx2, by2)
    _user32.DrawTextW(hdc, label, -1, ctypes.byref(r4),
                      0x00000001 | 0x00000004 | 0x00000020)   # 居中 | 垂直居中 | 单行
    _gdi32.SelectObject(hdc, old4)
    _gdi32.DeleteObject(font4)

    # 底部进度线：干活时是一块来回跑的亮条；出结果后变成整条实色
    y = ox + CARD_H - _sc(22)
    bar = max(2, _sc(3))
    x0l, x0r = ox + _sc(20), ox + CARD_W - _sc(20)
    if _ui["hold_until"]:
        fill(x0l, y, x0r, y + bar, accent)
    else:
        fill(x0l, y, x0r, y + bar, C_TRACK)
        block = _sc(110)
        bx = x0l + int((x0r - x0l - block) * _ui["anim"] / 99.0)
        fill(bx, y, bx + block, y + bar, accent)

    _user32.EndPaint(hwnd, ctypes.byref(ps))


def _ui_wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_PAINT:
        try:
            _ui_paint(hwnd)
        except Exception as e:
            log("面板绘制异常: %r" % e)          # 画不出来也不能让它崩
        return 0

    if msg == WM_ERASEBKGND:
        return 1

    if msg == WM_TIMER:
        if _ui["hold_until"]:
            if time.time() >= _ui["hold_until"] and not _ui["pinned"]:
                _user32.KillTimer(hwnd, 1)
                _user32.DestroyWindow(hwnd)
        else:
            _ui["anim"] = (_ui["anim"] + 3) % 100
            _user32.InvalidateRect(hwnd, None, False)
        return 0

    if msg == WM_LBUTTONDOWN:
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        if _hit_logout(x, y):
            _on_logout_click()
            return 0
        # 出结果后点一下可提前关掉；干活途中不响应，免得误触把它弄没了
        if _ui["hold_until"]:
            _user32.KillTimer(hwnd, 1)
            _user32.DestroyWindow(hwnd)
        return 0

    if msg == WM_CLOSE:
        _user32.KillTimer(hwnd, 1)
        _user32.DestroyWindow(hwnd)
        return 0

    if msg == WM_DESTROY:
        _user32.PostQuitMessage(0)
        return 0

    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _ensure_ui_class(hinst) -> bool:
    """窗口类全进程只注册一次，而且不注销。

    RegisterClassW 是进程级、**不按线程隔离**的：重复注册会返回 1410
    （ERROR_CLASS_ALREADY_EXISTS，属正常现象）；注销则可能撞上界面线程
    还没来得及销毁的窗口，所以干脆不注销。
    """
    global _ui_proc_ref, _ui_class_ready
    if _ui_class_ready:
        return True
    wc = WNDCLASSW()
    wc.lpfnWndProc = WNDPROC(_ui_wndproc)
    _ui_proc_ref = wc.lpfnWndProc          # 持引用，防止回调被 GC 回收
    wc.hInstance = hinst
    wc.lpszClassName = UI_CLASS
    wc.hbrBackground = None
    if not _user32.RegisterClassW(ctypes.byref(wc)):
        if _kernel32.GetLastError() != 1410:
            return False
    _ui_class_ready = True
    return True


def _ui_worker(hinst) -> None:
    """面板线程：建窗 → 做毛玻璃 → 跑自己的消息循环，直到定时器或点击关掉它。"""
    sw = _user32.GetSystemMetrics(0)
    sh = _user32.GetSystemMetrics(1)
    x = (sw - WIN_W) // 2
    y = int(sh * 0.10) - GLASS_PAD

    # 先建一个不显示的窗口：毛玻璃要抓"窗口背后"的画面，必须先抓后显示
    hwnd = _user32.CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        UI_CLASS, "校园网登录", WS_POPUP,
        x, y, WIN_W, WIN_H, None, None, hinst, None)

    if not hwnd:
        log("面板创建失败，本次不显示界面（日志里仍有完整记录）")
        _ui["closed"].set()
        return

    _ui["hwnd"] = hwnd

    # 毛玻璃背景：抓屏 → 模糊 → 加暗。失败就退回系统材质/纯色实底。
    mode = "纯色实底"
    try:
        base = _build_glass_base(x, y, WIN_W, WIN_H, GLASS_PAD,
                                 CARD_W, CARD_H, CORNER_R)
        bmp, dc = _make_dib(base)
        if dc:
            _ui["base_bmp"], _ui["base_dc"] = bmp, dc
            mode = "自制毛玻璃"
        else:
            raise RuntimeError("CreateDIBSection 失败")
    except Exception as e:
        log("自制毛玻璃失败（%r），改试系统材质" % (e,))
        _ui["opaque"] = not _apply_acrylic(hwnd)
        mode = "系统材质" if not _ui["opaque"] else "纯色实底"

    if not _apply_round_corners(hwnd, WIN_W, WIN_H):
        log("圆角设置失败，退化为直角")
    log("面板已显示（材质: %s，缩放 %.2fx）" % (mode, UI_SCALE))

    _user32.ShowWindow(hwnd, 4)               # SW_SHOWNOACTIVATE，不抢焦点
    _user32.SetTimer(hwnd, 1, 55, None)       # 动画 + 到点自动关窗

    msg = wintypes.MSG()
    while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        _user32.TranslateMessage(ctypes.byref(msg))
        _user32.DispatchMessageW(ctypes.byref(msg))

    if _ui["base_bmp"]:
        _gdi32.DeleteObject(_ui["base_bmp"])
        _ui["base_bmp"] = None
    if _ui["base_dc"]:
        _gdi32.DeleteDC(_ui["base_dc"])
        _ui["base_dc"] = None
    _ui["hwnd"] = None
    _ui["closed"].set()

def ui_open() -> None:
    """点下去的那一刻就开出面板。**全程只有这一块面板**，从头用到尾。"""
    global _ui_thread
    with _ui_lock:
        if _ui["hwnd"] or _ui_thread is not None:
            return
        _ui["closed"].clear()
    try:
        hinst = _kernel32.GetModuleHandleW(None)
        if not _ensure_ui_class(hinst):
            log("窗口类注册失败，跳过界面")
            _ui["closed"].set()
            return
        _ui_thread = threading.Thread(target=_ui_worker, args=(hinst,), daemon=True)
        _ui_thread.start()
    except Exception as e:
        log("面板启动异常: %r" % e)
        _ui["closed"].set()


def _ui_refresh() -> None:
    """让面板重画。跨线程调 InvalidateRect 是安全的：消息会投到窗口所属线程。"""
    hwnd = _ui.get("hwnd")
    if hwnd:
        try:
            _user32.InvalidateRect(hwnd, None, False)
        except Exception:
            pass


def ui_set(title: str, body: str, kind: str = "work") -> None:
    """**就地**更新面板文字 —— 不新建窗口，所以画面是连续的，不会闪。"""
    _ui["title"], _ui["body"], _ui["kind"] = title, body, kind
    _ui["confirm_until"] = 0.0
    _ui["anim"] = 0
    _ui_refresh()


def ui_finish(title: str, body: str, ok: bool = True) -> None:
    """出结果：立刻显示，并至少停留 HOLD_OK / HOLD_ERR 毫秒后自动关闭。"""
    _ui["title"], _ui["body"] = title, body
    _ui["kind"] = "ok" if ok else "err"
    _ui["hold_until"] = time.time() + (HOLD_OK if ok else HOLD_ERR) / 1000.0
    _ui_refresh()


def ui_wait(timeout: float = 20.0) -> None:
    """退出前等面板自己关掉 —— 少了这一步，最后的结果会一闪而过。"""
    try:
        _ui["closed"].wait(timeout)
    except Exception:
        pass


def fallback_messagebox(title: str, body: str, error: bool = False) -> None:
    """兜底提示：原生 MessageBox"""
    flags = 0x00000010 if error else 0x00000040      # ICON_ERROR / ICON_INFORMATION
    _user32.MessageBoxW(None, body, title, flags)


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
def log(text: str) -> None:
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_SIZE:
            os.remove(LOG_FILE)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


# --------------------------------------------------------------------------
# 登录流程
# --------------------------------------------------------------------------
PORTAL_WAIT = 25        # 连上 Wi-Fi 后，等认证服务器就绪的最长秒数
# 校园网在不同区域是不同的 SSID：宿舍楼是 6#SS-WiFi，教学楼是 FYLG-WIFI。
# 只认一个名字的话，换个楼就会"扫不到"——2026-11-23 教学楼实测踩到。
# 换学校 / 学校改名：往这里加一项就行，程序会自动挑附近可见的那个。
CAMPUS_SSIDS = ("6#SS-WiFi", "FYLG-WIFI")
WIFI_SSID = CAMPUS_SSIDS[0]   # 兼容旧代码与文档里的默认名
WIFI_IFACE = "WLAN"      # 无线网卡名称


def get_wlan_state():
    """读无线网卡的真实状态 → (状态文字, 当前 SSID)。

    只看物理网卡那一段：`netsh wlan show interfaces` 在开了移动热点 / Wi-Fi Direct
    时会多列出几个虚拟接口，不加区分地取第一个 SSID 会读到别的东西。
    """
    try:
        out = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                             capture_output=True).stdout.decode("gbk", "replace")
    except Exception:
        return None, None

    blocks, cur = [], []
    for line in out.splitlines():
        if line.strip() and not line.startswith(" "):     # 顶格的是标题行
            if cur:
                blocks.append(cur)
            cur = [line]
        elif cur:
            cur.append(line)
    if cur:
        blocks.append(cur)

    for blk in blocks:
        fields = {}
        for line in blk:
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            fields.setdefault(k.strip(), v.strip())
        name = fields.get("名称") or fields.get("Name")
        if name and name != WIFI_IFACE:
            continue                                      # 不是物理网卡，跳过
        state = fields.get("状态") or fields.get("State")
        return state, (fields.get("SSID") or None)
    return None, None


def get_current_ssid():
    """当前连着的 Wi-Fi 名称；没连返回 None。只读查询。"""
    return get_wlan_state()[1]


def scan_networks(wait: float = 1.5):
    """列出当前可见的 SSID（诊断用）。

    注意这只是"上一次扫描的缓存" —— 判断能不能连必须用 wlan_scan_target()，
    它才会真的点名扫描并等到结果回来。
    """
    def _read():
        try:
            return subprocess.run(["netsh", "wlan", "show", "networks"],
                                  capture_output=True).stdout.decode("gbk", "replace")
        except Exception:
            return ""

    _read()
    time.sleep(wait)
    out = _read()

    found = []
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("SSID") and not s.startswith("BSSID"):
            name = s.split(":", 1)[-1].strip()
            if name:
                found.append(name)
    return found


def is_campus_ssid(ssid) -> bool:
    """这个 SSID 是不是校园网（任一校区/任一楼栋）。大小写不敏感。"""
    if not ssid:
        return False
    s = ssid.strip().lower()
    return any(s == want.strip().lower() for want in CAMPUS_SSIDS)


def pick_campus_target(wait: float = 1.5):
    """在附近挑一个校园网，返回它的真实 SSID；一个都没有返回 None。

    先读系统扫描缓存（快，1 秒级）；缓存里没有才逐个点名扫描，
    给慢驱动留时间 —— 但只对列表里的名字扫，不会瞎扫。
    """
    visible = scan_networks(wait)
    by_lower = {}
    for v in visible:
        by_lower.setdefault(v.strip().lower(), v)
    for want in CAMPUS_SSIDS:
        real = by_lower.get(want.strip().lower())
        if real:
            return real
    for want in CAMPUS_SSIDS:
        try:
            ok, _q = wlan_scan_target(want, wait=3.0)
        except Exception:
            ok = False
        if ok:
            return want
    return None


def profile_exists(ssid: str) -> bool:
    """系统里是否存着这个 SSID 的无线配置。

    直接读配置文件原文，不走 netsh —— netsh 遇到名字里带 # 的会解析出错。
    """
    base = r"C:\ProgramData\Microsoft\Wlansvc\Profiles\Interfaces"
    try:
        for iface in os.listdir(base):
            d = os.path.join(base, iface)
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                try:
                    c = open(os.path.join(d, f), "rb").read().decode("utf-8", "replace")
                except Exception:
                    continue
                blk = re.search(r"<SSID\b[^>]*>(.*?)</SSID>", c, re.S)
                if not blk:
                    continue
                nm = re.search(r"<name>(.*?)</name>", blk.group(1))
                if nm and nm.group(1).strip() == ssid:
                    return True
    except Exception:
        pass
    return False


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class _DOT11_SSID(ctypes.Structure):
    _fields_ = [("uSSIDLength", ctypes.c_ulong),
                ("ucSSID", ctypes.c_ubyte * 32)]


class _WLAN_CONNECTION_PARAMETERS(ctypes.Structure):
    _fields_ = [("wlanConnectionMode", ctypes.c_int),
                ("strProfile", wintypes.LPCWSTR),
                ("pDot11Ssid", ctypes.POINTER(_DOT11_SSID)),
                ("pDesiredBssidList", ctypes.c_void_p),
                ("dot11BssType", ctypes.c_int),
                ("dwFlags", wintypes.DWORD)]


class _WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [("InterfaceGuid", _GUID),
                ("strInterfaceDescription", wintypes.WCHAR * 256),
                ("isState", ctypes.c_int)]


class _WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [("dwNumberOfItems", wintypes.DWORD),
                ("dwIndex", wintypes.DWORD),
                ("InterfaceInfo", _WLAN_INTERFACE_INFO * 1)]


class _WLAN_AVAILABLE_NETWORK(ctypes.Structure):
    _fields_ = [("strProfileName", wintypes.WCHAR * 256),
                ("dot11Ssid", _DOT11_SSID),
                ("dot11BssType", ctypes.c_uint),
                ("uNumberOfBssids", ctypes.c_ulong),
                ("bNetworkConnectable", wintypes.BOOL),
                ("wlanNotConnectableReason", ctypes.c_ulong),
                ("uNumberOfPhyTypes", ctypes.c_ulong),
                ("dot11PhyTypes", ctypes.c_uint * 8),
                ("bMorePhyTypes", wintypes.BOOL),
                ("wlanSignalQuality", ctypes.c_ulong),
                ("bSecurityEnabled", wintypes.BOOL),
                ("dot11DefaultAuthAlgorithm", ctypes.c_uint),
                ("dot11DefaultCipherAlgorithm", ctypes.c_uint),
                ("dwFlags", ctypes.c_ulong),
                ("dwReserved", ctypes.c_ulong)]


class _WLAN_AVAILABLE_NETWORK_LIST(ctypes.Structure):
    # Network 声明成长度 1 的数组，靠下标越界访问后续元素（Win32 常见做法）
    _fields_ = [("dwNumberOfItems", ctypes.c_ulong),
                ("dwIndex", ctypes.c_ulong),
                ("Network", _WLAN_AVAILABLE_NETWORK * 1)]


def _wlan_lib():
    """统一声明 wlanapi 的函数签名。64 位下不声明会把指针当 int 传。"""
    wlan = ctypes.windll.wlanapi
    wlan.WlanOpenHandle.restype = wintypes.DWORD
    wlan.WlanOpenHandle.argtypes = [wintypes.DWORD, ctypes.c_void_p,
                                    ctypes.POINTER(wintypes.DWORD),
                                    ctypes.POINTER(wintypes.HANDLE)]
    wlan.WlanEnumInterfaces.restype = wintypes.DWORD
    wlan.WlanEnumInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                        ctypes.c_void_p]
    wlan.WlanScan.restype = wintypes.DWORD
    wlan.WlanScan.argtypes = [wintypes.HANDLE, ctypes.POINTER(_GUID),
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    wlan.WlanGetAvailableNetworkList.restype = wintypes.DWORD
    wlan.WlanGetAvailableNetworkList.argtypes = [wintypes.HANDLE,
                                                 ctypes.POINTER(_GUID),
                                                 wintypes.DWORD,
                                                 ctypes.c_void_p, ctypes.c_void_p]
    wlan.WlanConnect.restype = wintypes.DWORD
    wlan.WlanConnect.argtypes = [wintypes.HANDLE, ctypes.POINTER(_GUID),
                                 ctypes.POINTER(_WLAN_CONNECTION_PARAMETERS),
                                 ctypes.c_void_p]
    wlan.WlanDisconnect.restype = wintypes.DWORD
    wlan.WlanDisconnect.argtypes = [wintypes.HANDLE, ctypes.POINTER(_GUID),
                                    ctypes.c_void_p]
    wlan.WlanFreeMemory.argtypes = [ctypes.c_void_p]
    wlan.WlanCloseHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    return wlan


def _interface_guid(wlan, handle):
    """取物理无线网卡的 GUID（不是 Wi-Fi Direct 虚拟网卡的）。"""
    plist = ctypes.c_void_p()
    if wlan.WlanEnumInterfaces(handle, None, ctypes.byref(plist)) != 0 or not plist:
        return None, None
    lst = ctypes.cast(plist, ctypes.POINTER(_WLAN_INTERFACE_INFO_LIST)).contents
    if lst.dwNumberOfItems == 0:
        wlan.WlanFreeMemory(plist)
        return None, None
    return lst.InterfaceInfo[0].InterfaceGuid, plist


def wlan_scan_target(ssid: str, wait: float = 4.0):
    """对指定 SSID 做一次**点名扫描**，并等它真的出现在可用网络列表里。

    这是「特定的网络不可用 / RSSI 255」的正解：
    `netsh wlan show networks` 读到的往往只是上一次扫描的缓存，
    驱动手里没有这个 SSID 的新鲜 BSS 记录时，关联会**当场失败**。
    用 WlanScan 显式点名扫描，再轮询 WlanGetAvailableNetworkList
    直到信号质量出现有效值（255 是无效值）。

    返回 (是否可见, 信号质量百分比)；不可见时为 (False, -1)。
    """
    try:
        wlan = _wlan_lib()
    except Exception as e:
        log("定向扫描: 加载 wlanapi 失败 %r" % (e,))
        return False, -1

    handle = wintypes.HANDLE()
    negotiated = wintypes.DWORD()
    if wlan.WlanOpenHandle(2, None, ctypes.byref(negotiated),
                           ctypes.byref(handle)) != 0:
        return False, -1

    plist = None
    try:
        guid, plist = _interface_guid(wlan, handle)
        if guid is None:
            return False, -1

        raw = ssid.encode("utf-8")[:32]
        want = _DOT11_SSID()
        want.uSSIDLength = len(raw)
        for i, b in enumerate(raw):
            want.ucSSID[i] = b

        rc = wlan.WlanScan(handle, ctypes.byref(guid), ctypes.byref(want),
                           None, None)
        if rc != 0:
            # 170 = ERROR_BUSY：上一次扫描还没结束。等一下再点一次名，
            # 别因为"忙"就判定"这个网络不在范围内"。
            log("定向扫描 %s: WlanScan rc=%d，稍后重试" % (ssid, rc))
            time.sleep(0.9)
            rc = wlan.WlanScan(handle, ctypes.byref(guid), ctypes.byref(want),
                               None, None)
        log("定向扫描 %s: WlanScan rc=%d" % (ssid, rc))

        deadline = time.time() + wait
        best = -1
        while time.time() < deadline:
            avail = ctypes.c_void_p()
            rc = wlan.WlanGetAvailableNetworkList(
                handle, ctypes.byref(guid), 0x00000003, None, ctypes.byref(avail))
            if rc == 0 and avail:
                try:
                    arr = ctypes.cast(
                        avail, ctypes.POINTER(_WLAN_AVAILABLE_NETWORK_LIST)).contents
                    # Network 在结构体里声明成长度 1 的数组，ctypes 会做越界检查，
                    # 直接 arr.Network[1] 会 IndexError —— 必须转成指针再按索引取
                    addr = (ctypes.addressof(arr)
                            + _WLAN_AVAILABLE_NETWORK_LIST.Network.offset)
                    items = ctypes.cast(
                        addr, ctypes.POINTER(_WLAN_AVAILABLE_NETWORK))
                    for i in range(arr.dwNumberOfItems):
                        net = items[i]
                        n = net.dot11Ssid.uSSIDLength
                        if n <= 0 or n > 32:
                            continue
                        name = bytes(net.dot11Ssid.ucSSID[:n]).decode(
                            "utf-8", "replace")
                        if name == ssid:
                            best = max(best, int(net.wlanSignalQuality))
                finally:
                    wlan.WlanFreeMemory(avail)
            if best > 0:
                return True, best
            time.sleep(0.4)
        return False, best
    except Exception as e:
        log("定向扫描异常: %r" % (e,))
        return False, -1
    finally:
        if plist:
            wlan.WlanFreeMemory(plist)
        wlan.WlanCloseHandle(handle, None)


WLAN_XML_TEMPLATE = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
\t<name>{ssid}</name>
\t<SSIDConfig>
\t\t<SSID>
\t\t\t<hex>{hexs}</hex>
\t\t\t<name>{ssid}</name>
\t\t</SSID>
\t</SSIDConfig>
\t<connectionType>ESS</connectionType>
\t<connectionMode>manual</connectionMode>
\t<MSM>
\t\t<security>
\t\t\t<authEncryption>
\t\t\t\t<authentication>open</authentication>
\t\t\t\t<encryption>none</encryption>
\t\t\t\t<useOneX>false</useOneX>
\t\t\t</authEncryption>
\t\t</security>
\t</MSM>
\t<MacRandomization xmlns="http://www.microsoft.com/networking/WLAN/profile/v3">
\t\t<enableRandomization>false</enableRandomization>
\t</MacRandomization>
</WLANProfile>
"""


def ensure_profile(ssid: str) -> bool:
    """确保系统里有这个 SSID 的无线配置。

    **只在缺失时新建，绝不动已有配置。**

    2026-11-23 踩过的坑：为了给校园网写"不用随机 MAC"，我改成了每次覆盖重建，
    结果切回用户的手机热点时，程序把 `手机热点`（有密码的）又当成新网络加了一遍，
    Windows 生成了一条**空密码的同名配置 `手机热点 2`**，之后系统连接时
    优先匹配到那条冒牌配置，热点就再也连不上了 —— 看似"把原来的网屏蔽了"。

    教训：任何"重建配置"的动作都可能毁掉别人的密码。已有配置一律原样保留。

    （附：随机 MAC 不是连不上的原因。08:23:56 那次关联用的就是真实 MAC
    94:BB:43:EA:42:02，照样在同一秒失败。配置里带 enableRandomization=false
    只是为了让校园网 MAC 稳定，方便门户按 IP/MAC 记账，别当成修复。）
    """
    if profile_exists(ssid):
        return True
    path = os.path.join(APP_DIR, "_wlan_tmp.xml")
    try:
        xml = WLAN_XML_TEMPLATE.format(ssid=ssid,
                                       hexs=ssid.encode("utf-8").hex().upper())
        with open(path, "w", encoding="utf-8") as f:
            f.write(xml)
        r = subprocess.run(["netsh", "wlan", "add", "profile",
                            "filename=%s" % path, "user=all"], capture_output=True)
        out = (r.stdout + r.stderr).decode("gbk", "replace").strip().replace("\n", " ")
        log("补建无线配置: 返回码=%d 输出=%s" % (r.returncode, out[:160]))
        # 返回码 0 但输出里带"添加到接口 xxx 2"= 撞名生成了副本，说明判断有误，
        # 立刻把这条副本删掉，别让它污染系统（原配置始终不碰）。
        if re.search(r"添加到接口\s+\S+\s*2\b", out) or " 2\n" in out:
            log("检测到撞名副本，立即清理: %s" % ssid)
            subprocess.run(["netsh", "wlan", "delete", "profile",
                            "name=%s 2" % ssid, "user=all"], capture_output=True)
    except Exception as e:
        log("补建无线配置异常: %r" % (e,))
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    return profile_exists(ssid)


def connect_wifi_api(ssid: str):
    """用 Windows 的 WlanConnect 接口发起连接 —— 资源管理器点"连接"走的就是它。

    netsh 那条路在 SSID 含特殊字符（比如 #）时会失败，这里绕开命令行解析。
    **注意：返回 0 只代表请求被受理，不代表连上了。**
    调用方必须再用 wait_for_association() 回来问系统真实结果。
    返回 (是否成功受理, 说明文字)。
    """
    try:
        wlan = _wlan_lib()
    except Exception as e:
        return False, "加载 wlanapi 失败: %r" % (e,)

    handle = wintypes.HANDLE()
    negotiated = wintypes.DWORD()
    rc = wlan.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle))
    if rc != 0:
        return False, "WlanOpenHandle 失败 rc=%d" % rc

    plist = None
    try:
        guid, plist = _interface_guid(wlan, handle)
        if guid is None:
            return False, "系统里没有可用的无线网卡接口"

        raw = ssid.encode("utf-8")[:32]
        dssid = _DOT11_SSID()
        dssid.uSSIDLength = len(raw)
        for i, b in enumerate(raw):
            dssid.ucSSID[i] = b

        params = _WLAN_CONNECTION_PARAMETERS()
        params.pDesiredBssidList = None
        params.dot11BssType = 1                # infrastructure
        params.dwFlags = 0

        if profile_exists(ssid):
            # 系统里存着配置：用它。注意 profile 模式下 pDot11Ssid 必须为 NULL，
            # 否则 WlanConnect 会返回 87（参数无效）。
            params.wlanConnectionMode = 0      # wlan_connection_mode_profile
            params.strProfile = ssid
            params.pDot11Ssid = None
            mode = "profile"
        else:
            # 系统里没有配置（校园网是开放网络，可以不用配置直接连）。
            # 枚举值必须查头文件：profile=0, temporary_profile=1,
            # discovery_secure=2, discovery_unsecure=3, auto=4, invalid=5。
            params.wlanConnectionMode = 3      # wlan_connection_mode_discovery_unsecure
            params.strProfile = None
            params.pDot11Ssid = ctypes.pointer(dssid)
            mode = "discovery_unsecure"

        rc = wlan.WlanConnect(handle, ctypes.byref(guid), ctypes.byref(params), None)
        if rc == 0:
            return True, "%s 模式连接成功" % mode
        return False, "%s 模式返回 rc=%d" % (mode, rc)
    except Exception as e:
        return False, "调用异常: %r" % (e,)
    finally:
        if plist:
            wlan.WlanFreeMemory(plist)
        wlan.WlanCloseHandle(handle, None)


def wlan_disconnect():
    """主动断开当前无线连接。

    用来对付"WlanConnect 受理了、但系统一直挂在旧网络上不松手"——
    2026-10-23、2026-09-18 两次实测都卡在这一步：目标是校园网、
    信号 100%，WlanConnect 返回 0，可 12 秒后 SSID 还是旧热点。
    这时先断开，让它把手松开，再连一次。
    """
    try:
        wlan = _wlan_lib()
    except Exception as e:
        return False, "加载 wlanapi 失败: %r" % (e,)

    handle = wintypes.HANDLE()
    negotiated = wintypes.DWORD()
    rc = wlan.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle))
    if rc != 0:
        return False, "WlanOpenHandle rc=%d" % rc

    plist = None
    try:
        guid, plist = _interface_guid(wlan, handle)
        if guid is None:
            return False, "没有可用的无线接口"
        rc = wlan.WlanDisconnect(handle, ctypes.byref(guid), None)
        return rc == 0, "WlanDisconnect rc=%d" % rc
    except Exception as e:
        return False, "调用异常: %r" % (e,)
    finally:
        if plist:
            wlan.WlanFreeMemory(plist)
        wlan.WlanCloseHandle(handle, None)


def wait_for_association(ssid: str, timeout: float = 12.0) -> bool:
    """等系统**真正**关联上目标网络。

    WlanConnect 返回 0 只说明请求被受理。2026-10-08 实测出现过
    "请求被受理、同一秒关联就失败"（事件日志 ID 8002：特定的网络不可用 / RSSI 255），
    而当时的代码看不出来，只会一路傻等到超时。
    所以这里必须回来问系统：状态是不是"已连接"、SSID 是不是目标。
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        state, cur = get_wlan_state()
        last = (state, cur)
        if cur == ssid and state and ("已连接" in state or "connected" in state.lower()):
            return True
        time.sleep(0.5)
    log("等待关联超时，最后状态=%s" % (last,))
    return False


def connect_wifi(ssid: str, iface: str = WIFI_IFACE, attempts: int = 2,
                 scan_wait: float = 4.0, assoc_wait: float = 12.0) -> bool:
    """切换到指定 Wi-Fi 并**确认真的连上了**。会断一下网，属正常现象。

    顺序不能变：点名扫描 → 确认目标可见 → WlanConnect → 问系统关联结果。
    少了最后一步就会"脚本以为成功、其实没连上"。
    最多试 2 次（间隔 1.5 秒），不再无限折腾网卡。
    """
    if not ssid:
        return False

    ensure_profile(ssid)                       # 配置丢了先补一份

    for n in range(1, attempts + 1):
        if n > 1:
            # 上一轮"受理了但没切过去"：先断开当前连接，逼它松手
            ok_d, why_d = wlan_disconnect()
            log("先断开当前连接：%s" % why_d)
            time.sleep(1.5)

        visible, quality = wlan_scan_target(ssid, wait=scan_wait)
        log("第 %d/%d 次：点名扫描 %s → 可见=%s 信号=%s"
            % (n, attempts, ssid, visible,
               ("%d%%" % quality) if quality >= 0 else "未知"))
        if not visible:
            time.sleep(1.0)
            continue

        ok, why = connect_wifi_api(ssid)
        log("WlanConnect(%s): %s" % (ssid, why))
        if ok and wait_for_association(ssid, assoc_wait):
            log("已确认关联上 %s" % ssid)
            return True
        log("第 %d/%d 次没连上，稍后重试" % (n, attempts))
        time.sleep(1.5)

    log("尝试 %d 次仍未连上 %s" % (attempts, ssid))
    return False


# 拿真实出网来验证"到底通没通"的探测点。
# 2026-11-08 实测（各 4 次）：msftconnecttest 最慢且有一次 8.4 秒，
# 已剔除；剩下四个国产/国际端点都是 0.1~3 秒。多个点是为了防止
# 某一家服务调整，全挂的概率极低。
# 第二个字段是期望内容；None 表示"只要返回 204 就算过"。
VERIFY_TARGETS = (
    ("http://connect.rom.miui.com/generate_204", None),
    ("http://wifi.vivo.com.cn/generate_204", None),
    ("http://connectivitycheck.platform.hicloud.com/generate_204", None),
    ("http://captive.apple.com/hotspot-detect.html", b"Success"),
)
VERIFY_TIMEOUT = 4.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止跟随跳转。

    认证生效时这些地址会直接返回内容；没生效时校园网会把请求 302 到门户。
    如果放任 urllib 跟着跳，最后拿到的是门户页面的 200，会被误判成"通"。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_VERIFY_OPENER = urllib.request.build_opener(_NoRedirect)


def _probe_one(url: str, expect, timeout: float) -> bool:
    """单点探测。拿到 204 或预期内容 -> True；被跳转/超时 -> False。"""
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Cache-Control", "no-cache")
        with _VERIFY_OPENER.open(req, timeout=timeout) as resp:
            if resp.status == 204:
                return True
            if expect is None:
                return False
            return expect in resp.read(256)
    except Exception:
        return False


def internet_ok(timeout: float = VERIFY_TIMEOUT, rounds: int = 2) -> bool:
    """真的能出网吗？

    为什么不能只看门户的返回：门户是**按 IP 记账**的。它的会话表里可能
    还留着这台电脑的记录，但网关那边早就不放行了（会话被判失效、被顶号、
    或长时间没流量）。这种"僵尸会话"状态下，服务器照样回「已经在线」，
    甚至直接给 result=1 —— 可实际上一点网都没有。

    2026-11-08 18:14 实测：服务器回「IP: 172.17.x.x 已经在线！」，
    紧接着 Windows 连续 9 次报「检测到热点」，打开网页就是门户页面。

    所以唯一可靠的办法是**访问公网地址，看它有没有被劫持**。

    几个点**同时**发（不是一个个排队）：谁先通就算通。串行测法最坏要
    等 4 个点依次超时，实测最快 222ms、最慢 5733ms；并行后耗时约等于
    最快那个点，判定也更不容易被单点抖动带偏。
    两轮都全挂才判不通 —— 单次抖动不该触发重登。
    """
    for r in range(max(1, rounds)):
        hit = threading.Event()
        winner = []

        def _worker(url, expect):
            if hit.is_set():
                return
            if _probe_one(url, expect, timeout):
                winner.append(url)
                hit.set()

        for url, expect in VERIFY_TARGETS:
            threading.Thread(target=_worker, args=(url, expect), daemon=True).start()

        hit.wait(timeout + 0.6)
        if winner:
            return True
        if r + 1 < max(1, rounds):
            time.sleep(0.6)
    return False


def probe_portal(timeout: float = 1.5) -> bool:
    """认证服务器是否可达。只做 TCP 探测，不发送任何认证请求。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((PORTAL_HOST, 80))
        return True
    except Exception:
        return False
    finally:
        s.close()


def wait_for_portal(max_wait: int = PORTAL_WAIT, interval: float = 0.8) -> bool:
    """轮询等待接入校园网。

    切 Wi-Fi 期间会短暂断网，所以这段逻辑必须完全跑在本地、不依赖任何云端连接，
    结果只写本地日志，等用户恢复网络后再复盘。
    轮询间隔取 0.8 秒：DHCP 一拿到地址就能立刻往下走，实测比 2 秒快 1 秒以上。
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if probe_portal():
            return True
        time.sleep(interval)
    return False


def get_local_ip() -> str:
    """获取本机在校园网中的出口 IP（即认证服务器看到的源地址）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((PORTAL_HOST, 80))
        return s.getsockname()[0]
    finally:
        s.close()


def request_login(account: str, password: str, isp: str) -> str:
    params = {
        "callback": "dr%d" % random.randint(1000, 9999),
        "login_method": "1",
        "user_account": account + isp,
        "user_password": password,
        "wlan_user_ip": get_local_ip(),
        "wlan_user_ipv6": "",
        "wlan_user_mac": "000000000000",
        "wlan_ac_ip": "",
        "wlan_ac_name": "",
        "jsVersion": JS_VERSION,
        "terminal_type": "1",
        "lang": "zh-cn",
        "v": str(random.randint(1000, 9999)),
    }
    url = "http://%s:%d%s?%s" % (PORTAL_HOST, PORTAL_PORT, LOGIN_PATH,
                                 urllib.parse.urlencode(params))
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Referer", "http://%s/a79.htm" % PORTAL_HOST)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def request_logout(ip: str = None) -> str:
    """向门户注销**本机 IP** 的会话。

    门户的注销是"按 IP"的：凭据是固定的 drcom/123，服务器靠 wlan_user_ip
    找到对应会话。所以它只能注销自己这台机器，注销不了别的设备，
    也不需要（更不会用到）你的账号密码。

    参数集是从门户前端 a40.js 的 logout.portal_logout() 里抄的，不是猜的。
    """
    ip = ip or get_local_ip()
    params = {
        "callback": "dr%d" % random.randint(1000, 9999),
        "login_method": "1",
        "user_account": "drcom",
        "user_password": "123",
        "ac_logout": "0",
        "register_mode": "1",
        "wlan_user_ip": ip,
        "wlan_user_ipv6": "",
        "wlan_vlan_id": "0",
        "wlan_user_mac": "000000000000",
        "wlan_ac_ip": "",
        "wlan_ac_name": "",
        "jsVersion": JS_VERSION,
        "lang": "zh-cn",
        "v": str(random.randint(1000, 9999)),
    }
    url = "http://%s:%d%s?%s" % (PORTAL_HOST, PORTAL_PORT, LOGOUT_PATH,
                                 urllib.parse.urlencode(params))
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Referer", "http://%s/a79.htm" % PORTAL_HOST)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def judge_logout(raw: str):
    """解析注销响应。判定比登录宽松 —— 已经不在线也算达成目的。"""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return False, "服务器返回内容无法识别"
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return False, "服务器返回内容无法识别"

    result = obj.get("result")
    msg = (obj.get("msg") or "").strip()

    if result in (1, "1", "ok", True):
        return True, msg or "已下线，名额已释放"
    if any(k in msg for k in ("注销成功", "下线成功", "已注销", "不在线", "无此用户")):
        return True, msg
    return False, msg or "注销失败"


def _logout_run() -> int:
    """手动下线：释放这台电脑占着的校园网会话。"""
    if not acquire_single_instance():
        log("已有实例正在运行，本次下线操作忽略")
        return 0

    log("-" * 46)
    log("手动下线：开始")

    if not probe_portal():
        log("不在校园网，没有可释放的会话")
        ui_open()
        ui_set("不在校园网", "当前没有连校园网，不需要下线")
        ui_finish("无需下线", "现在没连校园网\n会话应该已经释放了", ok=True)
        return 0

    ip = get_local_ip()
    log("本机出口 IP=%s，开始注销" % ip)
    ui_open()
    ui_set("正在下线", "正在释放这台电脑的校园网会话…")

    t0 = time.time()
    try:
        raw = request_logout(ip)
    except socket.timeout:
        log("注销请求超时（%.1fs）" % (time.time() - t0))
        ui_finish("下线失败", "认证服务器没响应\n请稍后再试一次", ok=False)
        return 3
    except Exception as e:
        log("注销请求异常: %r" % e)
        ui_finish("下线失败", "连不上认证服务器\n请稍后再试一次", ok=False)
        return 3

    log("注销响应（%.1fs）: %s" % (time.time() - t0, raw.strip()[:300]))
    ok, text = judge_logout(raw)
    log(("注销成功: " if ok else "注销失败: ") + text)

    if ok:
        # 告诉守护进程：这次下线是本人主动做的，30 分钟内别再登回来。
        # 不写这个标记的话，守护会在下一轮探测发现"没网"，然后立刻重新认证，
        # 用户想到手机上登录还是登不上 —— 等于白点了。
        try:
            with open(GUARD_PAUSE_FILE, "w", encoding="utf-8") as f:
                f.write(str(int(time.time()) + GUARD_PAUSE_SECONDS))
            log("已通知守护进程暂停 %d 分钟" % (GUARD_PAUSE_SECONDS // 60))
        except Exception as e:
            log("写守护暂停标记失败: %r" % e)

    ui_finish("已下线" if ok else "下线失败", text, ok=ok)
    return 0 if ok else 1


RAD_HINTS = [
    # 实测：输错密码时门户给的是中文「密码错误」；而正确密码会得到这个英文码。
    # 所以它**不是**密码错，而是"密码通过之后"被账号状态挡下（过期/冻结/绑定冲突）。
    ("Passwd_Err", "账号状态被拒绝（密码通常是没问题的）\n可能是密码过期、账号被冻结，或设备绑定冲突\n→ 先改一次密码；不行就联系网络中心"),
    ("Limit Users", "这个账号的在线数已满\n→ 先在别的设备上点「校园网下线」再试"),
    ("系统忙", "学校的认证服务器现在繁忙\n不是你账号或密码的问题\n→ 过几分钟再点我一次；一直这样就是学校那边的事"),
    ("User Not Found", "服务器里没有这个账号\n→ 检查学号填得对不对"),
    ("User Expire", "账号已过期\n→ 联系网络中心续期"),
    ("Frozen", "账号被冻结\n→ 联系网络中心"),
    ("Restriction", "同一账号失败次数太多，被暂时限制了\n→ 等 15~30 分钟再试"),
]


def is_busy(raw: str) -> bool:
    """服务器是不是"自己忙不过来"（而不是拒了这个账号）。"""
    return any(k in raw for k in BUSY_HINTS)


def retryable_raw(raw: str) -> bool:
    """值不值得等一会儿再来：忙 / 在线数超限 / 没拿到响应。

    密码过期、绑定异常这类账号侧拒绝**不在**这里 —— 重试只会让人干等。
    """
    return is_busy(raw) or ("Limit Users" in raw)


def explain_fail(msg: str) -> str:
    """把 RADIUS 错误码翻成人话。

    依据 Dr.COM 官方说明：提示"账号或密码错误"的第二类原因是"账号欠费或其他异常"，
    所以不能一口咬定是密码打错 —— 把可能性一起列出来，用户才知道下一步该干什么。
    """
    low = (msg or "").lower()
    for key, hint in RAD_HINTS:
        if key.lower() in low:
            return hint + "\n（服务器原话：" + msg + "）"
    return msg or "认证失败，请稍后重试"


def judge(raw: str):
    """解析 JSONP 响应，返回 (是否成功, 提示文字, 是否还需验证).

    第三项 是 2026-11-08 加的，起因是一个真实故障：
    门户按 IP 记账，"IP ... 已经在线" 这句话**不代表真的能上网**。
    旧会话可能已经是僵尸（服务端记着、网关不放行），此时它照样回这句。
    老代码把"在线"直接当成功，于是出现了"显示登录成功、实际没网"。

    所以现在遇到"在线"不直接下结论，而是标成待验证，由调用方用
    internet_ok() 探一次真实出网再定。
    """
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return False, "服务器返回内容无法识别", False
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return False, "服务器返回内容无法识别", False

    result = obj.get("result")
    msg = (obj.get("msg") or "").strip()

    if result in (1, "1", "ok", True):
        return True, msg or "认证成功，可以上网了", False
    if "在线" in msg and "限制" not in msg:
        # 待验证：可能是真在线，也可能是僵尸会话
        return True, msg or "当前设备已经在线", True
    return False, explain_fail(msg), False


_INSTANCE_MUTEX = None


def acquire_single_instance() -> bool:
    """同一时间只允许一个实例 —— 连点多次会让几个进程抢同一张网卡，
    互相把对方的连接请求打断。"""
    global _INSTANCE_MUTEX
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        _INSTANCE_MUTEX = k32.CreateMutexW(None, False, "CampusAutoLogin_SingleInstance")
        if not _INSTANCE_MUTEX:
            return True
        return k32.GetLastError() != 183        # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def release_single_instance() -> None:
    """把单实例锁交出去。

    为什么必须有这个函数（2026-11-24 真实故障）：

        net_guard.py 会 import 本模块，并在 do_repair() 里调用
        acquire_single_instance() 来实现「用户手动登录时守护让开」。
        但守护进程是常驻的 —— 它抢到的句柄只要不关就一直在。

        于是 2026-11-24 11:01:18 网络断了一次，守护修复时抢走了锁，
        修完（11:01:35）没有释放；此后用户每次双击桌面「校园网一键登录」，
        CreateMutexW 都返回 ERROR_ALREADY_EXISTS(183)，
        程序判定"已有实例正在运行"就静默退出 —— 面板再也不弹。

    所以：谁 acquire，谁就必须在结束时 release。
    """
    global _INSTANCE_MUTEX
    try:
        if _INSTANCE_MUTEX:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(_INSTANCE_MUTEX))
    except Exception:
        pass
    _INSTANCE_MUTEX = None


def acquire_single_instance_wait(seconds: float = 8.0) -> bool:
    """抢锁，抢不到就等一会儿再抢。

    为什么要有这个（2026-11-24 补充）：守护进程 net_guard 在自动修复网络时会
    短暂持有这把锁（通常 15 秒内）。用户正好在那个窗口双击桌面图标，
    旧代码会立刻判定"已有实例正在运行"并静默 return 0 —— 双击毫无反应。
    等一等再抢，就能在守护修完之后正常弹出面板。
    """
    deadline = time.time() + max(0.0, seconds)
    while True:
        if acquire_single_instance():
            return True
        # 抢失败时 CreateMutexW 也返回了有效句柄，先还回去再重试，
        # 否则每轮都漏一个句柄。
        release_single_instance()
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def _run() -> int:
    if not acquire_single_instance_wait(8.0):
        # 以前这里只是一行 log 就静默退出，用户看到的就是"双击没反应"。
        # 现在明确告诉用户发生了什么，而不是假装没事。
        log("已有实例正在运行（等待 8 秒仍未释放），本次忽略")
        fallback_messagebox(
            "校园网一键登录",
            "登录面板已经在使用了。\n\n"
            "如果屏幕上没看到它，可能是它被挡在其他窗口后面，"
            "按 Alt+Tab 切换过去；\n"
            "或者它正在自动修复网络（一般十几秒），稍等再双击一次。",
            error=False)
        return 0

    # 首次运行（还没有 config.dat）：弹控制台向导让用户录入账号密码
    if not os.path.exists(CONFIG_FILE):
        if getattr(sys, "frozen", False):
            console_setup()
        else:
            fallback_messagebox(
                "校园网一键登录",
                "尚未配置账号密码。\n请先运行同目录下的 setup_credentials.py",
                error=True)
            return 2
        if not os.path.exists(CONFIG_FILE):     # 用户没填完就关了窗口
            return 2

    try:
        cfg = load_config()
    except Exception as e:
        log("配置读取失败: %s" % e)
        fallback_messagebox(
            "校园网一键登录",
            "配置读取失败：%s\n配置文件与本机 Windows 账户绑定，换电脑后需要重新配置。" % e,
            error=True)
        return 2

    log("-" * 46)
    log("启动：账号=%s 运营商=%s" % (cfg["account"], cfg.get("isp", "@telecom")))

    # 点下去的那一刻就把面板摆出来：全程只有这一块，状态原地更新，不会闪
    ui_open()
    ui_set("正在检测网络", "看看现在是不是已经在校园网里")

    origin = get_current_ssid()

    if is_campus_ssid(origin):
        # ① 已经在校园网里 —— 直接确认网络就绪，然后认证
        log("已经连着校园网 %s" % origin)
        ui_set("正在检测网络", "已经在校园网里，正在确认网络状态")
        if not probe_portal():
            ui_set("正在等待网络就绪", "已连上校园网，正在获取 IP 地址")
            if not wait_for_portal(PORTAL_WAIT):
                log("已在校园网，但 %d 秒内认证服务器不可达" % PORTAL_WAIT)
                ui_finish("网络还没就绪",
                          "连着校园网，但暂时上不了网\n过一会儿再点我一次", ok=False)
                return 3
    elif not origin and probe_portal():
        # 没连 Wi-Fi 但门户可达 —— 可能是网线，按在校内处理
        log("没连 Wi-Fi 但认证服务器可达，按在校内网处理（可能是有线连接）")
    else:
        # ② 不在校园网 —— 先挑出附近的那一个，再切过去。会断一下网，属正常。
        # 校园网在宿舍和教学楼名字不一样，必须先认出"哪一个在附近"，
        # 否则就会像 11-23 那样：人站在教学楼里，程序却在找宿舍的网名。
        target = pick_campus_target()

        if not target:
            visible = scan_networks(2.0)
            log("校园网名单=%s 当前=%s 周围可见=%s"
                % ("/".join(CAMPUS_SSIDS), origin or "(未连接)",
                   "、".join(visible) if visible else "(扫不到任何网络)"))
            ui_finish("没搜到校园网",
                      "周围没搜到校园网\n（%s 都不在附近）\n\n"
                      "先确认人在校内，再点我一次" % "、".join(CAMPUS_SSIDS), ok=False)
            return 3

        log("当前 Wi-Fi=%s，附近校园网是 %s，准备切换"
            % (origin or "(未连接)", target))
        ui_set("正在连接校园网", "正在切换到 %s，会断一下网" % target)

        if not connect_wifi(target):
            visible = scan_networks(2.0)
            try:
                _, _q = wlan_scan_target(target, wait=2.0)
                signature = ("%d%%" % _q) if _q and _q > 0 else "看不到强度"
            except Exception:
                signature = "看不到强度"
            log("周围可见 Wi-Fi: %s" % ("、".join(visible) if visible else "(扫不到任何网络)"))
            if origin:
                connect_wifi(origin)          # 没连上就把原来的网还给用户
                log("已切回原网络 %s" % origin)
            if target in visible:
                # 这几乎总是"网卡粘在原网络上不松手"（10-23、10-31 两次都是这样）：
                # 目标信号 100%、WlanConnect 返回成功，可系统 12 秒后还在旧网络上。
                ui_finish("连不上校园网",
                          "%s 就在附近（信号有 %s），但系统切不过去\n"
                          "已在重试时强制断开过一次，仍不行\n\n"
                          "下一步：右下角 Wi-Fi 里手动点一次 %s；\n"
                          "要是连手动也切不动，重启电脑就好（关机再开，别用快速启动）"
                          % (target, signature, target), ok=False)
            else:
                ui_finish("没搜到校园网",
                          "刚才还能看到 %s，切的时候又消失了\n再点我一次试试" % target,
                          ok=False)
            return 3

        ui_set("正在等待网络就绪", "已连上 %s，正在获取 IP 地址" % target)
        if not wait_for_portal(PORTAL_WAIT):
            log("连上校园网后 %d 秒内认证服务器仍不可达，切回 %s"
                % (PORTAL_WAIT, origin or "(无)"))
            if origin:
                connect_wifi(origin)
            ui_finish("校园网连不上",
                      "连上 Wi-Fi 了，但一直上不了网\n已切回原来的网络", ok=False)
            return 3

    # ③ 到这里才允许提交账号密码。
    # **这道闸不能省**：手机热点借校园网出口时，认证服务器同样"可达"，
    # 但服务器看到的源 IP 不对，会返回"账号或密码错误"这种误导性结果
    # （2026-10-08 实测踩到：人还在热点上，服务器却说密码错）。
    log("认证服务器可达，出口 IP=%s" % get_local_ip())

    # 让网络"稳一下"再提交：刚切换完 Wi-Fi 时，路由/ARP/门户会话都还没建立，
    # 太早提交容易被服务器当成异常会话（2026-10-23 手机端实测过）。
    ui_set("正在认证", "已连上校园网，正在等网络就绪")
    settle_deadline = time.time() + 3.0
    while time.time() < settle_deadline:
        if probe_portal():
            break
        time.sleep(0.4)

    # 提交最多 4 次；临时性拒绝（在线数超限 / 无响应）隔几秒再来，
    # 账号被拒（密码、绑定）立即停 —— 重试只会让用户更烦。
    # 多出来的一轮是留给"僵尸会话修复"的：发现"已经在线"其实没网时，
    # 要先注销本机 IP 再登一次，这一步得占一轮。
    raw = ""
    ok = False
    text = "认证失败"
    used = 0
    wait_s = 0.0
    pending = False
    reauthed = False          # 僵尸会话修复只做一次，不做第二次

    for attempt in (1, 2, 3, 4):
        used = attempt
        if attempt > 1:
            ui_set("正在重试认证",
                   "第 %d/4 次，等 %.0f 秒\n上一次：%s"
                   % (attempt, wait_s, explain_fail(raw).split("\n")[0]))
            log("第 %d 次重试，等待 %.0f 秒（上次：%s）"
                % (attempt, wait_s, text.split("\n")[0]))
            time.sleep(wait_s)

        t0 = time.time()
        try:
            raw = request_login(cfg["account"], cfg["password"], cfg.get("isp", "@telecom"))
        except socket.timeout:
            cost = time.time() - t0
            log("请求超时（%.1fs），认证服务器无响应" % cost)
            raw = ""
            text = "认证服务器 %.1f 秒没响应\n门户现在可能比较忙，正在重试" % cost
            wait_s = RETRY_DELAY[min(attempt - 1, 1)]
        except Exception as e:
            log("请求异常（%.1fs）: %r" % (time.time() - t0, e))
            raw = ""
            text = "无法访问认证服务器"
            break                              # 代码层面异常，重试也没用
        else:
            log("提交完成，耗时 %.1fs，服务器原始响应: %s"
                % (time.time() - t0, raw.strip()[:400]))
            ok, text, pending = judge(raw)
            log(("判定成功: " if ok else "判定失败: ") + text)

            # 「已经在线」不能当成功用 —— 必须验一次真实出网。
            # 这一步是本文件最关键的修补：2026-11-08 18:14 服务器回
            # 「IP: 172.17.x.x 已经在线！」，实际连续 9 次被 NCSI
            # 判为"检测到热点"，网页全是门户页。
            if ok and pending:
                if internet_ok():
                    log("复核通过：真实出网正常（服务器说的在线是真的）")
                    text = text + "\n（已复核：确实能上网）"
                elif not reauthed:
                    reauthed = True
                    ip = get_local_ip()
                    log("复核失败：服务器说在线，但真实出网不通 → 判定为僵尸会话")
                    log("对本站 IP %s 发起注销（只影响这台电脑）" % ip)
                    ui_set("正在修复网络会话",
                           "服务器记着这台电脑在线，但网关没放行\n"
                           "正在释放旧会话，然后重新认证")
                    try:
                        lraw = request_logout(ip)
                        log("注销响应: %s" % lraw.strip()[:300])
                        lok, ltxt = judge_logout(lraw)
                        log(("注销成功: " if lok else "注销失败: ") + ltxt)
                    except Exception as e:
                        # 注销失败不阻塞：接着重新登录，多数情况也能覆盖掉旧会话
                        log("注销请求异常: %r" % e)
                    time.sleep(1.5)
                    ok = False
                    text = "已释放旧会话，正在重新认证"
                    wait_s = 1.0
                    continue
                else:
                    log("注销后服务器仍报「已经在线」，判定为它自己的会话没释放")
                    ok = False
                    text = ("服务器一直说这台电脑已经在线，但实际出不了网\n"
                            "它的会话表里还留着旧记录，得等它自己清理\n\n"
                            "下一步：等 1~2 分钟再点我一次\n"
                            "如果手机也连着同一个账号，先在手机上退出校园网")
                    break

            if ok:
                break
            if not retryable_raw(raw):
                break                          # 账号侧拒绝 → 重试没意义
            wait_s = BUSY_DELAY[min(attempt - 1, 1)] if is_busy(raw) \
                else RETRY_DELAY[min(attempt - 1, 1)]

    if ok:
        tail = "" if used == 1 else "（重试 %d 次）" % (used - 1)
        ui_finish("登录成功", "认证通过，可以上网了" + tail, ok=True)
    else:
        if used > 1:
            log("共尝试 %d 次仍失败" % used)
        if is_busy(raw):
            # 忙是临时状态，说清楚"不怪你"，并给出下一步
            text = (text + "\n\n学校的认证服务器现在繁忙，不是你账号的问题\n"
                    "过几分钟再点我一次就行")
        ui_finish("登录失败", text, ok=False)
    return 0 if ok else 1


def main() -> int:
    """入口：跑完流程后等面板自然关闭，再退出。

    带 --logout 参数时执行"下线"，正常启动时执行"登录"。
    """
    try:
        if "--logout" in sys.argv[1:]:
            return _logout_run()
        return _run()
    finally:
        ui_wait()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                       # 兜底，绝不静默失败
        try:
            log("未捕获异常: %r" % exc)
        except Exception:
            pass
        fallback_messagebox("校园网一键登录", "程序出错：%s" % exc, error=True)
        sys.exit(9)
