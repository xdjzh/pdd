# -*- coding: utf-8 -*-
"""右下角系统托盘图标（Windows 通知区域）——纯标准库 ctypes 实现。

为什么不用 pystray / infi.systray：
    本程序靠热更新分发，补丁只能替换 .py 文件、装不了新的 pip 包。
    纯 ctypes 只依赖标准库，老 exe 装上补丁立刻就能用。

结构：
    · 独立线程创建隐藏窗口 + 自己的消息循环（Tk 主线程不能被阻塞）；
    · Shell_NotifyIconW 把图标挂到右下角（时钟旁边，不在任务栏）；
    · 右键弹原生菜单：打开主界面 / 锁定 / 设置 / 检测版本 / 退出；
    · 左键单击或双击 = 打开主界面；
    · 回调只往队列里塞事件字符串，由 Tk 线程取出后执行，
      绝不在托盘线程里碰 tkinter（跨线程操作 tk 会随机崩溃）。

非 Windows 平台 start() 直接返回 False，调用方自动退回「最小化到任务栏」。
"""
import ctypes
import os
import queue
import sys
import threading
import time

IS_WINDOWS = sys.platform.startswith("win")

# ---- 消息常量 ----
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_APP = 0x8000
WM_TRAY = WM_APP + 1

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO = 0x01

MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0000, 0x0800, 0x0001
TPM_RETURNCMD, TPM_RIGHTBUTTON, TPM_NONOTIFY = 0x0100, 0x0002, 0x0080
IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x0010, 0x0040

# 菜单项 id → 事件名（事件由 Tk 线程消费）
MENU_OPEN, MENU_LOCK, MENU_SETTINGS, MENU_UPDATE, MENU_QUIT = 1, 2, 3, 4, 5

_API = {}


