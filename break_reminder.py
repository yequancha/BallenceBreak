#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""久坐休息提醒工具（Break Reminder）

Windows 10 个人工具：按可配置的分钟数倒计时，到点全屏提醒起身休息。
系统托盘通过 Windows 原生 Shell_NotifyIcon API 实现，不依赖第三方包。

核心功能：
1. 主窗口：设置分钟数 -> 点「开始倒计时」开始倒计时，实时显示剩余时间
   - 「未开始」状态窗口置顶（可开关，默认开启）；「倒计时中」自动取消置顶
   - 最小化 / 点关闭按钮 -> 缩到系统托盘；未开始状态下托盘气泡提醒
2. 空闲检测：鼠标键盘超过 15 分钟无操作 -> 询问是否重新开始计时
   - 询问弹窗不暂停倒计时，后台继续跑
   - 若倒计时已归零而弹窗还在 -> 自动关闭弹窗、回到未开始状态，不再打扰
3. 倒计时结束（无空闲处理）-> 全屏提醒，只能点击按钮关闭（Esc / Alt+F4 无效）
4. 喝水提醒（与休息提醒完全独立，默认开启）：左上角持久气泡，点击「好的，喝一口」才关闭
   - 频率由「每次饮水量 + 每日饮水量 + 工作时间段」自动推导，不手工设置
   - 默认每次 50ml / 每日 1500ml / 时段 08:30–12:30、13:30–17:30
   - 同一时刻至多一个气泡（防堆积）；全屏休息遮罩显示时水气泡照弹并悬浮其上
   - 可勾选「喝水提示框自动消失」（默认不勾选）并自定义秒数，到时自动关闭
   - 设置：主窗口「喝水提醒设置…」独立对话框
5. 系统托盘：左键单击显示窗口；右键菜单含「显示窗口 / 退出」
6. 单实例：同一时刻只跑一个程序；再次启动/打开时自动唤醒已有实例并恢复其界面显示，本进程退出
7. 登录提醒：开机自启拉起后若工作倒计时未开始，显示主界面并托盘冒泡提示「倒计时未开始」；
   锁屏解锁 / 任意账号登录后若倒计时未开始，仅显示置顶主界面（不弹冒泡）
8. 配置持久化：分钟数、置顶开关、喝水提醒参数保存到 config.json，启动自动读取

运行：python break_reminder.py   （或双击 run.bat）
依赖：pip install Pillow  （仅用于绘制托盘图标，安装一次后离线可用）
"""

import ctypes
import json
import os
import queue
import sys
import threading
import time
from ctypes import wintypes

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw

APP_TITLE = "久坐休息提醒"
# 应用根目录 = 配置/日志所在目录。
# 关键：PyInstaller 单文件模式下 __file__ 指向临时解包目录（_MEIxxxx），
# 若照用它，config.json/error.log 会写进临时目录并在退出后被删除 —— 表现为
# "设置每次启动都变回默认值"。因此打包运行时以 exe 所在目录为准。
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")

# 空闲确认阈值：鼠标/键盘连续无输入达到该秒数，认为人已离开
IDLE_THRESHOLD_SECONDS = 15 * 60

# 单实例：命名互斥量 + 主窗口标题，用于判断是否已有实例并唤醒之
SINGLE_INSTANCE_MUTEX = "Local\\BreakReminder_SingleInstance"
WM_BRING_TO_FRONT = 0x8011          # WM_APP + 0x11：唤醒已有实例显示界面

MIN_MINUTES, MAX_MINUTES = 1, 720

# 喝水提醒参数默认值 / 范围（见 喝水提醒-实施方案.md）
WATER_SIP_MIN, WATER_SIP_MAX = 10, 300   # 每次饮水量范围（下限 10ml，便于小口/杯型换算）
WATER_TARGET_MIN, WATER_TARGET_MAX = 500, 4000
WATER_INTERVAL_FLOOR = 5         # 推导间隔下限（分钟）
WATER_INTERVAL_CEIL = 180        # 推导间隔上限（分钟）
WATER_FIRST_DELAY_MIN = 5        # 每段窗口开头首杯的延迟（分钟）
WATER_DEFAULT_MESSAGE = "该喝水啦～啜一口，约 {sip}ml"
WATER_DEFAULT_WINDOWS = [["08:30", "12:30"], ["13:30", "17:30"]]
# 提醒方式（二选一，可在「喝水提醒设置…」里切换）：
#   derive = 按「每次饮水量 + 每日饮水量 + 工作时间段」自动推导间隔（默认，原行为）
#   time   = 按「指定时间点」直接提醒（每天循环，与工作时间段/饮水量无关）
WATER_MODE_DERIVE = "derive"
WATER_MODE_TIME = "time"
WATER_DEFAULT_TIMES = ["10:00", "15:00"]     # 按时间模式的默认提醒时刻
WATER_AUTOCLOSE_DEFAULT = False   # 喝水提示框自动消失（默认不勾选）
WATER_AUTOCLOSE_SECONDS = 10      # 勾选后默认 10 秒自动消失
WATER_AUTOCLOSE_MIN, WATER_AUTOCLOSE_MAX = 3, 120   # 自定义消失秒数范围
# 主屏工作区左上角的安全边距（rx = 物理像素 16，y 略下移避开开始/搜索栏）
WATER_MARGIN_X = 16
WATER_MARGIN_Y = 40

# 全屏提醒的主色
ACCENT = "#1a73e8"

# --------------------------------------------------------------------------
# Win32 基础设施（托盘、空闲检测、DPI）
# --------------------------------------------------------------------------

# 64 位安全的消息类型（标准库 wintypes 的 WPARAM/LPARAM 在 32 位上是错的）
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

# NotifyIcon 常量
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_COMMAND = 0x0111
WM_TRAY_NOTIFY = 0x8001          # WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO = 0x01
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
MF_DEFAULT = 0x1000
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
PM_REMOVE = 0x0001

# 会话状态通知（锁屏/解锁/登录）
WM_WTSSESSION_CHANGE = 0x02B1
NOTIFY_FOR_THIS_SESSION = 0x0000
WTS_SESSION_LOGON = 0x5
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_ubyte * 4)]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", wintypes.BYTE * 16), ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
        ("wParam", WPARAM), ("lParam", LPARAM),
        ("time", wintypes.DWORD), ("pt", wintypes.POINT),
    ]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),      # 显示器完整监视矩形
        ("rcWork", RECT),         # 排除任务栏后的工作区
        ("dwFlags", wintypes.DWORD),
    ]
    MONITORINFOF_PRIMARY = 0x1


WNDPROC = ctypes.WINFUNCTYPE(LPARAM, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)


def _set_win32_signatures():
    u, g, k, s = user32, gdi32, kernel32, shell32
    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k.GetModuleHandleW.restype = wintypes.HMODULE
    u.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    u.RegisterWindowMessageW.restype = wintypes.UINT
    u.RegisterClassW.argtypes = [ctypes.c_void_p]
    u.RegisterClassW.restype = wintypes.ATOM
    u.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    u.CreateWindowExW.restype = wintypes.HWND
    u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
    u.DefWindowProcW.restype = LPARAM
    u.GetCursorPos.argtypes = [ctypes.c_void_p]
    u.GetCursorPos.restype = wintypes.BOOL
    u.CreatePopupMenu.restype = wintypes.HMENU
    u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t,
                              wintypes.LPCWSTR]
    u.AppendMenuW.restype = wintypes.BOOL
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                 ctypes.c_void_p]
    u.TrackPopupMenu.restype = wintypes.BOOL
    u.DestroyMenu.argtypes = [wintypes.HMENU]
    u.DestroyMenu.restype = wintypes.BOOL
    u.DestroyWindow.argtypes = [wintypes.HWND]
    u.DestroyWindow.restype = wintypes.BOOL
    u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    u.MonitorFromWindow.restype = ctypes.c_void_p
    u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.GetMonitorInfoW.restype = wintypes.BOOL
    u.PeekMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                               wintypes.UINT, wintypes.UINT]
    u.PeekMessageW.restype = wintypes.BOOL
    u.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                              wintypes.UINT]
    u.GetMessageW.restype = ctypes.c_int
    u.TranslateMessage.argtypes = [ctypes.c_void_p]
    u.TranslateMessage.restype = wintypes.BOOL
    u.DispatchMessageW.argtypes = [ctypes.c_void_p]
    u.DispatchMessageW.restype = wintypes.LPARAM
    u.GetLastInputInfo.argtypes = [ctypes.c_void_p]
    u.GetLastInputInfo.restype = wintypes.BOOL
    u.GetSystemMetrics.argtypes = [ctypes.c_int]
    u.GetSystemMetrics.restype = ctypes.c_int
    u.DestroyIcon.argtypes = [wintypes.HICON]
    u.DestroyIcon.restype = wintypes.BOOL
    k.GetTickCount.restype = wintypes.DWORD
    g.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g.CreateCompatibleDC.restype = wintypes.HDC
    g.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.c_void_p, wintypes.HANDLE, wintypes.DWORD]
    g.CreateDIBSection.restype = wintypes.HBITMAP
    g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g.DeleteObject.restype = wintypes.BOOL
    g.DeleteDC.argtypes = [wintypes.HDC]
    g.DeleteDC.restype = wintypes.BOOL
    u.CreateIconIndirect.argtypes = [ctypes.c_void_p]
    u.CreateIconIndirect.restype = wintypes.HICON
    s.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
    s.Shell_NotifyIconW.restype = wintypes.BOOL
    u.EnumDisplayMonitors.argtypes = [
        wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    u.EnumDisplayMonitors.restype = wintypes.BOOL
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateMutexW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.GetLastError.restype = wintypes.DWORD
    u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.FindWindowW.restype = wintypes.HWND
    u.IsIconic.argtypes = [wintypes.HWND]
    u.IsIconic.restype = wintypes.BOOL
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.ShowWindow.restype = wintypes.BOOL
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL

    # 会话通知（wtsapi32）：监听锁屏/解锁，解锁后弹「倒计时未开始」
    # 说明：WTSUnregisterSessionNotification 在某些系统上导出名不同，属性访问会抛，
    # 因此单独 try；注册函数正常配置。
    wts = ctypes.windll.wtsapi32
    wts.WTSRegisterSessionNotification.argtypes = [wintypes.HWND,
                                                   wintypes.DWORD]
    wts.WTSRegisterSessionNotification.restype = wintypes.BOOL
    try:
        wts.WTSUnregisterSessionNotification.argtypes = [wintypes.HWND]
        wts.WTSUnregisterSessionNotification.restype = wintypes.BOOL
    except Exception:
        pass


_set_win32_signatures()


def enable_dpi_awareness():
    """让 Tk 界面在高 DPI 下不模糊（Windows）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 单实例：同一时刻只允许一个程序实例在跑
# --------------------------------------------------------------------------

ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9
SW_SHOW = 5

_singleton_mutex = None   # 模块级持有互斥量句柄，进程退出时由 OS 自动清理


def acquire_single_instance():
    """尝试独占单实例互斥量。

    返回 True = 本进程胜出，可继续启动；False = 已有实例在运行。
    互斥量句柄由模块级变量持有，进程退出（正常或崩溃）后 OS 自动释放，
    因此无需显式释放也不会锁死。
    """
    global _singleton_mutex
    if _singleton_mutex:
        return True
    try:
        h = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
        if not h:
            return True          # 创建失败：宽松放行，不阻塞启动
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(h)
            return False
        _singleton_mutex = h
        return True
    except Exception:
        return True


def activate_existing_instance():
    """已有实例在跑时：找到其主窗口并恢复显示、置前台。

    返回 True 表示成功唤醒了既有实例（本进程应退出）。FindWindow 按标题匹配，
    与是否在托盘无关——Tk 主窗口 withdraw 只是隐藏，窗口仍存在。
    """
    try:
        hwnd = user32.FindWindowW(None, APP_TITLE)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, SW_RESTORE if user32.IsIconic(hwnd) else SW_SHOW)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


# 登录/开机自启判定：进程启动时距系统开机的时间（秒），若小于阈值
# 认为本实例是在 Windows 登录后由开机自启拉起（而不是用户中途手动开的）。
# 这里在 import 后立即捕获 GetTickCount（毫秒，自开机起），尽量贴近进程出生时刻。
_process_start_tick_ms = None
try:
    _process_start_tick_ms = kernel32.GetTickCount()
except Exception:
    _process_start_tick_ms = None

BOOT_LAUNCH_GRACE_SECONDS = 5 * 60     # 开机后 5 分钟内启动视为「登录自启」


def is_login_launch():
    """本实例是否由 Windows 登录/开机自启拉起。

    依据：进程出生时的 GetTickCount()（毫秒，即自系统开机起的时长）很小，
    说明刚开机不久就被启动——正是登录后开机自启的典型时机；用户中途手动开则
    tick 已很大。
    """
    if _process_start_tick_ms is None:
        return False
    return _process_start_tick_ms <= BOOT_LAUNCH_GRACE_SECONDS * 1000


# 开机自启：写 HKCU\...\Run 启动项（用户级，免管理员权限）
STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE = "久坐休息提醒"


