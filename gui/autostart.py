# -*- coding: utf-8 -*-
"""Windows 开机自启（注册表 HKCU Run 项）。

只操作「当前用户」的 Run 键，不需要管理员权限。
  - enable():  写入一条指向 exe 的启动项，带 --minimized 静默参数
  - disable(): 删除该启动项
  - is_enabled(): 查询当前是否已注册、且指向本程序

非 Windows 平台一律安全降级（返回 False），不抛异常。
exe 路径以 sys.executable 为准；源码运行时用 run_gui.py 的绝对路径兜底。
"""
import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "PDDReviewHelper"
FLAG = "--minimized"


def _winreg():
    if sys.platform != "win32":
        return None
    try:
        import winreg
        return winreg
    except Exception:
        return None


def exe_command():
    """返回写进注册表的启动命令（带引号的路径 + 静默参数）。"""
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
    else:
        exe = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "run_gui.py"))
    quoted = '"%s"' % exe
    if not getattr(sys, "frozen", False):
        # 源码运行需要 python 解释器
        quoted = '"%s" "%s"' % (sys.executable, exe)
    return "%s %s" % (quoted, FLAG)


def is_enabled():
    wr = _winreg()
    if not wr:
        return False
    try:
        key = wr.OpenKey(wr.HKEY_CURRENT_USER, RUN_KEY)
        try:
            try:
                val, _ = wr.QueryValueEx(key, APP_NAME)
            except OSError:
                return False
            # 只认指向本程序的项，避免误判同名
            return APP_NAME in str(val) or "run_gui.py" in str(val) or \
                "PDD_Review_Helper" in str(val) or "评价举报" in str(val)
        finally:
            wr.CloseKey(key)
    except Exception:
        return False


def enable():
    wr = _winreg()
    if not wr:
        return False, "仅支持 Windows 开机自启"
    try:
        key = wr.OpenKey(wr.HKEY_CURRENT_USER, RUN_KEY, 0, wr.KEY_SET_VALUE)
        try:
            wr.SetValueEx(key, APP_NAME, 0, wr.REG_SZ, exe_command())
        finally:
            wr.CloseKey(key)
        return True, "已设置开机自启"
    except Exception as e:
        return False, "设置失败：%s" % str(e)[:120]


def disable():
    wr = _winreg()
    if not wr:
        return False, "仅支持 Windows 开机自启"
    try:
        key = wr.OpenKey(wr.HKEY_CURRENT_USER, RUN_KEY, 0, wr.KEY_SET_VALUE)
        try:
            try:
                wr.DeleteValue(key, APP_NAME)
            except OSError:
                pass  # 本来就不存在，视作成功
        finally:
            wr.CloseKey(key)
        return True, "已取消开机自启"
    except Exception as e:
        return False, "取消失败：%s" % str(e)[:120]