def _build_api():
    """把用到的 Win32 函数/结构一次性准备好（只在 Windows 上调用）。"""
    if _API:
        return _API
    from ctypes import wintypes as wt

    HANDLE = ctypes.c_void_p
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HANDLE, wt.UINT,
                                 ctypes.c_size_t, ctypes.c_ssize_t)

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD),
                    ("Data3", wt.WORD), ("Data4", ctypes.c_byte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        # Vista+ 完整结构；cbSize 用 sizeof，Explore 各版本都能吃
        _fields_ = [
            ("cbSize", wt.DWORD),
            ("hWnd", HANDLE),
            ("uID", wt.UINT),
            ("uFlags", wt.UINT),
            ("uCallbackMessage", wt.UINT),
            ("hIcon", HANDLE),
            ("szTip", wt.WCHAR * 128),
            ("dwState", wt.DWORD),
            ("dwStateMask", wt.DWORD),
            ("szInfo", wt.WCHAR * 256),
            ("uVersion", wt.UINT),
            ("szInfoTitle", wt.WCHAR * 64),
            ("dwInfoFlags", wt.DWORD),
            ("guidItem", GUID),
            ("hBalloonIcon", HANDLE),
        ]

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.UINT), ("style", wt.UINT),
            ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", HANDLE),
            ("hIcon", HANDLE), ("hCursor", HANDLE),
            ("hbrBackground", HANDLE), ("lpszMenuName", wt.LPCWSTR),
            ("lpszClassName", wt.LPCWSTR), ("hIconSm", HANDLE),
        ]

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    def sig(fn, restype, argtypes):
        fn.restype = restype
        fn.argtypes = argtypes
        return fn

    sig(kernel32.GetModuleHandleW, HANDLE, [wt.LPCWSTR])
    sig(kernel32.GetCurrentThreadId, wt.DWORD, [])
    sig(kernel32.GetLastError, wt.DWORD, [])

    sig(user32.RegisterClassExW, wt.WORD, [ctypes.POINTER(WNDCLASSEXW)])
    sig(user32.CreateWindowExW, HANDLE,
        [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
         HANDLE, HANDLE, HANDLE, HANDLE])
    sig(user32.DefWindowProcW, LRESULT,
        [HANDLE, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t])
    sig(user32.DestroyWindow, wt.BOOL, [HANDLE])
    sig(user32.GetMessageW, ctypes.c_int,
        [ctypes.POINTER(wt.MSG), HANDLE, wt.UINT, wt.UINT])
    sig(user32.TranslateMessage, wt.BOOL, [ctypes.POINTER(wt.MSG)])
    sig(user32.DispatchMessageW, LRESULT, [ctypes.POINTER(wt.MSG)])
    sig(user32.PostQuitMessage, None, [ctypes.c_int])
    sig(user32.PostMessageW, wt.BOOL,
        [HANDLE, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t])
    sig(user32.RegisterWindowMessageW, wt.UINT, [wt.LPCWSTR])
    sig(user32.SetForegroundWindow, wt.BOOL, [HANDLE])
    sig(user32.CreatePopupMenu, HANDLE, [])
    sig(user32.AppendMenuW, wt.BOOL,
        [HANDLE, wt.UINT, ctypes.c_size_t, wt.LPCWSTR])
    sig(user32.DestroyMenu, wt.BOOL, [HANDLE])
    sig(user32.TrackPopupMenu, ctypes.c_int,
        [HANDLE, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
         HANDLE, HANDLE])
    sig(user32.GetCursorPos, wt.BOOL, [ctypes.POINTER(wt.POINT)])
    sig(user32.LoadImageW, HANDLE,
        [HANDLE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT])
    sig(user32.LoadIconW, HANDLE, [HANDLE, HANDLE])
    sig(user32.DestroyIcon, wt.BOOL, [HANDLE])

    sig(shell32.Shell_NotifyIconW, wt.BOOL,
        [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)])
    sig(shell32.ExtractIconExW, wt.UINT,
        [wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(HANDLE),
         ctypes.POINTER(HANDLE), wt.UINT])

    _API.update({
        "HANDLE": HANDLE, "WNDPROC": WNDPROC, "MSG": wt.MSG,
        "POINT": wt.POINT, "NOTIFYICONDATAW": NOTIFYICONDATAW,
        "WNDCLASSEXW": WNDCLASSEXW,
        "user32": user32, "shell32": shell32, "kernel32": kernel32,
        "GetModuleHandleW": kernel32.GetModuleHandleW,
        "GetCurrentThreadId": kernel32.GetCurrentThreadId,
        "GetLastError": kernel32.GetLastError,
        "RegisterClassExW": user32.RegisterClassExW,
        "CreateWindowExW": user32.CreateWindowExW,
        "DefWindowProcW": user32.DefWindowProcW,
        "DestroyWindow": user32.DestroyWindow,
        "GetMessageW": user32.GetMessageW,
        "TranslateMessage": user32.TranslateMessage,
        "DispatchMessageW": user32.DispatchMessageW,
        "PostQuitMessage": user32.PostQuitMessage,
        "PostMessageW": user32.PostMessageW,
        "RegisterWindowMessageW": user32.RegisterWindowMessageW,
        "SetForegroundWindow": user32.SetForegroundWindow,
        "CreatePopupMenu": user32.CreatePopupMenu,
        "AppendMenuW": user32.AppendMenuW,
        "DestroyMenu": user32.DestroyMenu,
        "TrackPopupMenu": user32.TrackPopupMenu,
        "GetCursorPos": user32.GetCursorPos,
        "LoadImageW": user32.LoadImageW,
        "LoadIconW": user32.LoadIconW,
        "DestroyIcon": user32.DestroyIcon,
        "Shell_NotifyIconW": shell32.Shell_NotifyIconW,
        "ExtractIconExW": shell32.ExtractIconExW,
    })
    return _API


def _icon_candidates(icon_path=None):
    """图标文件候选：调用方给的路径 → exe 同级 app.ico → 打包解压目录。"""
    out = []
    if icon_path:
        out.append(icon_path)
    base = os.path.dirname(os.path.abspath(sys.executable))
    out.append(os.path.join(base, "app.ico"))
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        out.append(os.path.join(meipass, "app.ico"))
    here = os.path.dirname(os.path.abspath(__file__))
    out.append(os.path.join(os.path.dirname(here), "app.ico"))
    return [p for p in out if p and os.path.isfile(p)]


