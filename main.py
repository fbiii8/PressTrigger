"""PressTrigger v2.2 入口

1. 需要管理员权限（低层输入钩子 + 向提权窗口投递输入都需要）。
   未提权时自动通过 UAC 重新启动自己，避免"点了没反应"。
2. 命名互斥体单实例锁。
3. 启动失败（钩子安装不了）时弹窗说明原因，而不是静默退出。
"""

import ctypes
import sys
from ctypes import wintypes

from constants import APP_NAME, VERSION


def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """以管理员身份重启自己；已取消 UAC 或失败返回 False"""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = ""
    else:
        exe = sys.executable
        params = " ".join(f'"{a}"' for a in [sys.argv[0], *sys.argv[1:]])

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = (wintypes.HWND, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, ctypes.c_int)
    shell32.ShellExecuteW.restype = ctypes.c_void_p

    result = shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    return int(result) > 32


def show_error(message, title=f"{APP_NAME} 提示"):
    try:
        _user32().MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
    except Exception:
        print(f"{title}: {message}", file=sys.stderr)


def main():
    if sys.platform != "win32":
        show_error("PressTrigger 仅支持 Windows。")
        return 1

    if not is_admin():
        if relaunch_as_admin():
            return 0
        show_error("需要管理员权限才能监听并模拟按键。\n"
                   "请右键程序 -> 以管理员身份运行。")

    # 单实例锁必须在提权之后再判断，否则父进程和提权子进程会互相误判
    from config import acquire_instance_lock
    if acquire_instance_lock() is None:
        _user32().MessageBoxW(None, f"{APP_NAME} 已在运行。", "提示", 0x40)
        return 0

    try:
        from gui import App
    except Exception as exc:                       # pragma: no cover
        show_error(f"初始化失败：{exc}")
        return 1

    try:
        App().run()
    except Exception as exc:                       # pragma: no cover
        show_error(f"运行出错：{exc}\n\n版本 {VERSION}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