def set_startup_autostart(enabled):
    """enabled=True 写入注册表 Run 项指向本程序；False 删除该启动项。
    返回 True/False 表示操作是否成功。仅 Windows。

    指向策略：若以 PyInstaller 单文件运行（sys.frozen），直接指向 sys.executable
    （此时 __file__ 在临时解包目录里，不能拿来当启动路径）；若以启动器 exe 拉起
    脚本运行，优先指向同目录的「久坐休息提醒.exe」；否则写成 pythonw.exe + 脚本路径，
    保证开机启动的是这个程序而不是裸解释器。
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 单文件：开机自启直接指向本 exe
        cmd = '"{}"'.format(os.path.abspath(sys.executable))
    else:
        script = os.path.abspath(__file__)
        cmd = sys.executable
        # 尽量避免写到 pip 安装目录里的脚本；优先本程序目录下同名 exe（启动器）
        launcher = os.path.join(os.path.dirname(script), "久坐休息提醒.exe")
        if os.path.exists(launcher):
            cmd = '"{}"'.format(launcher)
        elif script.lower().endswith(".py"):
            cmd = '"{}" "{}"'.format(sys.executable, script)
        else:
            cmd = '"{}"'.format(sys.executable)
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0,
                             winreg.KEY_SET_VALUE)
        try:
            if enabled:
                winreg.SetValueEx(key, STARTUP_VALUE, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, STARTUP_VALUE)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True
    except Exception:
        return False


def startup_autostart_enabled():
    """查询注册表里是否已有本程序的开机自启项。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0,
                            winreg.KEY_QUERY_VALUE) as key:
            winreg.QueryValueEx(key, STARTUP_VALUE)
            return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def get_idle_seconds():
    """系统空闲秒数（鼠标/键盘无输入的时间）。仅 Windows。"""
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if user32.GetLastInputInfo(ctypes.byref(lii)):
            ticks = kernel32.GetTickCount()
            return max(0, (ticks - lii.dwTime) // 1000)
    except Exception:
        pass
    return 0


def virtual_screen_geometry():
    """覆盖所有显示器的 (x, y, w, h)。全屏提醒跨屏使用。"""
    x = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    y = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
    w = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
    h = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
    return x, y, w, h


def primary_monitor_center():
    """返回主显示器中心在「虚拟屏幕坐标系」中的 (x, y)。

    主屏 = MONITORINFOF_PRIMARY 的监视器；结果用于在全屏窗口（覆盖虚拟屏）
    内定位元素，保证提示始终落在主屏正中央，而非连屏(虚拟屏)中心。
    """
    try:
        hmon = user32.MonitorFromWindow(None, 1)   # MONITOR_DEFAULTTOPRIMARY
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            cx = (r.left + r.right) // 2
            cy = (r.top + r.bottom) // 2
            return cx, cy
    except Exception:
        pass
    # 兜底：主屏假定从虚拟屏原点开始
    return user32.GetSystemMetrics(0) // 2, user32.GetSystemMetrics(1) // 2


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    ctypes.c_void_p, ctypes.c_void_p)


def enum_monitors():
    """枚举所有显示器，返回 [ (left, top, right, bottom, is_primary), ... ]，
    坐标为虚拟桌面坐标。供「每个屏幕一个纯色遮罩」使用。"""
    result = []

    def cb(hmon, hdc, lprc, lparam):
        try:
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcMonitor
                result.append((r.left, r.top, r.right, r.bottom,
                              bool(mi.dwFlags & MONITORINFO.MONITORINFOF_PRIMARY)))
        except Exception:
            pass
        return True   # 继续枚举

    try:
        cb_ref = MONITORENUMPROC(cb)
        user32.EnumDisplayMonitors(None, None, cb_ref, None)
    except Exception:
        pass
    if not result:
        # 兜底：单屏从 0,0 开始
        result.append((0, 0, user32.GetSystemMetrics(0),
                       user32.GetSystemMetrics(1), True))
    return result


def _virtual_origin():
    """虚拟屏左上角 (x, y)。"""
    try:
        return virtual_screen_geometry()[:2]
    except Exception:
        return 0, 0


def _virtual_geometry_string():
    """返回覆盖整个虚拟屏的 Tk geometry 字符串，如 '4480x1440-1920+0'。"""
    try:
        x, y, w, h = virtual_screen_geometry()
        sign_x = "+" if x >= 0 else ""
        sign_y = "+" if y >= 0 else ""
        return f"{w}x{h}{sign_x}{x}{sign_y}{y}"
    except Exception:
        return ""


def primary_monitor_work_area():
    """返回主屏「工作区」（排除任务栏后）的 (left, top, right, bottom)。

    水气泡定位在左上角：取主屏 rcWork，而不是全屏遮罩用的 rcMonitor。
    多屏时不受外接屏影响——只看 MONITORINFOF_PRIMARY。
    """
    try:
        hmon = user32.MonitorFromWindow(None, 1)   # MONITOR_DEFAULTTOPRIMARY
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcWork
            if r.right > r.left and r.bottom > r.top:
                return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    # 兜底：主屏从 0,0 开始
    return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


# --------------------------------------------------------------------------
# 界面自适应（DPI / 分辨率 / 系统字体）：窗口按内容自然尺寸贴合，超出可滚动
# --------------------------------------------------------------------------
# 背景：程序是 DPI 感知的，Tk 的「点」字号会按系统 DPI 自动放大
# （96 DPI -> 1.333 px/pt，120 DPI(125%) -> 1.667 px/pt）。
# 因此任何「写死的窗口像素尺寸」在高 DPI 机器上都会被裁掉。
# 这里统一改为「按内容自然尺寸贴合 + 夹紧到主屏工作区 + 装不下时可滚动」。

UI_DPI_BASE = 96.0     # 96 DPI = 100% 缩放，作为「逻辑像素」基准
UI_MARGIN = 12         # 窗口与屏幕工作区边缘的最小留白（物理像素）


def ui_scale(widget):
    """当前界面的缩放比（相对 96 DPI）：100% -> 1.0，125% -> 1.25。"""
    try:
        return max(1.0, min(4.0, widget.winfo_fpixels("1i") / UI_DPI_BASE))
    except Exception:
        return 1.0


def dpi_px(widget, value):
    """逻辑像素 -> 物理像素。用于 wraplength / 边距 / padx 等以像素为单位的参数。"""
    return max(1, int(round(value * ui_scale(widget))))


def work_area_rect():
    """主屏工作区 (left, top, right, bottom)（已排除任务栏）；失败时退回整屏。"""
    try:
        l, t, r, b = primary_monitor_work_area()
        if r > l and b > t:
            return l, t, r, b
    except Exception:
        pass
    try:
        return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    except Exception:
        return 0, 0, 1024, 768


def center_in_work_area(win, w, h, y_div=3):
    """把窗口放进主屏工作区内：水平居中，垂直按 y_div 偏上（2 = 居中）。
    尺寸自动夹紧到工作区，绝不越出屏幕。返回最终 (w, h)。"""
    l, t, r, b = work_area_rect()
    aw = max(160, r - l - 2 * UI_MARGIN)
    ah = max(120, b - t - 2 * UI_MARGIN)
    w = max(1, min(int(w), aw))
    h = max(1, min(int(h), ah))
    x = l + UI_MARGIN + max(0, (aw - w) // 2)
    y = t + UI_MARGIN + max(0, (ah - h) // max(1, int(y_div)))
    try:
        win.geometry(f"{w}x{h}+{x}+{y}")
    except Exception:
        pass
    return w, h


def fit_window_to_content(win, content, min_w=320, min_h=280, y_div=3):
    """按内容「自然尺寸」设置窗口大小，并夹紧到主屏工作区。

    自适应点：DPI 缩放、系统字体差异、分辨率大小都由「内容自然尺寸」自动消化，
    不再写死窗口宽高（写死就是「换台电脑界面显示不全」的根因）。
    窗口可缩放；内容若被夹紧，需由调用方提供 ScrollBox 滚动兜底。
    返回最终 (w, h)。
    """
    try:
        content.update_idletasks()
        req_w = max(1, content.winfo_reqwidth())
        req_h = max(1, content.winfo_reqheight())
    except Exception:
        return 0, 0
    l, t, r, b = work_area_rect()
    avail_w = max(160, r - l - 2 * UI_MARGIN)
    avail_h = max(120, b - t - 2 * UI_MARGIN)
    w = min(max(int(min_w), req_w), avail_w)
    h = min(max(int(min_h), req_h), avail_h)
    center_in_work_area(win, w, h, y_div)
    try:
        win.minsize(min(w, dpi_px(win, 300)), min(h, dpi_px(win, 220)))
        win.maxsize(avail_w, avail_h)
    except Exception:
        pass
    return w, h


class ScrollBox:
    """可滚动容器：内容装得下时滚动条自动隐藏，装不下时自动出现（含滚轮）。

    content 属性即业务控件的父容器，用法与普通 Frame 完全一致。
    """

    def __init__(self, parent, padding=24):
        self.outer = ttk.Frame(parent)
        self.outer.pack(fill="both", expand=True)
        self.outer.rowconfigure(0, weight=1)
        self.outer.columnconfigure(0, weight=1)

        # 让画布底色跟 ttk 主题一致，避免出现白色底块
        opts = {"highlightthickness": 0, "bd": 0, "takefocus": 0}
        try:
            bg = ttk.Style(parent).lookup("TFrame", "background")
            if bg:
                opts["bg"] = bg
        except Exception:
            pass
        self.canvas = tk.Canvas(self.outer, **opts)
        self.vbar = ttk.Scrollbar(self.outer, orient="vertical",
                                  command=self.canvas.yview)
        self.hbar = ttk.Scrollbar(self.outer, orient="horizontal",
                                  command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self._on_yview,
                              xscrollcommand=self._on_xview)
        self.canvas.grid(row=0, column=0, sticky="nsew")

        self.content = ttk.Frame(self.canvas, padding=padding)
        self._win = self.canvas.create_window((0, 0), window=self.content,
                                              anchor="nw")
        self._syncing = False
        self.content.bind("<Configure>", self._on_content)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.outer.bind("<Configure>", self._on_canvas)
        # 首帧同步：等控件布局完成后再判定是否需要滚动条
        try:
            self.outer.after_idle(self._sync_bars)
        except Exception:
            pass

    # ---- 滚动条按需显示（按内容自然尺寸与容器尺寸判定，不依赖 yview 回调）----
    def _on_yview(self, first, last):
        try:
            self.vbar.set(first, last)
        except Exception:
            pass

    def _on_xview(self, first, last):
        try:
            self.hbar.set(first, last)
        except Exception:
            pass

    def _on_content(self, event=None):
        self._sync_bars()

    def _on_canvas(self, event=None):
        self._sync_bars()

    def _sync_bars(self):
        """内容装得下 -> 隐藏滚动条；装不下 -> 显示，并保持滚动区域正确。

        只在「尺寸判定」处切换显隐，避免出现「先显示再隐藏」的抖动/死锁。
        """
        if self._syncing:
            return
        self._syncing = True
        try:
            aw = self.outer.winfo_width()
            ah = self.outer.winfo_height()
            nw = max(1, self.content.winfo_reqwidth())
            nh = max(1, self.content.winfo_reqheight())
            bw = self.vbar.winfo_reqwidth() or 16
            bh = self.hbar.winfo_reqheight() or 16

            need_h = nw > aw                       # 水平装不下
            need_v = nh > ah - (bh if need_h else 0)
            # 复核一遍（显隐会改变可用空间，避免判定来回跳）
            if need_v and not need_h and nw > aw - bw:
                need_h = True
            if need_h and nh > ah - bh:
                need_v = True

            if need_v:
                self.vbar.grid(row=0, column=1, sticky="ns")
            else:
                self.vbar.grid_remove()
            if need_h:
                self.hbar.grid(row=1, column=0, sticky="ew")
            else:
                self.hbar.grid_remove()

            # 内容宽度跟随容器，但不小于其自然宽度（否则会被压扁/裁掉）
            cw = self.canvas.winfo_width()
            self.canvas.itemconfigure(self._win,
                                      width=max(cw, nw) if not need_h else nw)
            self.canvas.configure(
                scrollregion=(0, 0, max(aw, nw), max(ah, nh)))
        except Exception:
            pass
        finally:
            self._syncing = False

    # ---- 滚轮（Tk 的滚轮事件不冒泡，需逐个控件绑定）----
    def bind_wheel(self, widget=None):
        w = self.content if widget is None else widget
        try:
            w.bind("<MouseWheel>", self._on_wheel, add="+")
            children = w.winfo_children()
        except Exception:
            return
        for child in children:
            self.bind_wheel(child)

    def _on_wheel(self, event):
        try:
            if not self.vbar.winfo_ismapped():
                return                       # 内容装得下，无需滚动
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass


def _clock_minutes(now):
    """now 的当天时刻换算成「从 00:00 起的分钟数」；now 为 float 墙钟秒。"""
    t = time.localtime(now)
    return t.tm_hour * 60 + t.tm_min


def _wall_from_minutes(day_minutes, now):
    """把「当天第 N 分钟」还原成墙钟秒。day_minutes 可越界(负/超当日)自动折返。"""
    tm = time.localtime(now)
    return time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, -1)) \
        + day_minutes * 60


def _normalize_windows(raw):
    """把配置里的工作时间段容错解析成 [('HH:MM','HH:MM'), ...]。

    raw 应是 [ [start, end], ... ] 字符串对列表；单项坏 / 非法时间 / 结束<=开始
    一律丢弃（对应实施方案 §7「非法时间直接忽略不写」）。空列表保留为空（=提醒失效，
    用户在设置窗删空时段后恢复配置不该被默认值覆盖）；非列表坏结构回退默认。
    """
    default = WATER_DEFAULT_WINDOWS
    if not isinstance(raw, (list, tuple)):
        return [tuple(p) for p in default]
    out = []
    for item in raw:
        try:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            s, e = str(item[0]), str(item[1])
            st = time.strptime(s.strip(), "%H:%M")
            et = time.strptime(e.strip(), "%H:%M")
            sm, em = st.tm_hour * 60 + st.tm_min, et.tm_hour * 60 + et.tm_min
            if em <= sm:                     # 结束必须 > 开始
                continue
            out.append((s.strip(), e.strip()))
        except Exception:
            continue
    return out