class TrayIcon(object):
    """右下角托盘图标。线程安全：start/stop/set_tip/notify 可在任意线程调。"""

    def __init__(self, title="PDD 工作台", icon_path=None, log=None):
        self.title = title or "PDD 工作台"
        self.icon_path = icon_path
        self._log_fn = log
        self.events = queue.Queue()
        self._api = None
        self._thread = None
        self._hwnd = None
        self._hinst = None
        self._hicon = None
        self._nid = None
        self._proc = None
        self._cls_name = ""
        self._taskbar_msg = 0
        self._ok = False
        self._error = ""
        self._stopping = False
        self._lock = threading.RLock()
        self._tip = self.title[:127]
        self._locked = False
        self._added = False           # 图标当前是否已挂在通知区域

    # ---------- 生命周期 ----------
    def start(self, wait=3.0):
        """启动托盘线程；成功返回 True。失败不抛异常，调用方退回任务栏方案。"""
        if not IS_WINDOWS:
            self._log("非 Windows 平台，跳过托盘图标")
            return False
        with self._lock:
            if self._ok:
                return True
            if self._thread and self._thread.is_alive():
                return self._ok
            try:
                self._api = _build_api()
            except Exception as e:
                self._log("加载 Win32 接口失败：%s" % e)
                return False
            self._error = ""
            self._stopping = False
            self._added = False       # 重试时清掉上次的挂载标记
            self._hwnd = None
            self._thread = threading.Thread(target=self._run, name="pdd-tray",
                                            daemon=True)
            self._thread.start()
        t0 = time.time()
        while time.time() - t0 < max(0.5, wait):
            if self._ok or self._error:
                break
            time.sleep(0.05)
        if self._ok:
            self._log("托盘图标已就绪")
        else:
            self._log("托盘图标启动失败：%s" % (self._error or "超时"))
        return self._ok

    def stop(self, wait=1.5):
        """移除图标并结束消息循环（退出程序前必须调用，否则会留幽灵图标）。"""
        self._stopping = True
        hwnd = self._hwnd
        try:
            if hwnd and self._api:
                self._api["PostMessageW"](hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=wait)
        self._ok = False
        try:
            self._delete_icon()
        except Exception:
            pass

    def is_running(self):
        return bool(self._ok)

    def pop_events(self):
        """取出所有待处理事件（由 Tk 线程调用）。"""
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    # ---------- 标题 / 气泡 ----------
    def set_tip(self, text):
        text = (text or self.title)[:127]
        with self._lock:
            self._tip = text
            if not self._ok or not self._api:
                return
            try:
                nid = self._make_nid(flags=NIF_TIP)
                self._api["Shell_NotifyIconW"](NIM_MODIFY, ctypes.byref(nid))
            except Exception as e:
                self._log("更新托盘提示失败：%s" % e)

    def notify(self, title, text):
        """气泡提示（系统关了通知就静默失败，不影响主流程）。"""
        with self._lock:
            if not self._ok or not self._api:
                return
            try:
                nid = self._make_nid(flags=NIF_INFO)
                nid.szInfoTitle = str(title or "")[:63]
                nid.szInfo = str(text or "")[:255]
                nid.dwInfoFlags = NIIF_INFO
                self._api["Shell_NotifyIconW"](NIM_MODIFY, ctypes.byref(nid))
            except Exception as e:
                self._log("气泡提示失败：%s" % e)

    def set_locked(self, locked):
        """锁定状态只影响右键菜单里的「锁定 / 解锁」文案。"""
        self._locked = bool(locked)

    # ---------- 内部 ----------
    def _log(self, msg):
        if not self._log_fn:
            return
        try:
            self._log_fn(str(msg))
        except Exception:
            pass

    def _last_error(self):
        """真实 Win32 错误码（ctypes.get_last_error 未开 use_last_error 时恒为 0）。"""
        try:
            code = int(self._api["GetLastError"]())
        except Exception:
            code = 0
        try:
            return "%d(%s)" % (code, ctypes.FormatError(code).strip())
        except Exception:
            return str(code)

    @property
    def last_error(self):
        return self._error

    def _make_nid(self, flags=None):
        api = self._api
        nid = api["NOTIFYICONDATAW"]()
        nid.cbSize = ctypes.sizeof(api["NOTIFYICONDATAW"])
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = (NIF_MESSAGE | NIF_ICON | NIF_TIP) if flags is None else flags
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self._tip
        return nid

    def _load_icon(self):
        api = self._api
        # 1) 从自身 exe 里取图标（和桌面快捷方式同一个）
        try:
            exe = os.path.abspath(sys.executable)
            large = api["HANDLE"]()
            small = api["HANDLE"]()
            n = api["ExtractIconExW"](exe, 0, ctypes.byref(large),
                                      ctypes.byref(small), 1)
            if n and small.value:
                self._log("托盘图标来源：exe 小图标")
                return small
            if n and large.value:
                self._log("托盘图标来源：exe 大图标")
                return large
        except Exception as e:
            self._log("提取 exe 图标失败：%s" % e)
        # 2) app.ico 文件
        for path in _icon_candidates(self.icon_path):
            try:
                h = api["LoadImageW"](None, path, IMAGE_ICON, 16, 16,
                                      LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if h:
                    self._log("托盘图标来源：%s" % path)
                    return h
            except Exception:
                continue
        # 3) 系统默认图标（蓝色信息图标）：保证 hIcon 不为空，否则 NIM_ADD 必失败
        try:
            h = api["LoadIconW"](None,
                                 ctypes.cast(ctypes.c_void_p(32512),
                                             api["HANDLE"]))
            self._log("托盘图标来源：系统默认图标%s" % ("" if h else "（也失败了）"))
            return h
        except Exception as e:
            self._log("系统默认图标也失败：%s" % e)
            return None

    def _add_icon(self, tries=2):
        """挂图标。explorer 刚启动时 NIM_ADD 常返回假失败，重试几次。"""
        api = self._api
        if self._hicon is None:
            self._hicon = self._load_icon()
        if self._added:
            return True
        if not self._hicon:
            self._error = "取不到任何图标句柄（exe/ico/系统图标都失败）"
            return False
        last = ""
        for i in range(max(1, tries)):
            nid = self._make_nid()
            try:
                if api["Shell_NotifyIconW"](NIM_ADD, ctypes.byref(nid)):
                    self._nid = nid
                    self._added = True
                    return True
            except Exception as e:
                last = str(e)
            last = self._last_error() if not last else last
            if i + 1 < tries:
                time.sleep(0.25)
        self._error = "Shell_NotifyIcon 挂图标失败：%s" % last
        self._log(self._error)
        return False

    def _delete_icon(self):
        api = self._api
        if not api or self._hwnd is None or not self._added:
            return
        try:
            nid = self._make_nid(flags=NIF_MESSAGE)
            api["Shell_NotifyIconW"](NIM_DELETE, ctypes.byref(nid))
        except Exception:
            pass
        self._added = False

    def _create_window(self, ex_style, parent=None):
        api = self._api
        try:
            hwnd = api["CreateWindowExW"](
                ex_style, self._cls_name, self._cls_name, 0, 0, 0, 0, 0,
                parent, None, self._hinst, None)
        except Exception as e:
            self._log("CreateWindowExW(0x%x) 异常：%s" % (ex_style, e))
            return None
        if not hwnd:
            self._log("CreateWindowExW(0x%x) 失败：%s"
                      % (ex_style, self._last_error()))
            return None
        return hwnd

    def _run(self):
        api = self._api
        try:
            self._hinst = api["GetModuleHandleW"](None)
            self._cls_name = "PDDTrayWnd_%d_%x" % (os.getpid(), id(self))
            self._proc = api["WNDPROC"](self._wndproc)
            wc = api["WNDCLASSEXW"]()
            wc.cbSize = ctypes.sizeof(api["WNDCLASSEXW"])
            wc.lpfnWndProc = self._proc
            wc.hInstance = self._hinst
            wc.lpszClassName = self._cls_name
            atom = api["RegisterClassExW"](ctypes.byref(wc))
            if not atom:
                # 类名可能被同进程上一个实例占着（改密码/重试启动），换个名字再试
                self._cls_name += "_%d" % int(time.time() * 1000 % 100000)
                wc.lpszClassName = self._cls_name
                atom = api["RegisterClassExW"](ctypes.byref(wc))
            self._log("RegisterClassExW -> %s（%s）"
                      % (atom, "" if atom else self._last_error()))
            if not atom:
                self._error = "RegisterClassExW 失败：%s" % self._last_error()
                return
            try:
                self._taskbar_msg = api["RegisterWindowMessageW"](
                    "TaskbarCreated")
            except Exception:
                self._taskbar_msg = 0

            # 形态 A：隐藏顶层工具窗口（能收到 TaskbarCreated，explorer 重启后能自己补挂）
            # 形态 B：message-only 窗口（收不到广播，但兼容极端窗口管理器/安全软件）
            hwnd = self._create_window(0x80)
            if hwnd and not self._add_icon():
                try:
                    api["DestroyWindow"](hwnd)
                except Exception:
                    pass
                hwnd = None
            if not hwnd:
                msg_only = ctypes.c_void_p(0xFFFFFFFFFFFFFFFD
                                           if ctypes.sizeof(ctypes.c_void_p) == 8
                                           else 0xFFFFFFFD)
                hwnd = self._create_window(0, parent=msg_only)
                if hwnd and not self._add_icon():
                    try:
                        api["DestroyWindow"](hwnd)
                    except Exception:
                        pass
                    hwnd = None
            if not hwnd:
                self._error = self._error or "创建托盘窗口失败"
                self._log("托盘启动失败：%s" % self._error)
                return
            self._hwnd = hwnd
            self._ok = True
            self._error = ""          # 重试成功后清掉中间失败的原因
            self._log("托盘图标挂载成功（hwnd=%s）" % hwnd)
            msg = api["MSG"]()
            while True:
                r = api["GetMessageW"](ctypes.byref(msg), None, 0, 0)
                if r == 0 or r == -1:
                    break
                api["TranslateMessage"](ctypes.byref(msg))
                api["DispatchMessageW"](ctypes.byref(msg))
        except Exception as e:
            self._error = str(e)
            self._log("托盘线程异常：%s" % e)
        finally:
            self._ok = False

    def _wndproc(self, hwnd, msg, wparam, lparam):
        api = self._api
        try:
            if msg == WM_TRAY:
                ev = int(lparam) & 0xFFFF
                if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.events.put("open")
                    return 0
                if ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
                    return 0
                return 0
            if self._taskbar_msg and msg == self._taskbar_msg:
                # 任务栏/资源管理器重启后图标会消失，这里补挂一次
                self._added = False
                self._add_icon()
                return 0
            if msg == WM_CLOSE:
                api["DestroyWindow"](hwnd)
                return 0
            if msg == WM_DESTROY:
                self._delete_icon()
                api["PostQuitMessage"](0)
                return 0
        except Exception as e:
            self._log("托盘消息处理异常：%s" % e)
        return api["DefWindowProcW"](hwnd, msg, wparam, lparam)

    def _show_menu(self):
        api = self._api
        hwnd = self._hwnd
        menu = None
        try:
            menu = api["CreatePopupMenu"]()

            def add(flags, mid, label):
                api["AppendMenuW"](menu, flags, mid, label)

            add(MF_STRING, MENU_OPEN, "打开主界面")
            add(MF_SEPARATOR, 0, None)
            add(MF_STRING, MENU_LOCK, "解锁…" if self._locked else "锁定")
            add(MF_STRING, MENU_SETTINGS, "设置…")
            add(MF_STRING, MENU_UPDATE, "检测版本")
            add(MF_SEPARATOR, 0, None)
            add(MF_STRING, MENU_QUIT, "退出程序")

            pt = api["POINT"]()
            api["GetCursorPos"](ctypes.byref(pt))
            # 不 SetForegroundWindow 的话，点菜单外面菜单不会自动收起
            api["SetForegroundWindow"](hwnd)
            cmd = api["TrackPopupMenu"](
                menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                pt.x, pt.y, 0, hwnd, None)
            api["PostMessageW"](hwnd, WM_NULL, 0, 0)
            mapping = {
                MENU_OPEN: "open",
                MENU_LOCK: "unlock" if self._locked else "lock",
                MENU_SETTINGS: "settings",
                MENU_UPDATE: "update",
                MENU_QUIT: "quit",
            }
            ev = mapping.get(int(cmd) if cmd else 0)
            if ev:
                self.events.put(ev)
        except Exception as e:
            self._log("弹出托盘菜单失败：%s" % e)
        finally:
            if menu:
                try:
                    api["DestroyMenu"](menu)
                except Exception:
                    pass


class FloatingBall(object):
    """托盘图标挂不上时的兜底：屏幕右下角一个悬浮小圆钮。

    为什么还要这个：部分机器（安全软件、explorer 异常）死活挂不上通知区域图标，
    如果这时点 X 只能「最小化到任务栏」，用户会觉得跟没改一样。圆钮是普通 Tk 窗口，
    不依赖通知区域，位置固定在右下角工作区，行为跟托盘一致：
    左键=打开主界面，右键=同款菜单。
    """

    COLOR = "#2B5CE6"
    COLOR_HOVER = "#1F49C4"

    def __init__(self, root, on_open, menu_items=None, log=None, size=42):
        self.root = root
        self.on_open = on_open
        self.menu_items = menu_items or (lambda: [])
        self._log_fn = log
        self.size = int(size)
        self._win = None
        self._canvas = None
        self._on = False

    def _log(self, msg):
        if self._log_fn:
            try:
                self._log_fn("[悬浮球] %s" % msg)
            except Exception:
                pass

    def is_running(self):
        try:
            return bool(self._on and self._win and self._win.winfo_exists())
        except Exception:
            return False

    def _pos(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        right, bottom = sw, sh - 48
        try:
            import ctypes
            from ctypes import wintypes as wt

            class RECT(ctypes.Structure):
                _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                            ("right", wt.LONG), ("bottom", wt.LONG)]

            r = RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(r), 0):
                right, bottom = r.right, r.bottom
        except Exception:
            pass
        return max(0, right - self.size - 12), max(0, bottom - self.size - 12)

    def show(self):
        if self.is_running():
            try:
                self._win.deiconify()
                self._win.lift()
            except Exception:
                pass
            return True
        try:
            import tkinter as tk
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass
            c = tk.Canvas(win, width=self.size, height=self.size,
                          bg=self.COLOR, highlightthickness=0, bd=0,
                          cursor="hand2")
            c.pack()
            pad = 3
            c.create_oval(pad, pad, self.size - pad, self.size - pad,
                          fill=self.COLOR, outline="#FFFFFF", width=2)
            c.create_text(self.size / 2.0, self.size / 2.0 - 3,
                          text="PDD", fill="#FFFFFF",
                          font=("Segoe UI", 10, "bold"))
            c.create_text(self.size / 2.0, self.size / 2.0 + 9,
                          text="工作台", fill="#DCE6FF",
                          font=("Microsoft YaHei", 6))
            c.bind("<Button-1>", lambda e: self._open())
            c.bind("<Button-3>", self._popup)
            c.bind("<Enter>", lambda e: c.configure(bg=self.COLOR_HOVER))
            c.bind("<Leave>", lambda e: c.configure(bg=self.COLOR))
            x, y = self._pos()
            win.geometry("%dx%d+%d+%d" % (self.size, self.size, x, y))
            self._win, self._canvas, self._on = win, c, True
            self._log("已显示右下角悬浮球 (%d,%d)" % (x, y))
            return True
        except Exception as e:
            self._log("创建悬浮球失败：%s" % e)
            self._win = None
            self._on = False
            return False

    def hide(self):
        self._on = False
        try:
            if self._win:
                self._win.destroy()
        except Exception:
            pass
        self._win = None
        self._canvas = None

    def _open(self):
        try:
            self.hide()
        except Exception:
            pass
        try:
            self.on_open()
        except Exception as e:
            self._log("打开主界面失败：%s" % e)

    def _popup(self, event=None):
        try:
            import tkinter as tk
            m = tk.Menu(self.root, tearoff=0)
            for label, cmd in (self.menu_items() or []):
                if label == "-":
                    m.add_separator()
                else:
                    m.add_command(label=label, command=cmd)
            m.tk_popup(event.x_root if event else 0,
                       event.y_root if event else 0)
            try:
                m.grab_release()
            except Exception:
                pass
        except Exception as e:
            self._log("悬浮球菜单失败：%s" % e)
