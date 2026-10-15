"""剪贴板。

为什么不直接用 pyautogui.typewrite:它走的是键盘事件模拟,只能处理 ASCII,
遇到中文就静默丢字符。而中文偏偏是本机操作里最高频的需求。
所以中文一律走剪贴板粘贴,ASCII 短文本才直接敲,速度更快也更不容易被输入法干扰。

这里手写各平台实现而不是引入 pyperclip,是为了少一个依赖、少一层失败可能。
"""
from __future__ import annotations

import subprocess
import sys


def _copy_win(text: str) -> bool:
    import ctypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32

    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        # +1 留给终止符,GlobalAlloc 按字节算,UTF-16 一个字符两字节
        buf = ctypes.create_unicode_buffer(text)
        nbytes = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, nbytes)
        if not handle:
            return False
        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(ctypes.c_wchar_p(locked), buf, nbytes)
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


def _copy_mac(text: str) -> bool:
    p = subprocess.run(["pbcopy"], input=text.encode("utf-8"), capture_output=True)
    return p.returncode == 0


def _copy_linux(text: str) -> bool:
    for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        try:
            p = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True)
            if p.returncode == 0:
                return True
        except FileNotFoundError:
            continue
    return False


def copy_to_clipboard(text: str) -> bool:
    if sys.platform.startswith("win"):
        return _copy_win(text)
    if sys.platform == "darwin":
        return _copy_mac(text)
    return _copy_linux(text)