def _normalize_times(raw):
    """把配置里的「提醒时间点」容错解析成按时间排序的 ['HH:MM', ...]。

    与 _normalize_windows 同一套容错约定：非字符串 / 无法按 %H:%M 解析的一律丢弃、
    重复时刻合并（同一时刻只提醒一次）；空列表保留为空（= 按时间模式下不提醒，
    用户在设置窗删空时间后恢复配置不该被默认值覆盖）；非列表坏结构回退默认。
    """
    if not isinstance(raw, (list, tuple)):
        return list(WATER_DEFAULT_TIMES)
    out = []
    for item in raw:
        try:
            t = time.strptime(str(item).strip(), "%H:%M")
            hhmm = "%02d:%02d" % (t.tm_hour, t.tm_min)
            if hhmm not in out:
                out.append(hhmm)
        except Exception:
            continue
    out.sort(key=lambda s: (int(s[:2]), int(s[3:])))
    return out


def water_work_minutes(windows):
    """工作时间段总分钟数（前后段叠加计算）。"""
    total = 0
    for (s, e) in windows:
        st = time.strptime(s, "%H:%M")
        et = time.strptime(e, "%H:%M")
        total += (et.tm_hour * 60 + et.tm_min) - (st.tm_hour * 60 + st.tm_min)
    return total


def water_interval_minutes(sip, target, windows):
    """由每次饮水量 + 每日饮水量 + 工作时间段推导提醒间隔（分钟），含钳制。

    公式：每日杯数 = round(目标水量 / 每次饮水量)
          提醒间隔 = 全部工作时间段总分钟 / 每日杯数
    间隔下限 5 分钟 / 上限 180 分钟（实施方案 §4）。
    任一时段为空或总量 0 => 返回 None（提醒失效）。
    """
    total = water_work_minutes(windows)
    if total <= 0 or sip <= 0 or target <= 0:
        return None
    cups = max(1, round(target / sip))
    interval = total / cups
    return max(WATER_INTERVAL_FLOOR, min(WATER_INTERVAL_CEIL, interval))


# --------------------------------------------------------------------------
# 系统托盘（原生 Shell_NotifyIcon，无第三方依赖）
# --------------------------------------------------------------------------

class WinTray:
    """
    系统托盘（原生 Shell_NotifyIcon）。

    关键：Windows 窗口消息必须由「创建该窗口的线程」收取并派发。
    因此整个 WinTray（注册类、建窗口、加图标、GetMessage 循环、WndProc）
    全部放在一个独立的后台线程中完成；它与主线程只通过线程安全的
    queue.Queue 传递事件（"show" / "quit"）。主线程 WM_TRAY_NOTIFY/菜单
    事件不会触碰任何 Tk 对象，杜绝跨线程崩溃。
    """

    ID_SHOW = 1
    ID_QUIT = 2

    def __init__(self, tip=APP_TITLE):
        self._events = queue.Queue()       # 后台线程 -> 主线程
        self._tip = tip
        self._ready = threading.Event()    # 线程初始化完成
        self._closing = threading.Event()  # 请求关闭
        self._handle = None
        self._hicon = None
        self._nid = None
        self._added = False
        self._thread = None

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(3.0):
            raise RuntimeError("tray thread failed to start")

    # ---- 后台线程：创建 + 消息循环 ---- 

    def _run(self):
        # 全部 Win32 窗口相关代码在此线程执行（与 GetMessage 同一线程）
        try:
            self._taskbar_msg = user32.RegisterWindowMessageW("TaskbarCreated")

            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(WNDPROC(self._wnd_proc), ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = "BreakReminderTrayWnd"
            user32.RegisterClassW(ctypes.byref(wc))

            hwnd = user32.CreateWindowExW(
                0, "BreakReminderTrayWnd", "", 0, 0, 0, 0, 0,
                wintypes.HWND(-3),  # HWND_MESSAGE：消息类窗口
                None, hinst, None)
            if not hwnd:
                self._ready.set()
                return
            self._handle = hwnd
            # 注册会话通知：接收锁屏/解锁消息（WM_WTSSESSION_CHANGE），
            # 解锁后由主线程弹「倒计时未开始」冒泡（需求：锁屏再登录要有提示）
            try:
                ctypes.windll.wtsapi32.WTSRegisterSessionNotification(
                    hwnd, NOTIFY_FOR_THIS_SESSION)
            except Exception:
                pass

            self._hicon = _make_hicon()
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 0
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY_NOTIFY
            nid.hIcon = self._hicon
            nid.szTip = self._tip[:127]
            self._nid = nid
            shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            self._added = True

            self._ready.set()

            # 消息循环（本线程的窗口消息）
            msg = MSG()
            while not self._closing.is_set():
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass
        finally:
            # 清理
            try:
                if self._added and self._nid is not None:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
            try:
                if self._handle:
                    user32.DestroyWindow(self._handle)
            except Exception:
                pass
            try:
                if self._hicon:
                    user32.DestroyIcon(self._hicon)
            except Exception:
                pass
            # 注销会话通知（若注册过）
            try:
                if self._handle:
                    ctypes.windll.wtsapi32.WTSUnregisterSessionNotification(
                        self._handle)
            except Exception:
                pass
            self._added = False
            self._ready.set()

    # ---- 窗口过程（后台线程）----

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_WTSSESSION_CHANGE:
                low = wparam & 0xFF
                # 解锁（锁屏后重新登录）与任意账号登录（快速切换用户）都视为
                # 「登录后回来」：若倒计时未开始则显示界面并弹提示。
                if low == WTS_SESSION_UNLOCK or low == WTS_SESSION_LOGON:
                    self._emit("unlock")
                return 0
            if self._taskbar_msg and msg == self._taskbar_msg:
                # 资源管理器重启后重挂图标
                if self._nid is not None:
                    shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid))
                return 0
            if msg == WM_TRAY_NOTIFY:
                code = lparam & 0xFFFF
                if code == WM_LBUTTONUP:
                    self._emit("show")
                    return 0
                if code == WM_RBUTTONUP:
                    self._popup_menu()
                    return 0
            elif msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                hi = (wparam >> 16) & 0xFFFF
                if hi != 0:
                    return 0
                if cmd == self.ID_SHOW:
                    self._emit("show")
                elif cmd == self.ID_QUIT:
                    self._emit("quit")
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _emit(self, ev):
        """线程安全：只写队列，绝不直接调用 Tk。"""
        try:
            self._events.put(ev)
        except Exception:
            pass

    # ---- 菜单（后台线程）----

    def _popup_menu(self):
        try:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu, MF_STRING | MF_DEFAULT, self.ID_SHOW, "显示窗口")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, self.ID_QUIT, "退出")
            user32.SetForegroundWindow(self._handle)
            cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                        pt.x, pt.y, 0, self._handle, None)
            user32.DestroyMenu(menu)
            if cmd == self.ID_SHOW:
                self._emit("show")
            elif cmd == self.ID_QUIT:
                self._emit("quit")
        except Exception:
            pass

    # ---- 主线程可安全调用（纯 Win32，线程安全）----

    def notify(self, text, title=APP_TITLE):
        # 关键：绝不把 Shell_NotifyIconW 放在主(Tk)线程执行。
        # explorer.exe 忙/刚解锁/任务栏重建时，Shell_NotifyIconW(NIM_MODIFY) 可能
        # 长时间阻塞调用线程；若在主线程做，整个 UI（含倒计时刷新、退出）会停摆，
        # 表现为“卡住：窗口显示不了也退不了、按钮灰着但秒数不动”。
        # 这里丢给一个短命 daemon 线程执行，主线程立即返回，统称“冒泡不阻塞”。
        if self._nid is None:
            return
        nid = self._nid

        def _do_notify():
            try:
                nid.uFlags = NIF_INFO
                nid.szInfo = text[:255]
                nid.szInfoTitle = title[:63]
                nid.dwInfoFlags = NIIF_INFO
                nid.uTimeoutOrVersion = 0
                shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
                nid.szInfo = ""
                nid.szInfoTitle = ""
                shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            except Exception:
                pass

        threading.Thread(target=_do_notify, daemon=True).start()

    def stop(self):
        self._closing.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        self._handle = None
        self._hicon = None
        self._nid = None
        self._added = False


