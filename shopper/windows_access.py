"""Read Windows process integrity before physical input; never elevate a process.

SendInput is limited to processes at the same or a lower integrity level:
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from functools import lru_cache


@lru_cache(maxsize=1)
def _apis():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    security.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    security.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    security.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    return kernel, security


def process_integrity(pid: int) -> int:
    """Return the integrity RID (medium=0x2000, high=0x3000), or raise."""
    if sys.platform != "win32":
        raise OSError("Windows process tokens are unavailable")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("Invalid process id")
    kernel, security = _apis()
    process = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    token = wintypes.HANDLE()
    try:
        if not security.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            raise ctypes.WinError(ctypes.get_last_error())
        length = wintypes.DWORD()
        security.GetTokenInformation(token, 25, None, 0, ctypes.byref(length))  # TokenIntegrityLevel
        if length.value < ctypes.sizeof(ctypes.c_void_p):
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(length.value)
        if not security.GetTokenInformation(token, 25, buffer, length.value, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_MANDATORY_LABEL begins with SID_AND_ATTRIBUTES.Sid.
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not sid:
            raise OSError("Missing integrity SID")
        count = security.GetSidSubAuthorityCount(sid)[0]
        if count < 1:
            raise OSError("Missing integrity RID")
        return int(security.GetSidSubAuthority(sid, count - 1)[0])
    finally:
        if token:
            kernel.CloseHandle(token)
        kernel.CloseHandle(process)


def input_access_issue(target_pid: int, *, sender_pid: int | None = None) -> str | None:
    """Explain an incompatible or unreadable browser token before sending input."""
    try:
        sender = process_integrity(os.getpid() if sender_pid is None else sender_pid)
        if sender < 0x2000:
            return ("购物助手当前在受限权限环境中运行，无法可靠控制普通浏览器。"
                    "请关闭此购物助手，从项目文件夹普通双击“启动.vbs”重新打开后重试；"
                    "不要从受限开发工具启动，也不需要以管理员身份运行。")
        target = process_integrity(target_pid)
    except (OSError, ValueError, TypeError):
        return "无法核实浏览器进程权限，已停止操作。请确认浏览器仍在运行，并用普通方式打开 Edge/Chrome 后重试。"
    if target > sender:
        reason = "浏览器以管理员权限运行" if target >= 0x3000 else "浏览器进程权限高于购物助手"
        return (reason + "，Windows 会阻止真实鼠标和键盘输入。"
                "请先保存浏览器中的工作，手动关闭管理员浏览器，再以普通方式打开浏览器和购物助手后重试；"
                "程序不会自动关闭浏览器或提升权限。")
    return None
