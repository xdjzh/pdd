# -*- coding: utf-8 -*-
"""
GUI 入口。

源码运行：  python run_gui.py
打包运行：  PDD评价举报助手.exe

PyInstaller --windowed 下没有控制台，sys.stdout / sys.stderr 会是 None，
核心脚本里大量 print() 会直接抛异常，所以这里统一重定向到 pdd_data/gui.log。
"""
import os
import sys


def _prepare_frozen_env():
    frozen = getattr(sys, "frozen", False)
    if not frozen:
        return
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))

    # 若打包时嵌入了 Chromium 内核，让 Playwright 从解压目录里找浏览器
    bundled_browsers = os.path.join(base, "ms-playwright")
    if os.path.isdir(bundled_browsers):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", bundled_browsers)

    # Windows 无控制台时，给 Playwright 启动的 driver 子进程加 CREATE_NO_WINDOW，
    # 否则每开一个店铺都可能闪一下黑框
    if sys.platform == "win32":
        try:
            import subprocess
            _orig = subprocess.Popen
            _flag = 0x08000000  # CREATE_NO_WINDOW

            class _SilentPopen(_orig):
                def __init__(self, *args, **kwargs):
                    kwargs["creationflags"] = kwargs.get("creationflags", 0) | _flag
                    _orig.__init__(self, *args, **kwargs)

            subprocess.Popen = _SilentPopen
        except Exception:
            pass


def _redirect_output():
    try:
        from gui.state import ShopStore
        log = ShopStore().gui_log
    except Exception:
        # exe 放在只读目录（如 Program Files）时，退到系统临时目录
        import tempfile
        log = os.path.join(tempfile.gettempdir(), "pdd_review_gui.log")
    try:
        f = open(log, "a", encoding="utf-8", buffering=1)
        f.write("\n" + "=" * 60 + "\n启动 %s\n" %
                __import__("time").strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        f = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def _check_deps():
    try:
        import tkinter  # noqa: F401
        import playwright  # noqa: F401
        return True, ""
    except ImportError as e:
        return False, str(e)


def main():
    # 热更新必须是第一件事：_redirect_output() 会 import gui.state，
    # 一旦 gui 包被加载，它的 __path__ 就锁死在 _MEIPASS，补丁再也覆盖不了。
    try:
        import updater
        updater.apply_update()
    except Exception:
        pass  # 断网 / 云端异常一律静默降级到 exe 内置代码
    _prepare_frozen_env()
    _redirect_output()
    ok, err = _check_deps()
    if not ok:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "缺少依赖",
                "程序缺少依赖：%s\n\n"
                "源码运行请先执行：pip install -r requirements_gui.txt\n"
                "打包版请重新运行 build_exe.bat 完整构建。" % err)
            root.destroy()
        except Exception:
            print("缺少依赖: %s" % err)
        return 1

    # 授权门禁：未登录 / 未激活 / 已吊销 / 已到期时，主界面代码不会加载。
    # 仅「源码运行 + 显式 PDD_DEV_MODE=1」可跳过，方便本机验收界面；
    # 打包后的 exe 里 sys.frozen=True，环境变量一律无效。
    dev_mode = (not getattr(sys, "frozen", False) and
                os.environ.get("PDD_DEV_MODE") == "1")
    try:
        if dev_mode:
            print("本地开发模式：跳过授权门禁（分发版 exe 不受影响）")
            session = None
        else:
            from gui.license import ensure_license
            session = ensure_license()
    except Exception as e:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("授权校验失败", str(e)[:300])
            root.destroy()
        except Exception:
            print("授权校验失败: %s" % e)
        return 1
    if not session:
        return 1

    from gui.app import main as gui_main
    gui_main(license_session=session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