def _make_hicon(size=32):
    """把画好的时钟图案转成 32 位带透明通道的 HICON（alpha icon）。

    用兼容做法：bmiHeader 高度设为 2 倍（上半颜色 + 下半 1 位掩码），
    同时提供 hbmColor 与 hbmMask，避免旧版 Shell 读取时图标变透明不可见。
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size * 0.07
    d.ellipse((pad, pad, size - pad, size - pad), fill=ACCENT)
    cx = cy = size / 2
    w2 = max(1, int(size * 0.08))
    white = (255, 255, 255, 255)
    d.line((cx, cy, cx, cy - size * 0.28), fill=white, width=w2)
    d.line((cx, cy, cx + size * 0.20, cy + size * 0.05), fill=white, width=w2)
    r = size * 0.06
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=white)

    # RGBA(顶到下) -> BGRA 颜色缓冲（32bpp，顶部行序）
    raw = img.tobytes()
    n = size * size
    bgra = bytearray(n * 4)
    for i in range(n):
        j = i * 4
        bgra[j] = raw[j + 2]      # B
        bgra[j + 1] = raw[j + 1]  # G
        bgra[j + 2] = raw[j]      # R
        bgra[j + 3] = raw[j + 3]  # A
    # 掩码：1 位 @ size*ceil(size/8) 字节。掩码位=0 显示像素，=1 挖空(透明)。
    # 每个像素对应掩码一行（行序与位图一致：顶到下）；行宽按 DWORD 对齐为 4 的倍数。
    mask_row = ((size + 31) // 32) * 4
    mask = bytearray(mask_row * size)
    for y in range(size):
        for x in range(size):
            a = raw[(y * size + x) * 4 + 3]
            if a < 128:                      # 透明像素 -> 掩码位 = 1（挖空）
                byte_i = y * mask_row + (x // 8)
                mask[byte_i] |= (0x80 >> (x % 8))

    def dib(height):
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = size
        bi.bmiHeader.biHeight = -height       # 负值 = 自顶向下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0        # BI_RGB
        hdc = gdi32.CreateCompatibleDC(None)
        bits = ctypes.c_void_p()
        hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bi), 0,
                                     ctypes.byref(bits), None, 0)
        if not hbm:
            if hdc:
                gdi32.DeleteDC(hdc)
            return None, hdc, bits
        return hbm, hdc, bits

    hbm_color, hdc_color, bits_color = dib(size)
    hbm_mask, hdc_mask, bits_mask = dib(size)
    try:
        if not hbm_color or not hbm_mask:
            return user32.LoadIconW(None, 32512)   # IDI_APPLICATION 兜底
        ctypes.memmove(bits_color, bytes(bgra), len(bgra))
        ctypes.memmove(bits_mask, bytes(mask) + bytes(len(mask) % 1), len(mask))
        info = ICONINFO()
        info.fIcon = 1
        info.hbmMask = hbm_mask
        info.hbmColor = hbm_color
        hicon = user32.CreateIconIndirect(ctypes.byref(info))
        return hicon or None
    finally:
        if hbm_color:
            gdi32.DeleteObject(hbm_color)
        if hdc_color:
            gdi32.DeleteDC(hdc_color)
        if hbm_mask:
            gdi32.DeleteObject(hbm_mask)
        if hdc_mask:
            gdi32.DeleteDC(hdc_mask)


# --------------------------------------------------------------------------
# 主应用
# --------------------------------------------------------------------------

class BreakReminderApp:
    STATE_IDLE = "idle"          # 未开始
    STATE_COUNTING = "counting"  # 倒计时中

    def __init__(self, root):
        self.root = root
        self.state = self.STATE_IDLE
        self.remaining = 0        # 剩余秒数
        self.total_seconds = 0    # 本轮设定总时长（秒）

        self.minutes = 50
        self.topmost_on = True
        self.auto_min_on = True        # 开始倒计时后自动最小化到托盘（默认勾选）
        self.reminder_msg = ""         # 全屏提醒提示语（空=默认）
        self.delay_minutes = 5         # 延后休息分钟数（默认5）
        self.delay_mode = False        # 当前是否为延后休息倒计时（与常规倒计时区分）
        self.restart_on_done = False   # 点「休息完成」后自动开始下一轮工作倒计时（默认不勾选）
        self.autostart_on = True       # 开机自动启动（默认勾选）

        # 喝水提醒（实施方案 §1/§4/§8）—— 独立于休息提醒
        self.water_enabled = True            # 默认开（老用户升级即生效）
        self.water_sip = 50                  # 每次饮水量 ml
        self.water_target = 1500             # 每日饮水量 ml
        self.water_windows = [                # 工作时间段（可多段）
            ["08:30", "12:30"], ["13:30", "17:30"]]
        self.water_message = ""              # 提醒文案（空=默认）
        self.water_autoclose = WATER_AUTOCLOSE_DEFAULT  # 提示框自动消失（默认不勾选）
        self.water_autoclose_seconds = WATER_AUTOCLOSE_SECONDS  # 自动消失秒数
        self.water_mode = WATER_MODE_DERIVE  # 提醒方式：derive 推导 / time 指定时间
        self.water_times = list(WATER_DEFAULT_TIMES)  # 时间模式下的提醒时刻 ['HH:MM', ...]
        self._water_interval = None          # 推导出的间隔（分钟，实时重算）

        # 若 config.json 里没有该字段，则以注册表实际状态为准，避免覆盖用户系统设置
        self._autostart_from_registry = False

        self._tray = None
        self._tick_stop = None              # 倒计时后台线程停止信号
        self._tick_thread = None            # 倒计时后台线程
        self._ui_dirty = False              # 后台线程标记 UI 需刷新
        self._ui_poll_job = None            # 主线程 UI 轮询
        self._app_events = queue.Queue()    # 后台线程 -> 主线程 应用事件
        self._pump_job = None
        self._fullscreen_windows = []       # 全屏遮罩窗口列表
        self._idle_dialog = None          # 空闲确认弹窗
        self._idle_dialog_open = False
        self._idle_waiting = False        # 已对当前空闲片段提示过，等人回来后再重置

        # 喝水提醒运行时状态（§9）
        self._water_stop = None           # 喝水调度线程停止信号
        self._water_thread = None         # 喝水调度后台线程
        self._water_bubbles = []          # 当前存活的水气泡窗口列表（同一时刻至多 1 个，防堆积）
        self._water_next = None           # 主线程持有的下一次提醒墙钟秒（共享，供状态显示）
        self._water_autoclose_job = None  # 水气泡自动消失定时器（after id）
        # 「倒计时未开始」托盘冒泡（登录自启 & 锁屏解锁两种触发）
        self._login_notify_done = False

        self._load_config()
        self._apply_autostart()        # 默认勾选开机自启：确保注册表启动项与实际状态一致
        self._build_main_window()
        self._start_tray()
        self._apply_topmost()
        self._render()
        # 主线程 UI 轮询启动（含归零兜底检测）
        self._ui_poll()
        # 登录/开机自启启动：若工作倒计时未开始，显示界面并托盘冒泡提示
        self._maybe_login_notify()

    def _maybe_login_notify(self):
        """Windows 登录时被开机自启拉起：若倒计时未开始，则托盘冒泡提示「倒计时未开始」。

        用 is_login_launch()（进程启动离系统开机 < 5 分钟）判定是否登录拉起，
        避免用户中途手动打开也弹。只在 state 仍为 idle 时弹，且仅一次。
        """
        if self._login_notify_done:
            return
        self._login_notify_done = True
        if self.state != self.STATE_IDLE:
            return
        if not is_login_launch():
            return
        self._notify_countdown_not_started()

    def _notify_countdown_not_started(self):
        """开机登录自启后：若工作倒计时未开始，显示主界面窗口并弹托盘冒泡提示
        （提醒设置时长开始工作）。锁屏解锁路径不走这里，另见 _show_main_after_lock。"""
        if self.state != self.STATE_IDLE:
            return
        self._show_main()     # 显示主界面（从托盘收回并置前）
        try:
            self._notify("倒计时未开始，快去设置个时长开始工作吧")
        except Exception:
            pass

    def _show_main_after_lock(self):
        """锁屏后重新登录（解锁/任意账号登录）：若工作倒计时未开始，仅显示置顶的主界面，
        不再弹托盘冒泡提示。"""
        if self.state != self.STATE_IDLE:
            return
        self._show_main()     # 显示主界面（从托盘收回、置顶并置前）

    # ---------------------------------------------------------- 配置

    def _load_config(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.minutes = int(data.get("minutes", 50)) or 50
            self.topmost_on = bool(data.get("topmost", True))
            self.auto_min_on = bool(data.get("auto_min", True))
            self.reminder_msg = str(data.get("message", "") or "")
            self.delay_minutes = max(MIN_MINUTES, min(MAX_MINUTES,
                                    int(data.get("delay_minutes", 5))))
            self.restart_on_done = bool(data.get("restart_on_done", False))
            if "autostart" in data:
                self.autostart_on = bool(data.get("autostart", True))
            else:
                # config 里无该字段（旧版本升级）：以注册表实际状态为准，避免覆盖用户系统设置
                self.autostart_on = startup_autostart_enabled()
            # 喝水提醒（缺字段一律走默认，向后兼容老 config.json）
            self.water_enabled = bool(data.get("water_enabled", True))
            self.water_sip = max(
                WATER_SIP_MIN, min(WATER_SIP_MAX,
                                   int(data.get("water_sip", 50))))
            self.water_target = max(
                WATER_TARGET_MIN, min(WATER_TARGET_MAX,
                                      int(data.get("water_target", 1500))))
            if "water_windows" in data:
                # 已持久化：容错解析；为空列表则保留（用户在设置窗删空时段 = 提醒失效）
                wins = _normalize_windows(data["water_windows"])
                self.water_windows = [[s, e] for (s, e) in wins]
            else:
                # 旧版本无该字段：走默认
                self.water_windows = [[s, e]
                                      for (s, e) in WATER_DEFAULT_WINDOWS]
            # 提醒方式：缺字段走默认（旧配置自动升级），非法值回退「按推导」
            mode = str(data.get("water_mode", WATER_MODE_DERIVE) or "").strip()
            self.water_mode = (WATER_MODE_TIME if mode == WATER_MODE_TIME
                               else WATER_MODE_DERIVE)
            if "water_times" in data:
                # 已持久化：容错解析；为空列表保留（时间模式下删空 = 不提醒）
                self.water_times = _normalize_times(data["water_times"])
            else:
                self.water_times = list(WATER_DEFAULT_TIMES)
            self.water_message = str(data.get("water_message", "") or "")
            self.water_autoclose = bool(
                data.get("water_autoclose", WATER_AUTOCLOSE_DEFAULT))
            self.water_autoclose_seconds = max(
                WATER_AUTOCLOSE_MIN,
                min(WATER_AUTOCLOSE_MAX,
                    int(data.get("water_autoclose_seconds",
                                 WATER_AUTOCLOSE_SECONDS))))
        except Exception:
            pass

    def _save_config(self):
        data = {"minutes": self.minutes,
                "topmost": self.topmost_on,
                "auto_min": self.auto_min_on,
                "message": self.reminder_msg,
                "delay_minutes": self.delay_minutes,
                "restart_on_done": self.restart_on_done,
                "autostart": self.autostart_on,
                "water_enabled": self.water_enabled,
                "water_target": self.water_target,
                "water_sip": self.water_sip,
                "water_windows": [list(w) for w in self.water_windows],
                "water_mode": self.water_mode,
                "water_times": list(self.water_times),
                "water_message": self.water_message,
                "water_autoclose": self.water_autoclose,
                "water_autoclose_seconds": self.water_autoclose_seconds}
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _apply_autostart(self):
        """把 autostart_on 同步到注册表启动项：仅当状态不一致时写入，避免每次启动都写。"""
        try:
            registered = startup_autostart_enabled()
            if bool(self.autostart_on) != registered:
                set_startup_autostart(bool(self.autostart_on))
        except Exception:
            pass

    # ---------------------------------------------------------- 主窗口

    def _build_main_window(self):
        root = self.root
        root.title(APP_TITLE)
        # 允许缩放：窗口尺寸按内容自适应，用户也可自行调整（原先写死 360x512
        # 且禁止缩放，在 125% 缩放等其他机器上会把底部内容裁掉）
        root.resizable(True, True)
        root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        root.bind("<Unmap>", self._on_unmap)

        # 自适应容器：内容装得下时无滚动条，装不下（小屏 / 高缩放 / 大字体）时
        # 自动出现滚动条 + 支持滚轮，保证任何机器上控件都能看到、点到
        self._main_box = ScrollBox(root, padding=24)
        frame = self._main_box.content

        ttk.Label(frame, text="工作倒计时", font=("Microsoft YaHei UI", 14, "bold")
                  ).grid(row=0, column=0, columnspan=3, pady=(0, 16))

        ttk.Label(frame, text="时长（分钟）：", font=("Microsoft YaHei UI", 11)
                  ).grid(row=1, column=0, sticky="e")
        self.minutes_var = tk.IntVar(value=self.minutes)
        self.spin = ttk.Spinbox(frame, from_=MIN_MINUTES, to=MAX_MINUTES,
                                textvariable=self.minutes_var, width=8,
                                font=("Microsoft YaHei UI", 12))
        self.spin.grid(row=1, column=1, padx=(8, 4), sticky="w")
        self.minutes_var.trace_add("write", lambda *a: self._on_minutes_change())

        # 剩余时间 / 当前设置显示
        self.time_label = ttk.Label(frame, text="", font=("Consolas", 24, "bold"),
                                    foreground=ACCENT)
        self.time_label.grid(row=2, column=0, columnspan=3, pady=(20, 6))
        self.status_label = ttk.Label(frame, text="", font=("Microsoft YaHei UI", 10),
                                      foreground="#666666")
        self.status_label.grid(row=3, column=0, columnspan=3, pady=(0, 10))

        # 喝水提醒倒计时显示（距下次喝水 / 期间状态）
        self.water_status_label = ttk.Label(
            frame, text="", font=("Microsoft YaHei UI", 10, "bold"),
            foreground="#1a7a37")
        self.water_status_label.grid(row=4, column=0, columnspan=3, pady=(0, 8))

        # 按钮
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=5, column=0, columnspan=3, pady=(6, 0))
        self.start_btn = ttk.Button(btn_frame, text="开始倒计时",
                                    command=self._start_countdown, width=13)
        self.start_btn.grid(row=0, column=0, padx=(0, 8))
        self.stop_btn = ttk.Button(btn_frame, text="停止",
                                   command=self._stop_countdown, width=13,
                                   state="disabled")
        self.stop_btn.grid(row=0, column=1)
        # 喝水提醒设置（独立 Toplevel，主窗不加行）
        ttk.Button(btn_frame, text="喝水提醒设置…",
                   command=self._show_water_settings, width=13).grid(
            row=0, column=2, padx=(8, 0))

        # 提示语（全屏提醒显示；留空用默认）
        ttk.Label(frame, text="提示语：", font=("Microsoft YaHei UI", 11)
                  ).grid(row=6, column=0, pady=(14, 0), sticky="e")
        self.message_var = tk.StringVar(value=self.reminder_msg)
        ttk.Entry(frame, textvariable=self.message_var, width=22,
                  font=("Microsoft YaHei UI", 10)
                  ).grid(row=6, column=1, columnspan=2, pady=(14, 0),
                         padx=(8, 4), sticky="w")
        self.message_var.trace_add("write", lambda *a: self._on_message_change())

        # 延后休息时长（分钟，独立的配置）
        ttk.Label(frame, text="延后(分)：", font=("Microsoft YaHei UI", 11)
                  ).grid(row=7, column=0, pady=(14, 0), sticky="e")
        self.delay_var = tk.IntVar(value=self.delay_minutes)
        ttk.Spinbox(frame, from_=MIN_MINUTES, to=MAX_MINUTES,
                    textvariable=self.delay_var, width=8,
                    font=("Microsoft YaHei UI", 12)
                    ).grid(row=7, column=1, pady=(14, 0), padx=(8, 4), sticky="w")
        self.delay_var.trace_add("write", lambda *a: self._on_delay_change())

        self.auto_min_var = tk.BooleanVar(value=self.auto_min_on)
        ttk.Checkbutton(frame, text="开始倒计时后自动最小化到托盘",
                        variable=self.auto_min_var,
                        command=self._on_auto_min_toggle
                        ).grid(row=8, column=0, columnspan=3, pady=(12, 0))

        self.restart_on_done_var = tk.BooleanVar(value=self.restart_on_done)
        ttk.Checkbutton(frame, text="休息完成自动开始工作倒计时",
                        variable=self.restart_on_done_var,
                        command=self._on_restart_on_done_toggle
                        ).grid(row=9, column=0, columnspan=3, pady=(12, 0))

        self.autostart_var = tk.BooleanVar(value=self.autostart_on)
        ttk.Checkbutton(frame, text="开机自动启动",
                        variable=self.autostart_var,
                        command=self._on_autostart_toggle
                        ).grid(row=10, column=0, columnspan=3, pady=(12, 0))

        self.topmost_var = tk.BooleanVar(value=self.topmost_on)
        ttk.Checkbutton(frame, text="未开始时窗口置顶", variable=self.topmost_var,
                        command=self._on_topmost_toggle
                        ).grid(row=11, column=0, columnspan=3, pady=(6, 0))

        # 窗口自适应：按内容自然尺寸贴合主屏工作区（不写死宽高），
        # 高 DPI（如本机 125%）/ 小屏 / 系统字体差异都不会再裁掉内容
        self._main_box.bind_wheel()
        fit_window_to_content(root, frame, min_w=320, min_h=300, y_div=3)

    def _on_minutes_change(self):
        self.minutes = self._read_minutes()
        self._save_config()
        if self.state == self.STATE_IDLE:
            self._render_remaining()

    def _on_message_change(self):
        try:
            self.reminder_msg = self.message_var.get()
        except Exception:
            return
        self._save_config()

    def _read_delay_minutes(self):
        try:
            v = int(self.delay_var.get())
        except Exception:
            return self.delay_minutes
        return max(MIN_MINUTES, min(MAX_MINUTES, v))

    def _on_delay_change(self):
        self.delay_minutes = self._read_delay_minutes()
        self._save_config()

    def _read_minutes(self):
        try:
            v = int(self.minutes_var.get())
        except Exception:
            return self.minutes
        return max(MIN_MINUTES, min(MAX_MINUTES, v))

    # ---------------------------------------------------------- 状态控制

    def _begin_countdown(self, minutes, delay):
        """通用：以 minutes（分钟）启动倒计时；delay=True 表示延后休息倒计时。"""
        # 关键修复：启动新一轮倒计时前，先关掉可能残留的上一轮全屏遮罩。
        # 场景：全屏提醒弹出后（用户还没点按钮），用户直接在仍可见的主界面点
        # 「开始倒计时」——此时遮罩还挂在屏幕上，但那已是上一轮的 UI。
        # 若不清理，驻留在遮罩里的「休息完成/延后休息」按钮仍可被点击，
        # 会回调 close() 把这一轮刚开始的倒计时误杀（见 bug 报告：
        # 点完休息后主界面又弹出、倒计时处于未开始状态）。
        # 新的倒计时属于全新一轮，旧遮罩应被立即销毁，避免状态机混淆。
        self._close_fullscreen()
        self.minutes = minutes
        self.delay_mode = bool(delay)
        self.total_seconds = minutes * 60
        self.remaining = self.total_seconds
        self.state = self.STATE_COUNTING
        self._save_config()
        # 重新计时前后台真正用到的也是这套总时长；旧轮残留的全屏已清理，
        # 因此归零后一定走「弹全屏提醒」主线，不会被上一轮的空闲确认弹窗吞掉。
        self._apply_topmost()      # 倒计时中取消置顶
        self._render()
        self._schedule_tick()

    def _start_countdown(self):
        if self.state != self.STATE_IDLE:
            return
        self._begin_countdown(self._read_minutes(), delay=False)
        # 勾选了「自动最小化」且窗口当前可见时，缩到托盘开始静默计时。
        # 直接读勾选控件值（而非仅缓存属性），避免编程设置不触发回调时失效。
        auto_min = self.auto_min_on
        try:
            auto_min = bool(self.auto_min_var.get())
        except Exception:
            pass
        if auto_min and self.root.state() != "withdrawn":
            self._hide_to_tray()

    def _start_delay_countdown(self):
        """全屏提醒点「延后休息」：以延后时长启动倒计时。"""
        self._begin_countdown(self._read_delay_minutes(), delay=True)
        # 延后休息也自动缩到托盘，静默计时
        try:
            if self.root.state() != "withdrawn":
                self._hide_to_tray()
        except Exception:
            pass

    def _stop_countdown(self):
        if self.state != self.STATE_COUNTING:
            return
        self._close_idle_dialog()
        self.delay_mode = False
        self.state = self.STATE_IDLE
        self.remaining = 0
        self._cancel_tick()
        self._apply_topmost()
        self._render()

    def _on_countdown_finish(self):
        """倒计时归零：弹全屏提醒；若残留空闲确认弹窗则先关闭它再弹。

        （历史行为：空闲弹窗存在时不打扰、只关闭弹窗不弹全屏。但这会在
        用户「重新开始新一轮倒计时后、倒计时期间被空闲弹窗盖住」的路径下，
        表现为到点后什么提醒都没有——用户点开始倒计时就是想被提醒。
        因此改为：先关掉残留的空闲弹窗，再照常弹全屏提醒。）
        """
        if self._idle_dialog_open:
            self._close_idle_dialog()
        self.state = self.STATE_IDLE
        self.remaining = 0
        self._cancel_tick()
        self._render()
        # 防御：show 全屏若在极端时序下抛 Tk 异常，也不能让 poll 链断裂或进程闪退
        try:
            self._show_fullscreen_reminder()
        except BaseException:
            self._log_error(sys.exc_info()[1])
            self._close_fullscreen()

    # ---------------------------------------------------------- 倒计时心跳

    # ---- 倒计时心跳：独立后台线程，避免 Tk after 在窗口 withdrawn 时被拖慢 ----

    def _schedule_tick(self):
        self._cancel_tick()
        self._tick_stop = threading.Event()
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._tick_thread.start()

    def _cancel_tick(self):
        if self._tick_stop is not None:
            self._tick_stop.set()
        self._tick_stop = None
        self._tick_thread = None

    def _tick_loop(self):
        # 纯后台逻辑线程：绝不直接操作 Tk（Tk 非线程安全，跨线程调用即崩溃）。
        # 只维护剩余秒数、状态标志、写入事件队列；一切 Tk 操作由主线程完成。
        ev = self._tick_stop
        while ev is not None and not ev.is_set():
            if self.state != self.STATE_COUNTING:
                break
            self.remaining -= 1
            self._ui_dirty = True
            if self.remaining <= 0:
                # 归零：写入事件队列，主线程 _ui_poll 消费并弹全屏
                self._enqueue_app_event("finish")
                break
            # 空闲检测（系统调用，天然线程安全）
            try:
                self._check_idle()
            except Exception:
                pass
            ev = self._tick_stop
            if ev is None:
                break
            ev.wait(1.0)

    def _enqueue_app_event(self, ev):
        """后台线程安全入队：主线程 _ui_poll 消费。"""
        try:
            self._app_events.put(ev)
        except Exception:
            pass

    def _ui_poll(self):
        # 主线程高频轮询（每 ~100ms)：统一消费后台线程/托盘线程投递的事件，
        # 并做 UI 刷新。所有 Tk 操作只在这里（主线程）执行。
        # 关键：整个函数必须“永不死循环”。一旦 after() 链因任何异常断开，
        # UI 立即停摆（按钮灰着、秒数不动、窗口看似卡死）。因此把真正的
        # 业务逻辑包进 try/except(BaseException)，最后无论成败都重新调度，
        # 异常只打日志绝不让轮询断链。
        # 注意：取队列用 get_nowait()，空队列抛 queue.Empty 是“正常退路”，
        # 不能把它和渲染逻辑放在同一个 try 里（否则空队一次就跳过整段渲染，
        # 秒数永远不刷新）——所以先独立处理事件，再无条件做渲染。
        if getattr(self, "_quitting", False):
            return
        _err = None

        # ---- 1) 托盘事件 + 2) 应用事件（倒计时归零 / 空闲提示 / 喝水）----
        try:
            self._drain_tray_events()
            while True:
                ev = self._app_events.get_nowait()
                if ev == "finish":
                    self._on_countdown_finish()
                elif ev == "idle":
                    self._show_idle_dialog()
                elif ev == "water":
                    self._show_water_bubble()
        except queue.Empty:
            pass                     # 队列空 = 正常，继续
        except BaseException as e:   # 事件处理任何异常都不杀轮询链
            _err = e

        # ---- 3) 渲染：与 1/2 分离，永远执行（这一块之前不能因为空队被跳过）----
        try:
            if self._ui_dirty:
                self._ui_dirty = False
                self._render_remaining()
            self._render_water_remaining()
            # ---- 4) 兜底：后台线程已归零但事件未及处理（窗口隐藏态轮询可能略慢）----
            if self.state == self.STATE_COUNTING and self.remaining <= 0:
                self._on_countdown_finish()
                # 注意：这里不能 return 提前跑掉——归零后 _on_countdown_finish 里
                # 可能打开全屏遮罩（大量 Toplevel 创建），此刻立刻 return 会让本函数
                # 跳过下面的重新调度；若 _on_countdown_finish 恰好抛异常（Tk 在
                # 极端时序下的 C 层崩溃），poll 链就断了、UI 停摆甚至进程闪退。
                # 统一走末尾的重新调度，宁可多等一个周期也别让轮询断链。
        except BaseException as e:
            _err = e

        # ---- 无论成败，重新调度自己 ----
        try:
            if self.root.winfo_exists():
                self._ui_poll_job = self.root.after(100, self._ui_poll)
        except BaseException:
            pass
        if _err is not None:
            # 不打到控制台（可能无窗口）；记入应用日志便于排查
            self._log_error(_err)

    def _log_error(self, exc):
        """把未知异常写入同目录 error.log（绝不抛异常、绝不影响主流程）。"""
        try:
            import traceback
            tb = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__))
            with open(os.path.join(APP_DIR, "error.log"), "a",
                      encoding="utf-8") as f:
                f.write("\n[{}] {}\n{}".format(
                    time.strftime("%Y-%m-%d %H:%M:%S"), type(exc).__name__, tb))
        except Exception:
            pass

    def _render(self):
        counting = self.state == self.STATE_COUNTING
        self.start_btn.config(state="disabled" if counting else "normal")
        self.stop_btn.config(state="normal" if counting else "disabled")
        self.spin.config(state="disabled" if counting else "normal")
        self._render_remaining()
        self._render_water_remaining()

    def _render_remaining(self):
        if self.state == self.STATE_COUNTING:
            minutes, secs = divmod(max(0, self.remaining), 60)
            self.time_label.config(text=f"{minutes:02d}:{secs:02d}")
            if self.delay_mode:
                # 延后休息倒计时：独立文案，与常规区分
                self.status_label.config(
                    text=f"延后休息中 · {self.minutes} 分钟 · 之后弹全屏提醒")
                self.time_label.config(font=("Consolas", 24, "bold"),
                                       foreground="#e8710a")  # 橙色，与常规区分
            else:
                self.time_label.config(font=("Consolas", 24, "bold"),
                                       foreground=ACCENT)
                self.status_label.config(
                    text=f"本轮 {self.minutes} 分钟 · 离开超过 {IDLE_THRESHOLD_SECONDS // 60} 分钟会询问重新开始")
        else:
            self.time_label.config(text=f"{self.minutes} 分钟",
                                   font=("Consolas", 24, "bold"),
                                   foreground=ACCENT)
            self.status_label.config(text="点击「开始倒计时」开始")

    def _render_water_remaining(self):
        """主窗口喝水提醒倒计时显示：距下次喝水还有多久（调度线程共享 _water_next）。"""
        if not getattr(self, "water_status_label", None):
            return
        try:
            txt, color = self._water_status_text()
            self.water_status_label.config(text=txt, foreground=color)
        except Exception:
            pass

    def _water_status_text(self):
        """计算主窗口喝水状态文本与颜色。返回 (text, color)。（两种提醒方式文案不同）"""
        if not self.water_enabled:
            return ("💧 喝水提醒已关闭", "#999999")
        by_time = (self.water_mode == WATER_MODE_TIME)
        if by_time and not self.water_times:
            return ("💧 未设置提醒时间，不提醒", "#999999")
        if not by_time and not self.water_windows:
            return ("💧 未设置工作时间段，不提醒", "#999999")
        if self._water_bubbles:
            return ("💧 该喝一口啦！", "#d93025")   # 气泡未点，保持催促
        target = self._water_next
        if target is None:
            return ("💧 等待下次安排…", "#1a7a37")

        remain = target - time.time()
        # 已到点（事件待主线程消费弹气泡）
        if remain <= 0:
            return ("💧 该喝一口啦！", "#d93025")   # 红色催促
        mins, secs = divmod(int(remain), 60)
        hmm = ""
        if mins >= 60:
            hmm = f"{mins // 60} 小时 "
            mins %= 60
        if by_time:
            hhmm = time.strftime("%H:%M", time.localtime(target))
            text = f"💧 距 {hhmm} 喝水还有 {hmm}{mins:02d}:{secs:02d}"
        else:
            text = f"💧 距下次喝水 {hmm}{mins:02d}:{secs:02d}"
        color = "#d93025" if remain < 60 else "#1a7a37"
        return (text, color)

    # ---------------------------------------------------------- 置顶

    def _apply_topmost(self):
        on = self.topmost_var.get() if hasattr(self, "topmost_var") else self.topmost_on
        if self.state == self.STATE_COUNTING:
            on = False            # 倒计时中不盖住其他窗口
        try:
            self.root.attributes("-topmost", bool(on))
        except Exception:
            pass

    def _on_topmost_toggle(self):
        try:
            self.topmost_on = bool(self.topmost_var.get())
        except Exception:
            pass
        self._save_config()
        self._apply_topmost()

    def _on_auto_min_toggle(self):
        try:
            self.auto_min_on = bool(self.auto_min_var.get())
        except Exception:
            pass
        self._save_config()

    def _on_restart_on_done_toggle(self):
        try:
            self.restart_on_done = bool(self.restart_on_done_var.get())
        except Exception:
            pass
        self._save_config()

    def _on_autostart_toggle(self):
        try:
            self.autostart_on = bool(self.autostart_var.get())
        except Exception:
            pass
        self._apply_autostart()        # 同步注册表启动项
        self._save_config()

    # ---------------------------------------------------------- 空闲检测

    def _check_idle(self):
        # 只从后台倒计时线程调用。只做判断；所有 UI 操作写入事件队列，
        # 由主线程消费后执行（绝不在此跨线程操作 Tk）。
        idle = get_idle_seconds()
        if idle < 2:
            self._idle_waiting = False          # 人回来了，下次空闲片段可再次询问
            return
        if (idle >= IDLE_THRESHOLD_SECONDS
                and not self._idle_dialog_open
                and not self._idle_waiting):
            self._idle_waiting = True           # 本空闲片段只提示一次
            self._enqueue_app_event("idle")

    def _show_idle_dialog(self):
        self._idle_waiting = True               # 本空闲片段只提示一次
        dlg = tk.Toplevel(self.root)
        dlg.title("离开一会儿")
        dlg.resizable(False, False)

        frm = ttk.Frame(dlg, padding=24)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="检测到你已离开超过 15 分钟",
                  font=("Microsoft YaHei UI", 13, "bold")).pack(pady=(0, 6))
        ttk.Label(frm, text="是否重新开始本轮倒计时？",
                  font=("Microsoft YaHei UI", 11), foreground="#444444").pack(pady=(0, 18))

        btns = ttk.Frame(frm)
        btns.pack()
        ttk.Button(btns, text="重新开始", width=10,
                   command=lambda: self._idle_decision(True)).pack(side="left", padx=6)
        ttk.Button(btns, text="继续", width=10,
                   command=lambda: self._idle_decision(False)).pack(side="left", padx=6)

        # 弹窗按内容自然尺寸 + 主屏工作区居中（DPI 自适应；模态但不阻塞后台倒计时）
        dlg.update_idletasks()
        center_in_work_area(dlg, dlg.winfo_reqwidth(), dlg.winfo_reqheight(),
                            y_div=2)
        dlg.grab_set()

        # 用 X 关闭等同于「继续」
        dlg.protocol("WM_DELETE_WINDOW", lambda: self._idle_decision(False))

        self._idle_dialog = dlg
        self._idle_dialog_open = True

    def _idle_decision(self, restart):
        if restart and self.state == self.STATE_COUNTING:
            self.remaining = self.total_seconds
        self._close_idle_dialog()
        self._render_remaining()

    def _close_idle_dialog(self):
        if self._idle_dialog is not None:
            try:
                self._idle_dialog.grab_release()
                self._idle_dialog.destroy()
            except Exception:
                pass
        self._idle_dialog = None
        self._idle_dialog_open = False

    # ---------------------------------------------------------- 全屏提醒

    def _show_fullscreen_reminder(self):
        # 每个显示器建一个纯色遮罩窗口（Windows 多屏全屏锁定标准做法），
        # 其他屏幕被蓝色纯色挡死；提示文字与「休息完成」按钮只在主屏居中。
        # 所有遮罩窗口记录在 self._fullscreen_windows，统一关闭。
        self._close_fullscreen()          # 防重入：若已有遮罩先关掉

        monitors = enum_monitors()        # [(l, t, r, b, is_primary), ...]
        masks = []
        primary_win = None
        primary_rect = None              # 主屏显示器物理区域，供自适应排版使用
        V = 32767                        # 用较大 z 序值置顶

        for (l, t, r, b, is_primary) in monitors:
            w = r - l
            h = b - t
            mask = tk.Toplevel(self.root)
            mask.overrideredirect(True)
            mask.geometry(f"{w}x{h}+{l}+{t}")
            mask.attributes("-topmost", True)
            mask.configure(bg=ACCENT)
            # 遮罩窗口本身不抢焦点，避免多屏下焦点混乱
            mask.attributes("-toolwindow", True)
            masks.append(mask)
            if is_primary:
                primary_win = mask
                primary_rect = (l, t, r, b)

        # 保证所有遮罩绝对置顶于普通窗口之上
        for m in masks:
            try:
                m.attributes("-topmost", True)
                m.lift()
            except Exception:
                pass

        # 主屏窗口放提示 + 按钮（自适应：按主屏实际分辨率与 DPI 换算，绝不溢出）
        fs = primary_win
        if fs is None and masks:
            fs = masks[0]
        # 遮罩窗口的坐标系 = 主屏显示器局部坐标（0,0 起）
        if primary_rect is not None:
            pl, pt_, pr, pb = primary_rect
        else:
            pl, pt_, pr, pb = work_area_rect()
        sw = max(240, int(pr - pl))          # 主屏宽（物理像素）
        sh = max(180, int(pb - pt_))         # 主屏高（物理像素）
        cx = sw // 2                         # 主屏中心

        # 字号随「逻辑分辨率」缩放：Tk 点字号已按 DPI 自动放大，这里只管分辨率，
        # 小屏（如 1366x768）自动收小，大屏最多到原来的 56pt
        scale = ui_scale(self.root)          # 相对 96 DPI 的缩放比
        logical_h = sh / max(1.0, scale)     # 折算成 100% 缩放下的高度
        title_pt = int(max(20, min(56, logical_h / 15)))
        sub_pt = int(max(11, min(20, title_pt * 0.36)))
        btn_pt = int(max(10, min(18, title_pt * 0.32)))
        gap = dpi_px(self.root, 14)
        wrap = max(160, sw - dpi_px(self.root, 200))

        # 提示语：编辑框内容（留空用默认）
        msg = "该休息啦！"
        try:
            txt = (self.message_var.get() or "").strip()
            if txt:
                msg = txt
        except Exception:
            pass

        title_lbl = tk.Label(fs, text=msg,
                             font=("Microsoft YaHei UI", title_pt, "bold"),
                             bg=ACCENT, fg="white", wraplength=wrap,
                             justify="center")

        # 副标题：延后休息时明确提示「延后休息中」
        sub_txt = f"本次倒计时 {self.minutes} 分钟 · 站起来活动一下，让眼睛休息一会儿"
        if self.delay_mode:
            sub_txt = f"延后休息结束 · 已达 {self.minutes} 分钟 · 该休息了"
        sub_lbl = tk.Label(fs, text=sub_txt,
                           font=("Microsoft YaHei UI", sub_pt), bg=ACCENT,
                           fg="#eaf2ff", wraplength=wrap, justify="center")

        def close(show_after=True):
            # 关掉全屏，回到未开始状态。
            # show_after=False：点「休息完成」且勾选「自动开始工作倒计时」时调用，
            # 新倒计时直接静默启动（配合 auto_min 缩到托盘），无需恢复主界面显示。
            #
            # 防御（配合 _begin_countdown 开新一轮前先 _close_fullscreen）：
            # 这套遮罩窗口只属于「当前这一轮」全屏。正常情况下它启动倒计时时已被
            # 清理，不可能再被陈旧按钮回调点到；这里再兜一层——若此刻状态不是
            # 由本全屏的归零（idle）所导致、或 self._fullscreen_windows 已经不是
            # 本闭包持有的 masks，就直接销毁本组遮罩并返回，绝不误杀别的倒计时。
            if self._fullscreen_windows is not masks:
                self._close_fullscreen()
                return
            self.delay_mode = False
            self.state = self.STATE_IDLE
            self.remaining = 0
            self._cancel_tick()
            for w_ in masks:
                try:
                    w_.destroy()
                except Exception:
                    pass
            self._fullscreen_windows = []
            self._apply_topmost()
            self._render()
            if show_after:
                self._show_main()

        def rest_done():
            # 点「休息完成」：关掉全屏、回到未开始。
            # 若勾选了「休息完成自动开始工作倒计时」，随后自动开始下一轮常规工作倒计时
            # （直接读勾选控件值，避免编程设置不触发回调时失效，与 auto_min 一致）。
            restart = self.restart_on_done
            try:
                restart = bool(self.restart_on_done_var.get())
            except Exception:
                pass
            # 会自动开始下一轮则不再显示主界面（避免先弹窗又立刻缩回托盘的闪屏）
            close(show_after=not restart)
            if restart:
                self._start_countdown()

        def postpone():
            # 点「延后休息」：关全屏，用延后时长重新开始倒计时
            close()
            self._start_delay_countdown()

        # 两个并排按钮：休息完成 + 延后休息（先建后量，再自下而上夹紧排版）
        btn_row = tk.Frame(fs, bg=ACCENT)
        btn_pad = dpi_px(self.root, 15)
        btn = tk.Button(btn_row, text="休息完成",
                        font=("Microsoft YaHei UI", btn_pt, "bold"),
                        bg="white", fg=ACCENT, activebackground="#eaf2ff",
                        activeforeground=ACCENT, relief="flat", cursor="hand2",
                        command=rest_done, bd=0, width=10, height=2)
        btn.pack(side="left", padx=btn_pad)
        btn2 = tk.Button(btn_row, text="延后休息",
                         font=("Microsoft YaHei UI", btn_pt, "bold"),
                         bg="#2e8b57", fg="white", activebackground="#3cb371",
                         activeforeground="white", relief="flat", cursor="hand2",
                         command=postpone, bd=0, width=10, height=2)
        btn2.pack(side="left", padx=btn_pad)

        # 依实测高度自下而上排布：按钮 -> 副标题 -> 主标题，
        # 各自夹在「屏幕可视范围内」，长文案/小屏都不会跑到屏幕外或互相压住
        btn_row.update_idletasks()
        title_lbl.update_idletasks()
        sub_lbl.update_idletasks()
        bh = max(1, btn_row.winfo_reqheight())
        th = max(1, title_lbl.winfo_reqheight())
        shh = max(1, sub_lbl.winfo_reqheight())
        y_btn = min(int(sh * 0.70), sh - bh // 2 - gap)
        y_btn = max(bh // 2 + gap, y_btn)
        y_sub = min(int(sh * 0.52), y_btn - bh // 2 - shh // 2 - gap)
        y_sub = max(shh // 2 + gap, y_sub)
        y_title = min(int(sh * 0.36), y_sub - shh // 2 - th // 2 - gap)
        y_title = max(th // 2 + gap, y_title)

        title_lbl.place(x=cx, y=y_title, anchor="center")
        sub_lbl.place(x=cx, y=y_sub, anchor="center")
        btn_row.place(x=cx, y=y_btn, anchor="center")
        btn.focus_force()

        # 只能点按钮关闭：禁用 Esc，Alt+F4 也拦截；WM_CLOSE 忽略
        fs.bind("<Escape>", lambda e: "break")
        fs.bind("<KeyPress-F4>",
                lambda e: "break" if (e.state & 0x020000) else None)
        fs.protocol("WM_DELETE_WINDOW", lambda: None)

        self._fullscreen_windows = masks

        # 竞态兜底（实施方案 §6）：全屏遮罩可能盖住已存在的水气泡，补一次 lift 保证可见可点
        self._lift_water_bubbles()

    def _close_fullscreen(self):
        """销毁所有全屏遮罩窗口（防重入 / 退出时兜底）。"""
        for w_ in getattr(self, "_fullscreen_windows", []) or []:
            try:
                w_.destroy()
            except Exception:
                pass
        self._fullscreen_windows = []

    # ---------------------------------------------------------- 喝水提醒（独立于休息提醒）

    def _start_water_scheduler(self):
        """启动喝水调度线程。该线程只算时间/入队事件，绝不碰 Tk（同 _tick_loop 模式）。"""
        if self._water_stop is not None:
            return
        self._water_stop = threading.Event()
        self._water_thread = threading.Thread(target=self._water_loop, daemon=True)
        self._water_thread.start()

    def _water_loop(self):
        # 独立后台线程：每 0.5s 轮询一次。
        # 线程持有「下一次实际到点」next_fire：
        #   - 到点(now >= next_fire)则入队事件并立即推进到下一轮，整个到点区间内
        #     任何一次轮询都能触发，绝不跳点（旧实现在 0.05s 容差内才会触发，极易错过）；
        #   - 配置(sip/target/时段)变化时自动重算 next_fire，无需重启。
        ev = self._water_stop
        next_fire = None            # 下一次到点的墙钟秒（线程私有）
        last_sig = None             # 上次重算采用的配置签名
        while ev is not None and not ev.is_set():
            try:
                by_time = (self.water_mode == WATER_MODE_TIME)
                # 时间模式：只看「提醒时间点」；推导模式：看「工作时间段」
                plan_ok = (bool(self.water_times) if by_time
                           else bool(self.water_windows))
                if self.water_enabled and plan_ok:
                    # 签名含「提醒方式 / 时间点 / 饮水量 / 时段」：任一变化都会让线程
                    # 自动重算下一次到点，切换模式无需重启程序
                    sig = (self.water_mode, int(self.water_sip),
                           int(self.water_target),
                           tuple(self.water_times),
                           tuple(tuple(w) for w in self.water_windows))
                    now = time.time()
                    if last_sig != sig or next_fire is None:
                        # 配置变更 / 首次：从现在起重算下一个到点（严格 > now，不补历史）
                        last_sig = sig
                        next_fire = self._water_next_due(now)
                    # 共享下一个到点给主线程做倒计时显示（整值写，线程安全）
                    self._water_next = next_fire
                    if next_fire is not None and now >= next_fire:
                        self._enqueue_app_event("water")
                        # 推进到下一轮：基于刚过的到点向后找下一个（保持节奏）
                        nxt = self._water_next_due(next_fire + 1.0)
                        if nxt is not None and nxt > next_fire:
                            next_fire = nxt
                        else:
                            next_fire = self._water_next_due(now)
                        self._water_next = next_fire
                else:
                    # 关闭 / 未配置提醒时间（时间模式）或未设置时段（推导模式）
                    self._water_next = None
                    next_fire = None
                    last_sig = None
            except Exception:
                pass
            ev.wait(0.5)

    def _water_next_due(self, now):
        """按当前「提醒方式」推算下一次到点（严格 > now）。"""
        if self.water_mode == WATER_MODE_TIME:
            return self._water_next_by_time(now)
        return self._water_next_scheduled(now)

    def _water_next_by_time(self, now):
        """时间模式：返回严格 > now 的下一个「指定时刻」墙钟秒（每天循环，错过不补发）。

        与「工作时间段 / 饮水量」完全无关：到点就提醒，跨天自动滚到次日第一个时刻。
        """
        mins = []
        for t in self.water_times:
            try:
                st = time.strptime(str(t).strip(), "%H:%M")
                mins.append(st.tm_hour * 60 + st.tm_min)
            except Exception:
                continue
        if not mins:
            return None
        mins = sorted(set(mins))
        tm = time.localtime(now)
        now_min = tm.tm_hour * 60 + tm.tm_min + (now % 60) / 60.0
        day_origin = _wall_from_minutes(0, now)     # 今日 00:00 的墙钟秒
        for m in mins:
            if m > now_min:
                return day_origin + m * 60
        # 今天全部已过 -> 次日第一个时刻（用本地墙钟构造次日，避免夏令时误差）
        tomorrow = time.mktime(
            (tm.tm_year, tm.tm_mon, tm.tm_mday + 1, 0, 0, 0, 0, 0, -1))
        return tomorrow + mins[0] * 60

    def _water_next_scheduled(self, now):
        """按当前配置推算下一次喝水提醒的墙钟秒（严格 > now），跨窗口/跨天自动衔接。

        规则（实施方案 §5）：每段窗口内首杯在段开始后约 5 分钟，之后按推导间隔均匀
        铺开到段末（段太短时首杯延迟收窄为段长，保证会弹一次）；下一段自然重新起算；
        当天全部铺完则滚到次日首段。错过不补发。
        """
        sip = int(self.water_sip)
        target = int(self.water_target)
        windows = self.water_windows
        interval = water_interval_minutes(sip, target, windows)
        if interval is None:
            return None
        # 解析时间段为「当日分钟数」且去重排序
        segs = []
        for (s, e) in windows:
            try:
                st = time.strptime(str(s).strip(), "%H:%M")
                et = time.strptime(str(e).strip(), "%H:%M")
                sm = st.tm_hour * 60 + st.tm_min
                em = et.tm_hour * 60 + et.tm_min
                if em > sm:
                    segs.append((sm, em))
            except Exception:
                continue
        if not segs:
            return None
        segs.sort()

        # 取本地墙钟的「当日分钟数」（不能用 epoch 算术，那只算得出 UTC 时刻）
        t_now = time.localtime(now)
        now_min = t_now.tm_hour * 60 + t_now.tm_min + (now % 60) / 60.0
        day_origin = _wall_from_minutes(0, now)   # 今日 00:00 墙钟秒

        # 1) 找今天还剩的可触发点
        for (sm, em) in segs:
            if float(em) <= now_min:
                continue                      # 该段已结束
            first_delay = min(WATER_FIRST_DELAY_MIN, max(0, em - sm))
            p = sm + first_delay
            if p <= now_min:                  # 首杯已过 -> 沿间隔推到下一个点
                while p <= now_min:
                    p += interval
            if p <= float(em) and p > now_min:
                return day_origin + p * 60
        # 2) 今天全部铺完 -> 滚到次日首段开头（+首杯延迟）
        sm0, em0 = segs[0]
        first_delay = min(WATER_FIRST_DELAY_MIN, max(0, em0 - sm0))
        tm = time.localtime(now)
        tomorrow = time.mktime(
            (tm.tm_year, tm.tm_mon, tm.tm_mday + 1, 0, 0, 0, 0, 0, -1))
        return tomorrow + (sm0 + first_delay) * 60

    def _show_water_bubble(self):
        """主线程消费 "water" 事件：显示左上角持久气泡（点击才关闭，防堆积）。"""
        # 唯一抑制规则：同一时刻至多一个水气泡；已有气泡未关闭则跳过本轮（§6）
        if self._water_bubbles:
            return
        sip = int(self.water_sip)
        msg = (self.water_message or "").strip()
        if not msg:
            msg = WATER_DEFAULT_MESSAGE.format(sip=sip)

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)                     # 无边框
        try:
            win.attributes("-topmost", True)           # 悬浮于所有窗口（含全屏遮罩上方）
        except Exception:
            pass
        try:
            win.attributes("-toolwindow", True)        # 无任务栏图标 / 不占 Alt-Tab
        except Exception:
            pass

        card = tk.Frame(win, bg="white", highlightbackground=ACCENT,
                        highlightthickness=2, bd=0)
        card.pack(fill="both", expand=True)

        head = tk.Frame(card, bg=ACCENT)
        head.pack(fill="x")
        tk.Label(head, text="  💧 喝水提醒", bg=ACCENT, fg="white",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w",
                                                               pady=(4, 4))
        tk.Label(card, text=msg, bg="white", fg="#333333",
                 font=("Microsoft YaHei UI", 11),
                 wraplength=dpi_px(win, 260), justify="left").pack(
            fill="x", padx=14, pady=(10, 6))
        tk.Button(card, text="好的，喝一口", command=self._water_bubble_close,
                  bg=ACCENT, fg="white", activebackground="#4d8fe6",
                  activeforeground="white", relief="flat", cursor="hand2",
                  font=("Microsoft YaHei UI", 10, "bold"),
                  bd=0, padx=16, pady=6).pack(anchor="e", padx=12, pady=(0, 12))

        self._water_cur_win = win                  # 当前气泡，供关闭回调定位
        # 定位：主屏工作区左上角 + 安全边距（边距随 DPI 缩放，并整体夹紧在工作区内）
        l, t, r, b = work_area_rect()
        win.update_idletasks()
        bw = win.winfo_reqwidth()
        bh = win.winfo_reqheight()
        mx = dpi_px(win, WATER_MARGIN_X)
        my = dpi_px(win, WATER_MARGIN_Y)
        x = min(int(l) + mx, max(int(l), int(r) - bw - mx))
        y = min(int(t) + my, max(int(t), int(b) - bh - my))
        win.geometry(f"+{x}+{y}")
        win.protocol("WM_DELETE_WINDOW", self._water_bubble_close)
        win.bind("<Escape>", lambda e: None)
        try:
            win.lift()                                # 压过全屏休息遮罩
        except Exception:
            pass
        self._water_bubbles.append(win)

        # 可选自动消失：勾选后在设定秒数后自动关闭（默认不勾选）
        if self.water_autoclose:
            seconds = max(WATER_AUTOCLOSE_MIN,
                          min(WATER_AUTOCLOSE_MAX,
                              int(self.water_autoclose_seconds)))
            self._water_autoclose_job = self.root.after(
                seconds * 1000, self._water_bubble_close)

    def _water_bubble_close(self):
        """关闭当前水气泡（点击「好的，喝一口」或自动消失触发）。"""
        # 取消待触发的自动消失定时器，避免 its 回调操作已销毁窗口
        if self._water_autoclose_job is not None:
            try:
                self.root.after_cancel(self._water_autoclose_job)
            except Exception:
                pass
            self._water_autoclose_job = None
        win = getattr(self, "_water_cur_win", None)
        self._water_cur_win = None
        if win is not None and win in self._water_bubbles:
            try:
                if win.winfo_exists():
                    win.destroy()
            except Exception:
                pass
            try:
                self._water_bubbles.remove(win)
            except Exception:
                pass

    def _close_water_bubbles(self):
        """销毁所有水气泡窗口（退出 / 兜底清理）。"""
        if self._water_autoclose_job is not None:
            try:
                self.root.after_cancel(self._water_autoclose_job)
            except Exception:
                pass
            self._water_autoclose_job = None
        for w_ in list(self._water_bubbles):
            try:
                if w_.winfo_exists():
                    w_.destroy()
            except Exception:
                pass
        self._water_bubbles = []

    def _lift_water_bubbles(self):
        """全屏遮罩创建后把现存水气泡 lift 回最前（§6 竞态兜底）。"""
        for w_ in list(self._water_bubbles):
            try:
                w_.attributes("-topmost", True)
                w_.lift()
            except Exception:
                pass

    # ---- 设置窗辅助 ----

    def _water_valid_pair(self, s, e):
        """校验「开始 结束」HH:MM 对：可解析且结束 > 开始。"""
        try:
            st = time.strptime(s.strip(), "%H:%M")
            et = time.strptime(e.strip(), "%H:%M")
            return (et.tm_hour * 60 + et.tm_min) > (st.tm_hour * 60 + st.tm_min)
        except Exception:
            return False

    def _water_valid_time(self, s):
        """校验单个 HH:MM 时刻是否合法（用于「按指定时间」模式）。"""
        try:
            time.strptime(str(s).strip(), "%H:%M")
            return True
        except Exception:
            return False

    def _water_derive_preview(self, sip, target, windows):
        """设置窗实时推导信息：'预计每 N 分钟提醒一次，全天约 M 次 ≈ X L'。"""
        if not windows:
            return "未设置工作时间段，喝水提醒暂不生效（时段删空）"
        interval = water_interval_minutes(sip, target, windows)
        if interval is None:
            return "参数无效：请检查饮水量设置"
        cups = max(1, round(target / sip))
        exact = water_work_minutes(windows) / cups
        if interval == WATER_INTERVAL_FLOOR and exact < WATER_INTERVAL_FLOOR:
            note = "（已触下限 5 分钟，实际次数少于公式）"
        elif interval == WATER_INTERVAL_CEIL and exact > WATER_INTERVAL_CEIL:
            note = "（已触上限 180 分钟）"
        else:
            note = ""
        iv = f"{interval:.0f}" if float(interval).is_integer() else f"{interval:.1f}"
        return (f"预计每 {iv} 分钟提醒一次，全天约 {cups} 次 "
                f"≈ {target / 1000:g}L{note}")

    def _show_water_settings(self):
        """独立 Toplevel 设置窗（主窗口不再加行）。关闭 = 保存。"""
        dlg = tk.Toplevel(self.root)
        dlg.title("喝水提醒设置")
        # 允许缩放 + 自适应容器：高 DPI / 小屏下不会再把「完成」按钮挤出屏幕
        dlg.resizable(True, True)

        box = ScrollBox(dlg, padding=18)
        frm = box.content

        enabled_var = tk.BooleanVar(value=self.water_enabled)
        ttk.Checkbutton(frm, text="启用喝水提醒", variable=enabled_var
                        ).grid(row=0, column=0, columnspan=4, sticky="w",
                               pady=(0, 10))

        # 提醒方式：二选一（推导间隔 / 直接指定时间）
        mode_var = tk.StringVar(value=self.water_mode)
        derive_only = []        # 仅「按饮水量推导」模式显示的控件
        time_only = []          # 仅「按指定时间」模式显示的控件

        def on_mode_change():
            """切换提醒方式：推导模式显示饮水量+工作时间段，时间模式显示提醒时间点。"""
            is_time = mode_var.get() == WATER_MODE_TIME
            for w in derive_only:
                if is_time:
                    w.grid_remove()          # 收起该行（行高归零，下方内容自动上移）
                else:
                    w.grid()
            for w in time_only:
                w.grid() if is_time else w.grid_remove()
            refresh_preview()

        ttk.Radiobutton(frm, text="按饮水量推导", variable=mode_var,
                        value=WATER_MODE_DERIVE,
                        command=on_mode_change).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Radiobutton(frm, text="按指定时间", variable=mode_var,
                        value=WATER_MODE_TIME,
                        command=on_mode_change).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(0, 6))

        # 每次饮水量 / 每日饮水量（仅「按饮水量推导」模式使用）
        sip_var = tk.IntVar(value=self.water_sip)
        target_var = tk.IntVar(value=self.water_target)
        sip_lbl = ttk.Label(frm, text="每次饮水量（ml）：")
        sip_lbl.grid(row=2, column=0, sticky="e", padx=(0, 6))
        spin_sip = ttk.Spinbox(frm, from_=WATER_SIP_MIN, to=WATER_SIP_MAX,
                               textvariable=sip_var, width=7,
                               font=("Microsoft YaHei UI", 10))
        spin_sip.grid(row=2, column=1, sticky="w", padx=(0, 16))

        tgt_lbl = ttk.Label(frm, text="每日饮水量（ml）：")
        tgt_lbl.grid(row=2, column=2, sticky="e", padx=(0, 6))
        spin_tgt = ttk.Spinbox(frm, from_=WATER_TARGET_MIN, to=WATER_TARGET_MAX,
                               textvariable=target_var, width=8,
                               font=("Microsoft YaHei UI", 10))
        spin_tgt.grid(row=2, column=3, sticky="w")

        # 工作时间段（多行编辑，可增删行）—— 仅「按饮水量推导」模式使用
        win_title = ttk.Label(frm, text="工作时间段（开始 结束，HH:MM）：",
                              font=("Microsoft YaHei UI", 10))
        win_title.grid(row=3, column=0, columnspan=4, sticky="w", pady=(14, 4))
        rows_box = ttk.Frame(frm)
        rows_box.grid(row=4, column=0, columnspan=4, sticky="w")
        row_entries = []          # [(start_entry, end_entry), ...]

        def collect_rows():
            """读取设置窗时间段，返回 [['08:30','12:30'], ...]（空行/非法行忽略）。"""
            wins = []
            for (se, ee) in row_entries:
                s = se.get().strip()
                e = ee.get().strip()
                if not s and not e:
                    continue                       # 空行删除
                if self._water_valid_pair(s, e):
                    wins.append([s, e])
            return wins

        def collect_times():
            """读取时间模式各行，返回合法 ['HH:MM', ...]（去重 + 按时刻排序）。"""
            out = []
            for te in time_entries:
                try:
                    t = (te.get() or "").strip()
                except Exception:
                    continue
                if not t or not self._water_valid_time(t):
                    continue                    # 空行 / 非法时间忽略
                if t not in out:
                    out.append(t)
            out.sort(key=lambda s: (int(s[:2]), int(s[3:])))
            return out

        def refresh_preview():
            """底部只读信息：推导模式显示推导结果，时间模式显示当天提醒时刻。"""
            if mode_var.get() == WATER_MODE_TIME:
                times = collect_times()
                if not times:
                    deriv_label.config(
                        text="未设置提醒时间，喝水提醒暂不生效（时间删空）")
                else:
                    deriv_label.config(
                        text="每天在 {} 提醒（共 {} 次，每天循环）".format(
                            "、".join(times), len(times)))
                return
            try:
                sip_v = int(sip_var.get())
                tgt_v = int(target_var.get())
            except Exception:
                sip_v, tgt_v = self.water_sip, self.water_target
            wins = collect_rows()
            deriv_label.config(text=self._water_derive_preview(
                sip_v, tgt_v, wins))

        # 饮水量变化也实时刷新推导信息
        sip_var.trace_add("write", lambda *a: refresh_preview())
        target_var.trace_add("write", lambda *a: refresh_preview())

        def add_row(s="08:30", e="12:30"):
            row = ttk.Frame(rows_box)
            row.pack(fill="x", pady=2)
            se = ttk.Entry(row, width=6,
                           font=("Microsoft YaHei UI", 10))
            se.insert(0, s)
            se.pack(side="left", padx=(0, 4))
            ttk.Label(row, text="～").pack(side="left")
            ee = ttk.Entry(row, width=6,
                           font=("Microsoft YaHei UI", 10))
            ee.insert(0, e)
            ee.pack(side="left", padx=(4, 6))
            ttk.Button(row, text="删", width=4,
                       command=lambda: (row.destroy(),
                                        row_entries.remove((se, ee)),
                                        refresh_preview())).pack(side="left")
            se.bind("<KeyRelease>", lambda ev: refresh_preview())
            ee.bind("<KeyRelease>", lambda ev: refresh_preview())
            row_entries.append((se, ee))

        for (s, e) in self.water_windows:
            add_row(s, e)
        if not row_entries:
            add_row()

        btn_add = ttk.Button(frm, text="＋添加上班时间段",
                             command=lambda: (add_row(), refresh_preview()))
        btn_add.grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ---- 「按指定时间」模式：直接设置每天的提醒时刻（HH:MM，可多行增删）----
        time_title = ttk.Label(frm, text="提醒时间（HH:MM，可多行）：",
                               font=("Microsoft YaHei UI", 10))
        time_title.grid(row=6, column=0, columnspan=4, sticky="w",
                        pady=(14, 4))
        time_rows_box = ttk.Frame(frm)
        time_rows_box.grid(row=7, column=0, columnspan=4, sticky="w")
        time_entries = []         # [Entry, ...]（每行一个提醒时刻）

        def add_time_row(t="10:00"):
            row = ttk.Frame(time_rows_box)
            row.pack(fill="x", pady=2)
            te = ttk.Entry(row, width=8, font=("Microsoft YaHei UI", 10))
            te.insert(0, t)
            te.pack(side="left", padx=(0, 6))
            ttk.Button(row, text="删", width=4,
                       command=lambda: (row.destroy(),
                                        time_entries.remove(te),
                                        refresh_preview())).pack(side="left")
            te.bind("<KeyRelease>", lambda ev: refresh_preview())
            time_entries.append(te)

        for t in self.water_times:
            add_time_row(t)
        if not time_entries:
            add_time_row()

        btn_add_time = ttk.Button(frm, text="＋添加提醒时间",
                                  command=lambda: (add_time_row(),
                                                   refresh_preview()))
        btn_add_time.grid(row=8, column=0, columnspan=4, sticky="w",
                          pady=(6, 0))

        # 两种模式各自可见的控件（切换时整体收起/展开）
        derive_only.extend([sip_lbl, spin_sip, tgt_lbl, spin_tgt,
                            win_title, rows_box, btn_add])
        time_only.extend([time_title, time_rows_box, btn_add_time])

        # 提醒文案（空 = 默认）
        ttk.Label(frm, text="提醒文案（空=默认）：").grid(
            row=9, column=0, sticky="e", pady=(14, 4), padx=(0, 6))
        msg_var = tk.StringVar(value=self.water_message)
        msg_entry = ttk.Entry(frm, textvariable=msg_var, width=30,
                              font=("Microsoft YaHei UI", 10))
        msg_entry.grid(row=9, column=1, columnspan=3, sticky="w", pady=(14, 4))

        # 自动消失：勾选后气泡在设定秒数后自动关闭（默认不勾选）
        autoclose_var = tk.BooleanVar(value=self.water_autoclose)
        ttk.Checkbutton(frm, text="喝水提示框自动消失", variable=autoclose_var,
                        command=lambda: autoc_close_toggle()
                        ).grid(row=10, column=0, columnspan=2, sticky="w",
                               pady=(4, 0))
        ttk.Label(frm, text="消失秒数：").grid(
            row=10, column=2, sticky="e", padx=(0, 4))
        acsec_var = tk.IntVar(value=self.water_autoclose_seconds)
        acsec_spin = ttk.Spinbox(frm, from_=WATER_AUTOCLOSE_MIN,
                                 to=WATER_AUTOCLOSE_MAX,
                                 textvariable=acsec_var, width=6,
                                 font=("Microsoft YaHei UI", 10))
        acsec_spin.grid(row=10, column=3, sticky="w")

        def autoc_close_toggle():
            # 勾选自动消失时启用秒数输入，否则置灰
            try:
                state = "normal" if autoclose_var.get() else "disabled"
                acsec_spin.config(state=state)
            except Exception:
                pass

        autoc_close_toggle()   # 初始状态

        # 只读信息（实时刷新）：推导模式 = 预计间隔；时间模式 = 当天提醒时刻
        deriv_label = ttk.Label(frm, text="", foreground="#1a73e8",
                                wraplength=dpi_px(dlg, 420), justify="left")
        deriv_label.grid(row=11, column=0, columnspan=4, sticky="w", pady=(10, 6))
        on_mode_change()   # 按当前提醒方式显示对应区域，并刷新只读信息

        btn_save = ttk.Button(frm, text="完成（保存并关闭）",
                              command=lambda: self._water_save_settings(
                                  dlg, enabled_var, sip_var, target_var,
                                  msg_var, row_entries,
                                  autoclose_var, acsec_var,
                                  mode_var, time_entries))
        btn_save.grid(row=12, column=0, columnspan=4, pady=(12, 0))

        dlg.protocol("WM_DELETE_WINDOW", lambda: self._water_save_settings(
            dlg, enabled_var, sip_var, target_var, msg_var, row_entries,
            autoclose_var, acsec_var, mode_var, time_entries))

        # 自适应尺寸：按内容自然尺寸贴合并夹紧到主屏工作区（不再写死 / 越屏）
        box.bind_wheel()
        fit_window_to_content(dlg, frm, min_w=dpi_px(dlg, 460),
                              min_h=dpi_px(dlg, 320), y_div=3)

    def _water_save_settings(self, dlg, enabled_var, sip_var, target_var,
                             msg_var, row_entries,
                             autoclose_var=None, acsec_var=None,
                             mode_var=None, time_entries=None):
        try:
            sip = int(sip_var.get())
        except Exception:
            sip = self.water_sip
        sip = max(WATER_SIP_MIN, min(WATER_SIP_MAX, sip))
        try:
            target = int(target_var.get())
        except Exception:
            target = self.water_target
        target = max(WATER_TARGET_MIN, min(WATER_TARGET_MAX, target))

        wins = []
        for (se, ee) in row_entries:
            s = se.get().strip()
            e = ee.get().strip()
            if not s and not e:                  # 空行删除
                continue
            if self._water_valid_pair(s, e):     # 非法时间直接忽略不写
                wins.append([s, e])

        # 提醒方式 + 指定时间点（时间行在两种模式下都收集，来回切换不丢已填内容）
        mode = WATER_MODE_DERIVE
        if mode_var is not None:
            try:
                mode = str(mode_var.get() or "")
            except Exception:
                mode = WATER_MODE_DERIVE
        if mode not in (WATER_MODE_DERIVE, WATER_MODE_TIME):
            mode = WATER_MODE_DERIVE
        times = []
        if time_entries is not None:
            for te in time_entries:
                try:
                    t = (te.get() or "").strip()
                except Exception:
                    continue
                if not t or not self._water_valid_time(t):
                    continue                     # 空行 / 非法时刻直接忽略不写
                if t not in times:
                    times.append(t)
            times.sort(key=lambda s: (int(s[:2]), int(s[3:])))

        self.water_enabled = bool(enabled_var.get())
        self.water_sip = sip
        self.water_target = target
        self.water_windows = wins                # 删空 => 提醒失效（设置窗已有提示）
        self.water_mode = mode                   # derive / time
        self.water_times = times                 # 时间模式下删空 => 不提醒
        self.water_message = (msg_var.get() or "")
        # 自动消失：勾选状态 + 秒数（未勾选时沿用原值即可；勾选时读取秒数并钳制）
        if autoclose_var is not None:
            self.water_autoclose = bool(autoclose_var.get())
            if self.water_autoclose and acsec_var is not None:
                try:
                    self.water_autoclose_seconds = max(
                        WATER_AUTOCLOSE_MIN,
                        min(WATER_AUTOCLOSE_MAX, int(acsec_var.get())))
                except Exception:
                    pass
        self._save_config()
        try:
            dlg.destroy()
        except Exception:
            pass

    # ---------------------------------------------------------- 托盘与窗口显隐

    def _start_tray(self):
        tray = WinTray()
        # 后台消息线程只写事件队列，不直接调 Tk；这里不再绑定 Tk 回调。
        # 事件由主线程 _ui_poll 统一轮询消费（见 _drain_tray_events）。
        self._tray = tray
        self._pump_job = None
        # 喝水提醒调度线程（与休息倒计时完全独立，全屏照弹）
        self._start_water_scheduler()

    def _drain_tray_events(self):
        # 主线程消费托盘事件队列（安全操作 Tk）
        try:
            tray = self._tray
            if tray is None:
                return
            while True:
                ev = tray._events.get_nowait()
                if ev == "show":
                    self._show_main()
                elif ev == "quit":
                    self._quit()
                elif ev == "unlock":
                    # 锁屏后重新登录（解锁/任意账号登录）：仅显示置顶界面，不弹冒泡
                    self._show_main_after_lock()
        except queue.Empty:
            pass
        except Exception:
            pass

    def _pump_tray(self):
        # 保留空实现（兼容旧调用），消息泵已在后台线程运行。
        if getattr(self, "_quitting", False):
            return

    def _show_main(self):
        # 从托盘恢复主窗口。withdrawn 状态下直接 focus_force 在部分 Windows/Tk 组合
        # 上行为异常（可能导致窗口状态错乱、看起来像"点开就退出"），先 restore 再聚焦。
        # 注意：root.state() 是查询方法，不能设置状态；恢复用 deiconify()。
        try:
            cur = "normal"
            try:
                cur = self.root.state()
            except Exception:
                cur = "normal"
            if cur in ("withdrawn", "iconic"):
                self.root.deiconify()   # 从 withdrawn/iconic 安全恢复为可见
            self.root.lift()
            # Windows 锁屏/解锁会重置窗口的置顶层级，恢复显示后需重新应用置顶，
            # 否则解锁弹回的主界面不会置顶（需求：未开始倒计时时界面要醒目置顶）。
            self._apply_topmost()
            self.root.after_idle(lambda: self._safe_focus())
        except Exception:
            pass

    def _safe_focus(self):
        try:
            if self.root.state() != "withdrawn":
                self.root.focus_force()
        except Exception:
            pass

    def _hide_to_tray(self):
        """点关闭按钮或最小化：缩到托盘；未开始状态弹气泡提醒。"""
        if self.root.state() == "withdrawn":
            return
        self.root.withdraw()
        if self.state == self.STATE_IDLE:
            self._notify("倒计时未开始")

    def _on_unmap(self, event):
        # 用户点标题栏「最小化」也隐藏到托盘（withdraw 自身触发的 Unmap 已排除）。
        # 用事件延迟到安全时刻再隐藏，避免与 withdraw/deiconify 的状态竞争，
        # 防止"从托盘恢复时又被误判为最小化而隐藏"导致看起来像退出的问题。
        if event.widget is self.root:
            if self.root.state() == "iconic":
                # 最小化 -> 缩到托盘
                self.root.after_idle(self._hide_to_tray)
            elif self.root.state() == "withdrawn":
                pass  # 自身 withdraw 触发的 Unmap,忽略
            else:
                pass

    def _notify(self, message):
        if self._tray is not None:
            self._tray.notify(message, APP_TITLE)

    def _quit(self):
        # 正常退出：只通过托盘菜单「退出」或主窗口销毁触发。
        # 彻底停止所有后台线程（托盘消息线程 + 倒计时线程），并安全销毁主窗口。
        if getattr(self, "_quitting", False):
            return
        self._quitting = True
        self._save_config()
        # 停止倒计时线程
        if self._tick_stop is not None:
            self._tick_stop.set()
        self._tick_stop = None
        # 停止喝水调度线程
        if self._water_stop is not None:
            self._water_stop.set()
        self._water_stop = None
        # 关闭可能残留的水气泡窗口（销毁根窗口前，避免 after 回调访问已销毁对象）
        self._close_water_bubbles()
        # 停止 UI 轮询
        if self._ui_poll_job is not None:
            try:
                self.root.after_cancel(self._ui_poll_job)
            except Exception:
                pass
        self._ui_poll_job = None
        # 停止托盘后台线程并移除图标
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        try:
            # 兜底：销毁根窗口前先关闭后台线程，避免其 after 回调访问已销毁对象
            if self._tick_thread is not None and self._tick_thread.is_alive():
                self._tick_thread.join(timeout=1.0)
        except Exception:
            pass
        self._tick_thread = None
        try:
            if self._water_thread is not None and self._water_thread.is_alive():
                self._water_thread.join(timeout=1.0)
        except Exception:
            pass
        self._water_thread = None
        # 兜底：关闭可能残留的全屏遮罩窗口
        self._close_fullscreen()
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass


def tkinter_ok():
    """返回 True：Tk 还活着（pump 轮询用）。"""
    return True


def main():
    # 单实例控制：已有实例在跑 -> 恢复其界面显示后本进程直接退出
    if not acquire_single_instance():
        try:
            if activate_existing_instance():
                return     # 已唤醒旧实例，本进程退出
        except Exception:
            pass

    enable_dpi_awareness()
    root = tk.Tk()
    app = BreakReminderApp(root)
    if os.environ.get("BREAK_REMINDER_SMOKE"):
        # 冒烟测试：短暂启动，创建托盘 + 启动一次倒计时，然后自动退出
        root.after(400, lambda: (app._start_countdown(),
                                 print("SMOKE: countdown started")))
        root.after(1200, lambda: (print("SMOKE: remaining=" + repr(app.remaining)),
                                  app._quit()))
        root.after(4000, lambda: (print("SMOKE: ERROR window did not close"),
                                  os._exit(3)))
    root.mainloop()


if __name__ == "__main__":
    main()
