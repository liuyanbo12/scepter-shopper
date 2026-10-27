"""User-owned Windows browser: UIA/DOM reads, real mouse/keyboard writes.

This module intentionally imports Windows packages lazily. It never fetches JD
HTML, accepts model JavaScript, calls UIA Invoke, or takes automatic screenshots.
All UI methods must be called on one worker thread; health() is safe anywhere.
"""
from __future__ import annotations

import ctypes
import importlib.util
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


class DesktopError(RuntimeError):
    def __init__(self, code: str, message: str, *, needs_user: bool = False):
        super().__init__(message)
        self.code = code
        self.needs_user = needs_user


_BLOCKED_TARGET = re.compile(
    r"立即购买|马上购买|去结算|立即结算|提交订单|确认订单|确认付款|立即支付|去支付|"
    r"支付订单|货到付款|删除|清空|注销|退出登录|修改密码|账号设置|账户设置|收货地址|"
    r"联系客服|在线客服|联系卖家|发送消息|确认收货|取消订单|申请退款|退换货|"
    r"\b(?:checkout|pay\s*now|place\s*order|buy\s*now|delete|sign\s*out)\b",
    re.I,
)
_BLOCKED_EXACT = {"结算", "支付", "付款", "购买", "下单", "删除选中商品", "移入关注"}
_SENSITIVE_EDIT = re.compile(r"密码|验证码|短信|手机号|手机号码|身份证|银行卡|password|verification|otp", re.I)
_CAPTCHA = re.compile(r"拖动滑块|拖动.*拼图|请完成.*验证|安全验证|访问过于频繁|请验证您是|点击.*完成验证|人机验证|captcha", re.I)
_LOGIN = re.compile(r"扫码登录|扫描二维码登录|短信登录|账户登录|账号密码登录|密码登录|登录京东|京东登录")
_SITE_ERROR = re.compile(r"当前页面异常|请刷新或切换账户|访问受限，请稍后")
_INTERACTIVE = {"Button", "Hyperlink", "Edit", "ComboBox", "CheckBox", "RadioButton", "ListItem", "TabItem", "DataItem"}
_VISIBLE_TYPES = _INTERACTIVE | {"Text", "Image", "Header", "HeaderItem"}
_SAFE_KEYS = {"enter", "esc", "tab", "backspace", "left", "right", "up", "down", "home", "end", "pageup", "pagedown"}


def normalize_url(value: str) -> str:
    """Normalize the scheme omitted by Chromium's visible address bar."""
    value = str(value or "").strip()
    if value and "://" not in value and not value.startswith(("about:", "edge:", "chrome:")):
        value = "https://" + value
    return value


def is_allowed_url(url: str, local_origins: set[str] | None = None) -> bool:
    """Exact suffix boundary, no userinfo, and explicitly registered local origin."""
    try:
        parsed = urlsplit(normalize_url(url))
        if parsed.username or parsed.password or parsed.scheme not in {"http", "https"}:
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        # A trailing dot is legal DNS but deliberately not admitted here.
        if (parsed.hostname or "").endswith("."):
            return False
        if host == "jd.com" or host.endswith(".jd.com"):
            return parsed.port in {None, 80, 443}
        return host == "127.0.0.1" and parsed.scheme == "http" and _origin(url) in (local_origins or set())
    except (ValueError, TypeError):
        return False


def _origin(url: str) -> str:
    parsed = urlsplit(normalize_url(url))
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def blocked_target(name: str) -> bool:
    name = re.sub(r"\s+", "", str(name or "")).strip()
    return name in _BLOCKED_EXACT or bool(_BLOCKED_TARGET.search(name))


def command_user_data_dirs(argv: list[str] | None) -> list[str]:
    """Extract Chromium --user-data-dir values from a process command line."""
    paths: list[str] = []
    args = [str(item) for item in (argv or [])]
    for item in args:
        if item.startswith("--user-data-dir="):
            paths.append(item.split("=", 1)[1].strip('"'))
    if "--user-data-dir" in args:
        offset = args.index("--user-data-dir")
        if offset + 1 < len(args):
            paths.append(args[offset + 1].strip('"'))
    return paths


_JD_WINDOW_TITLE = re.compile(
    r"京东热卖|京东超市|京东国际|京东图书|京东\(JD\.COM\)|item\.jd\.com|search\.jd\.com|cart\.jd\.com|\bjd\.com\b",
    re.I,
)
_NOT_JD_WINDOW = re.compile(r"购物 Agent|127\.0\.0\.1")


def looks_like_jd_title(title: str) -> bool:
    """Rank browser candidates only; a title never proves the current URL."""
    text = str(title or "")
    if _NOT_JD_WINDOW.search(text):
        return False
    return bool(_JD_WINDOW_TITLE.search(text) or
                (re.search(r"京东", text) and re.search(r"Microsoft\s*Edge", text, re.I)))


def default_edge_user_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "") or "") / "Microsoft" / "Edge" / "User Data"


def is_default_user_edge_command(
    argv: list[str] | None, *, app_profile: Path, isolated_profile: Path, user_data: Path | None = None,
) -> bool:
    """True for the user's own Edge, never the shopping-assistant window."""
    if is_project_browser_command(argv, app_profile=app_profile, isolated_profile=isolated_profile):
        return False
    args = [str(item) for item in (argv or [])]
    joined = " ".join(args).lower()
    if "--profile-directory=default" in joined:
        return True
    expected = os.path.normcase(str((user_data or default_edge_user_data()).resolve()))
    for raw in command_user_data_dirs(args):
        try:
            resolved = os.path.normcase(str(Path(raw).resolve()))
        except OSError:
            continue
        if resolved == expected or resolved.startswith(expected + os.sep):
            return True
    return bool(args) and Path(args[0]).name.lower() == "msedge.exe" and not command_user_data_dirs(args)


def is_project_browser_command(argv: list[str] | None, *, app_profile: Path, isolated_profile: Path) -> bool:
    """True for the shopping-assistant window or an isolated project profile."""
    args = [str(item) for item in (argv or [])]
    joined = " ".join(args)
    if "--app=" in joined and "127.0.0.1" in joined:
        return True
    expected = []
    for folder in (app_profile, isolated_profile):
        try:
            expected.append(os.path.normcase(str(Path(folder).resolve())))
        except OSError:
            continue
    for raw in command_user_data_dirs(args):
        try:
            resolved = os.path.normcase(str(Path(raw).resolve()))
        except OSError:
            continue
        if resolved in expected:
            return True
    return False


def _rect(raw: Any) -> list[int]:
    return [int(raw.left), int(raw.top), int(raw.right), int(raw.bottom)]


def _intersect(a: list[int], b: list[int]) -> list[int] | None:
    box = [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]
    return box if box[2] - box[0] >= 3 and box[3] - box[1] >= 3 else None


class DesktopController:
    """Bind the user's already-open Edge/Chrome window; never spawn a hidden one.

    ``stop_check`` may return True to pause, or raise an exception. Errors are
    returned as structured results so a UI can display a useful recovery action.
    Login stays in the user's own browser profile. The shopping-assistant
    ``--app=`` window and any isolated project profile are never bound.
    """

    def __init__(self, root: Path | str, browser: str = "edge", max_elements: int = 180,
                 action_delay: float = 0.5, stop_check: Callable[[], Any] | None = None):
        self.root = Path(root).resolve()
        self.browser = browser.lower()
        self.max_elements = max(20, min(int(max_elements), 300))
        self.action_delay = max(0.1, min(float(action_delay), 3.0))
        self.stop_check = stop_check
        self.profile_dir = self.root / "data" / "browser-profile" / self.browser
        self.app_profile_dir = self.root / "data" / "app-profile"
        self._thread_id: int | None = None
        self._desktop = None
        self._uia_desktop = None
        self._gui = None
        self._psutil = None
        self._window = None
        self._handle: int | None = None
        self._owned_pids: set[int] = set()
        self._process: subprocess.Popen | None = None
        self._targets: dict[str, dict[str, Any]] = {}
        self._observation: dict[str, Any] | None = None
        self._local_origins: set[str] = set()
        self._typed_target: dict[str, Any] | None = None
        self._paused = False
        self._session = uuid.uuid4().hex[:8]
        self._counter = 0
        self._dom_reader = None
        self._pw_runtime = None
        self._pw_context = None
        self._user_cdp_attached = False
        self._user_cdp_port: int | None = None

    def health(self) -> dict[str, Any]:
        modules = {name: importlib.util.find_spec(name) is not None
                   for name in ("pyautogui", "pywinauto", "psutil", "comtypes", "websocket")}
        from shopper.browser_bridge import NodeBrowserBridge
        modules["node_playwright"] = NodeBrowserBridge.available(self.root)
        executable = self._browser_executable()
        return {
            "status": "ok" if sys.platform == "win32" and all(modules.values()) and executable else "error",
            "platform": sys.platform, "dependencies": modules,
            "browser": self.browser, "browser_executable": str(executable) if executable else None,
            "profile_dir": str(self.profile_dir), "screenshots_default": False,
            "observation_method": "UIA/DOM read of the user's already-open Edge; native HWND/PID/title/bounds binding",
            "input_method": "Physical mouse and Unicode keyboard on the bound user Edge window",
            "message": "绑定当前已打开的 Edge，只读取京东页面。" if executable else "未找到 Edge 或 Chrome，请先安装浏览器。",
        }

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def close(self) -> dict[str, Any]:
        """Close only a browser created by this controller, on its UI thread."""
        if self._thread_id is not None and self._thread_id != threading.get_ident():
            return self._error(DesktopError("wrong_thread", "关闭浏览器必须在原桌面工作线程调用。"))
        try:
            if self._pw_context is not None:
                if self._user_cdp_attached and hasattr(self._pw_context, "detach"):
                    self._pw_context.detach()
                else:
                    self._pw_context.close()
                self._pw_context = None
                self._user_cdp_attached = False
                self._user_cdp_port = None
            if self._pw_runtime is not None:
                self._pw_runtime.stop()
                self._pw_runtime = None
            self._invalidate()
            self._handle = None
            return {"status": "ok"}
        except Exception as exc:
            return self._error(exc)

    def _checkpoint(self) -> None:
        if self._paused:
            raise DesktopError("paused", "桌面操作已暂停。", needs_user=True)
        if self.stop_check and self.stop_check():
            raise DesktopError("stopped", "任务已暂停或停止。", needs_user=True)

    def _init(self) -> None:
        self._checkpoint()
        if sys.platform != "win32":
            raise DesktopError("unsupported_platform", "真实桌面执行需要 Windows 10/11。")
        current = threading.get_ident()
        if self._thread_id is not None and self._thread_id != current:
            raise DesktopError("wrong_thread", "UIA 必须在同一个专用工作线程中调用。")
        if self._desktop is not None:
            return
        try:
            # Initialize COM on the actual worker, not the GUI/main thread.
            result = ctypes.windll.ole32.CoInitializeEx(None, 0)  # MTA
            if result not in (0, 1, -2147417850):  # existing STA can also host UIA
                raise OSError(f"CoInitializeEx: {result}")
            import pyautogui
            import psutil
            from pywinauto import Desktop
            self._gui = pyautogui
            self._gui.FAILSAFE = True
            self._gui.PAUSE = 0.08
            self._psutil = psutil
            self._desktop = Desktop(backend="win32")
            self._thread_id = current
            ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p
        except ImportError as exc:
            raise DesktopError("missing_dependency", f"缺少桌面依赖 {exc.name}，请运行安装脚本。") from exc

    def _browser_executable(self) -> Path | None:
        names = [self.browser] if self.browser in {"edge", "chrome"} else ["edge", "chrome"]
        # Fall back when the preferred browser is unavailable.
        names += [x for x in ("edge", "chrome") if x not in names]
        bases = [os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("PROGRAMFILES", ""), os.environ.get("LOCALAPPDATA", "")]
        for name in names:
            relative = "Microsoft/Edge/Application/msedge.exe" if name == "edge" else "Google/Chrome/Application/chrome.exe"
            for base in bases:
                if base:
                    path = Path(base) / relative
                    if path.is_file():
                        return path
        return None

    def _error(self, exc: Exception) -> dict[str, Any]:
        if isinstance(exc, DesktopError):
            return {"status": "needs_user" if exc.needs_user else "error", "error": exc.code, "message": str(exc)}
        if exc.__class__.__name__ == "FailSafeException":
            self._paused = True
            return {"status": "needs_user", "error": "emergency_stop", "message": "鼠标移到屏幕角落，已触发紧急停止。移开鼠标后点击继续。"}
        if "no active desktop" in str(exc).lower():
            return {"status": "needs_user", "error": "desktop_unavailable", "message": "当前 Windows 桌面不可交互，可能已锁屏或远程桌面已断开。请解锁/恢复桌面后点击继续。"}
        return {"status": "error", "error": exc.__class__.__name__, "message": f"桌面操作失败：{str(exc)[:450]}"}

    def _bridge_pids(self) -> set[int]:
        if self._pw_context is None:
            return set()
        try:
            info = self._pw_context.current()
        except Exception:
            return set()
        pids: set[int] = set()
        for page in info.get("pages") or []:
            pid = page.get("browser_pid")
            if not isinstance(pid, int) or pid <= 0:
                continue
            pids.add(pid)
            try:
                pids.update(child.pid for child in self._psutil.Process(pid).children(recursive=True))
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied, OSError):
                continue
        return pids

    def _processes_for_profile(self) -> set[int]:
        """Legacy isolated-profile lookup; attach mode prefers user Edge instead."""
        result: set[int] = set()
        expected = os.path.normcase(str(self.profile_dir.resolve()))
        for process in self._psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if (process.info.get("name") or "").lower() not in {"msedge.exe", "chrome.exe"}:
                    continue
                paths = command_user_data_dirs(process.info.get("cmdline") or [])
                if any(os.path.normcase(str(Path(p).resolve())) == expected for p in paths):
                    result.add(process.pid)
                    result.update(child.pid for child in process.children(recursive=True))
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied, OSError):
                continue
        return result

    def _user_browser_pids(self) -> set[int]:
        result: set[int] = set()
        for process in self._psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if (process.info.get("name") or "").lower() not in {"msedge.exe", "chrome.exe"}:
                    continue
                if is_project_browser_command(
                    process.info.get("cmdline") or [],
                    app_profile=self.app_profile_dir,
                    isolated_profile=self.profile_dir,
                ):
                    continue
                result.add(process.pid)
                result.update(child.pid for child in process.children(recursive=True))
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied, OSError):
                continue
        return result

    def _window_belongs_to_user_browser(self, pid: int) -> bool:
        if pid in self._owned_pids:
            return True
        try:
            process = self._psutil.Process(pid)
            if process.name().lower() not in {"msedge.exe", "chrome.exe"}:
                return False
            if is_project_browser_command(
                process.cmdline(),
                app_profile=self.app_profile_dir,
                isolated_profile=self.profile_dir,
            ):
                return False
            self._owned_pids.add(pid)
            return True
        except (self._psutil.NoSuchProcess, self._psutil.AccessDenied, OSError):
            return False

    def _window_score(self, window: Any) -> int:
        title = ""
        try:
            title = window.window_text() or ""
        except Exception:
            title = ""
        score = 0
        if looks_like_jd_title(title):
            score += 20
        elif _NOT_JD_WINDOW.search(title):
            score -= 40
        try:
            name = self._psutil.Process(window.process_id()).name().lower()
        except Exception:
            name = ""
        preferred = "msedge.exe" if self.browser != "chrome" else "chrome.exe"
        if name == preferred:
            score += 8
        if self._handle and int(window.handle) == self._handle:
            score += 5
        try:
            if int(window.handle) == int(ctypes.windll.user32.GetForegroundWindow() or 0):
                score += 3
        except Exception:
            pass
        try:
            box = _rect(window.rectangle())
            score += min(max(0, box[2] - box[0]) * max(0, box[3] - box[1]) // 200000, 4)
        except Exception:
            pass
        return score

    def _find_owned_window(self) -> Any:
        self._owned_pids = self._user_browser_pids() | self._bridge_pids()
        windows = []
        # Enumerate HWNDs directly: UIA's top-level enumeration may omit a
        # Chromium window even while direct access by its HWND works.
        from ctypes import wintypes
        handles = []
        user32 = ctypes.windll.user32
        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def collect(handle, _):
            name = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(handle, name, 128)
            if user32.IsWindowVisible(handle) and name.value.startswith("Chrome_WidgetWin_"):
                handles.append(handle)
            return True
        user32.EnumWindows(collect, 0)
        for handle in handles:
            try:
                window = self._desktop.window(handle=handle).wrapper_object()
                if not window.class_name().startswith("Chrome_WidgetWin_"):
                    continue
                if not self._window_belongs_to_user_browser(window.process_id()):
                    continue
                title = ""
                try:
                    title = window.window_text() or ""
                except Exception:
                    title = ""
                box = _rect(window.rectangle())
                minimized = False
                try:
                    minimized = bool(window.is_minimized())
                except Exception:
                    minimized = bool(user32.IsIconic(handle))
                # Minimized or title-known JD tabs can report a tiny rect; still bind them.
                if looks_like_jd_title(title) or minimized or (box[2] - box[0] >= 400 and box[3] - box[1] >= 250):
                    windows.append(window)
            except Exception:
                continue
        if not windows:
            raise DesktopError("browser_not_found", "没有找到已打开的 Edge。请先打开你的 Edge 并登录京东，再点打开京东。", needs_user=True)
        selected = max(windows, key=self._window_score)
        self._window, self._handle = selected, int(selected.handle)
        return selected

    def _assert_foreground(self) -> None:
        self._checkpoint()
        self._assert_input_access()
        foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if not foreground:
            raise DesktopError("desktop_unavailable", "当前 Windows 桌面不可交互，可能已锁屏或远程桌面已断开。请解锁/恢复桌面后点击继续。", needs_user=True)
        if not self._handle or foreground != self._handle:
            raise DesktopError("focus_changed", "已打开的 Edge 失去前台焦点。请切回该窗口，再点击继续；已停止点击。", needs_user=True)

    def _assert_input_access(self) -> None:
        if self._window is None:
            raise DesktopError("browser_not_found", "尚未绑定浏览器窗口，请先打开京东。", needs_user=True)
        if self._pw_context is not None:
            # Node performs physical input on this route. Its token may differ
            # from Python's, so the broker checks its own access to the browser.
            try:
                access = self._pw_context.check_input_access()
                if not isinstance(access, dict) or access.get("status") != "ok":
                    raise RuntimeError(access.get("message", "浏览器输入权限检查未成功。") if isinstance(access, dict) else "浏览器输入权限检查未成功。")
            except Exception as exc:
                raise DesktopError("browser_permission_mismatch", str(exc), needs_user=True) from exc
            return
        from shopper.windows_access import input_access_issue
        issue = input_access_issue(int(self._window.process_id()))
        if issue:
            raise DesktopError("browser_permission_mismatch", issue, needs_user=True)

    def _assert_launch_access(self, executable: Path) -> None:
        """Do not start a second instance against an inaccessible user profile."""
        from shopper.windows_access import input_access_issue
        issue = input_access_issue(os.getpid())
        if issue:
            raise DesktopError("browser_permission_mismatch", issue, needs_user=True)
        if self._psutil is None:
            return
        for process in self._psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if (process.info.get("name") or "").lower() != executable.name.lower():
                    continue
                argv = process.info.get("cmdline") or []
                if any(str(arg).startswith("--type=") for arg in argv):
                    continue
                if is_project_browser_command(argv, app_profile=self.app_profile_dir, isolated_profile=self.profile_dir):
                    continue
                issue = input_access_issue(process.pid)
                if issue:
                    raise DesktopError("browser_permission_mismatch", issue, needs_user=True)
            except self._psutil.NoSuchProcess:
                continue
            except self._psutil.AccessDenied as exc:
                raise DesktopError("browser_permission_mismatch", "无法检查现有浏览器的权限。请保存工作后，以普通用户方式手动重新打开浏览器。", needs_user=True) from exc

    def _activate_window(self) -> None:
        """Explicit activation only; ordinary input still requires foreground."""
        from ctypes import wintypes
        window = self._window
        hwnd = int(self._handle or 0)
        if window is None or not hwnd:
            raise DesktopError("browser_not_found", "尚未绑定浏览器窗口，请先打开京东。", needs_user=True)

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                        ("time", wintypes.DWORD), ("pt", wintypes.POINT),
                        ("lPrivate", wintypes.DWORD)]

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user32.PeekMessageW.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetActiveWindow.argtypes = [wintypes.HWND]
        user32.SetActiveWindow.restype = wintypes.HWND
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        expected_pid = int(window.process_id())
        target_pid = wintypes.DWORD()
        target_thread = int(user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid)))
        if not target_thread or target_pid.value != expected_pid:
            raise DesktopError("browser_window_changed", "绑定的浏览器窗口已变化，请重新打开京东入口。", needs_user=True)
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return
        # AttachThreadInput requires a message queue on the worker thread.
        message = MSG()
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)  # PM_NOREMOVE
        user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.08)
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return

        current_thread = int(kernel32.GetCurrentThreadId())
        for _ in range(3):
            self._checkpoint()
            foreground = user32.GetForegroundWindow()
            if not foreground:
                raise DesktopError("desktop_unavailable", "当前 Windows 桌面不可交互，请解锁或恢复远程桌面。", needs_user=True)
            foreground_thread = int(user32.GetWindowThreadProcessId(foreground, None))
            attached = []
            seen = set()
            cleanup_failed = False
            try:
                for thread in (foreground_thread, target_thread):
                    if thread and thread != current_thread and thread not in seen:
                        seen.add(thread)
                        if user32.AttachThreadInput(current_thread, thread, True):
                            attached.append(thread)
                verified_pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(verified_pid))
                if verified_pid.value != expected_pid:
                    raise DesktopError("browser_window_changed", "绑定的浏览器窗口已变化，请重新打开京东入口。", needs_user=True)
                user32.BringWindowToTop(hwnd)
                user32.SetActiveWindow(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                for thread in reversed(attached):
                    try:
                        if not user32.AttachThreadInput(current_thread, thread, False):
                            cleanup_failed = True
                    except Exception:
                        cleanup_failed = True
            if cleanup_failed:
                raise DesktopError("focus_cleanup_failed", "Windows 未能释放临时焦点连接，已暂停操作。请重新打开应用后继续。", needs_user=True)
            time.sleep(0.12)
            if int(user32.GetForegroundWindow() or 0) == hwnd:
                return
        raise DesktopError("focus_acquisition_failed", "Windows 暂未允许切换到京东窗口。请点击该浏览器窗口，再按继续。", needs_user=True)

    def _focus(self) -> None:
        self._checkpoint()
        self._find_owned_window()
        self._assert_input_access()
        if self._pw_context is not None:
            try:
                self._pw_context.input(self._handle, "focus")
            except Exception as exc:
                raise DesktopError("focus_acquisition_failed", f"浏览器输入服务未能切换到京东窗口：{str(exc)[:250]}", needs_user=True) from exc
            self._assert_foreground()
            return
        self._activate_window()
        self._assert_foreground()

    def _connect_browser_context(self) -> None:
        from shopper.browser_bridge import NodeBrowserBridge
        executable = self._browser_executable()
        if executable is None:
            raise DesktopError("browser_missing", "未找到 Microsoft Edge 或 Google Chrome。")
        bridge = NodeBrowserBridge(self.root)
        bridge.launch(self.profile_dir, "msedge" if executable.name.lower() == "msedge.exe" else "chrome",
                      executable=str(executable))
        self._pw_context = bridge
        self._dom_reader = None

    def _default_user_data(self) -> Path:
        return default_edge_user_data()

    def _user_edge_debug_port(self) -> int | None:
        if self._psutil is None or self._window is None:
            return None
        bound_pid = int(self._window.process_id())
        for process in self._psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if process.pid != bound_pid:
                    continue
                if (process.info.get("name") or "").lower() not in {"msedge.exe", "chrome.exe"}:
                    continue
                argv = process.info.get("cmdline") or []
                if is_project_browser_command(argv, app_profile=self.app_profile_dir, isolated_profile=self.profile_dir):
                    continue
                for item in argv:
                    if str(item).startswith("--remote-debugging-port="):
                        port = int(str(item).split("=", 1)[1])
                        if 1024 <= port <= 65535:
                            return port
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied, OSError, ValueError):
                continue
        return None

    def _connect_user_cdp(self, port: int) -> None:
        from shopper.browser_bridge import NodeBrowserBridge
        bridge = NodeBrowserBridge(self.root)
        bridge.connect(port)
        self._pw_context = bridge
        self._dom_reader = None
        self._user_cdp_attached = True
        self._user_cdp_port = port

    def _try_attach_user_cdp(self) -> None:
        if self._pw_context is not None:
            return
        port = self._user_edge_debug_port()
        if port is None:
            return
        try:
            self._connect_user_cdp(port)
        except Exception:
            self._pw_context = None
            self._user_cdp_attached = False

    def _uia_window(self, window: Any) -> Any:
        """Read accessibility through UIA while retaining Win32 HWND ownership."""
        try:
            if self._uia_desktop is None:
                from pywinauto import Desktop
                self._uia_desktop = Desktop(backend="uia")
            wrapper = self._uia_desktop.window(handle=int(window.handle)).wrapper_object()
            if int(wrapper.handle) != int(window.handle) or wrapper.process_id() != window.process_id():
                raise ValueError("UIA HWND/PID mismatch")
            return wrapper
        except Exception as exc:
            raise DesktopError("uia_unavailable", "无法读取当前浏览器的 UIA 无障碍接口。请确认浏览器已正常加载，并与应用使用相同的普通用户权限。", needs_user=True) from exc

    def focus(self) -> dict[str, Any]:
        """Explicit user resume may bring only the owned browser to foreground."""
        try:
            self._init()
            self._focus()
            return self._with_observation(self._observe())
        except Exception as exc:
            return self._error(exc)

    def _launch(self, url: str) -> None:
        """Open the user's own Edge if none is visible. Never use an isolated profile."""
        self._init()
        executable = self._browser_executable()
        if executable is None:
            raise DesktopError("browser_missing", "未找到 Microsoft Edge 或 Google Chrome。")
        self._assert_launch_access(executable)
        self._pw_context = None
        self._dom_reader = None
        creation = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creation |= subprocess.DETACHED_PROCESS
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creation |= subprocess.CREATE_NEW_PROCESS_GROUP
        # Default profile only: if Edge is already running, this usually adds a
        # tab to that window instead of creating a second browser.
        self._process = subprocess.Popen([str(executable), url], creationflags=creation)
        deadline = time.monotonic() + 25
        last_error = None
        window_seen = False
        while time.monotonic() < deadline:
            self._checkpoint()
            try:
                self._find_owned_window()
                window_seen = True
                focused = False
                try:
                    self._focus()
                    focused = True
                except DesktopError as exc:
                    if exc.code not in {"focus_changed", "focus_acquisition_failed"}:
                        raise
                    last_error = exc
                current = ""
                try:
                    current = self._read_url(self._window) or ""
                except Exception:
                    current = ""
                if is_allowed_url(current, self._local_origins):
                    return
                if focused:
                    self._keys("ctrl", "l")
                    self._send_unicode(url)
                    self._assert_foreground()
                    self._press("enter")
                    self._wait_after_action(extra=0.7)
                    return
            except DesktopError as exc:
                last_error = exc
                if exc.code not in {"browser_not_found", "focus_changed", "focus_acquisition_failed"}:
                    raise
            time.sleep(0.4)
        if window_seen:
            raise DesktopError("focus_acquisition_failed", "Edge 已打开，但未能切到前台。请点击你的 Edge 窗口后再继续。", needs_user=True) from last_error
        raise DesktopError("browser_launch_timeout", "没有找到已打开的 Edge。请先打开你的 Edge 并登录京东，再点打开京东。", needs_user=True) from last_error

    def open_jd(self) -> dict[str, Any]:
        return self.open_url("https://www.jd.com/")

    def open_url(self, url: str, allow_local: bool = False) -> dict[str, Any]:
        """Bind the user's Edge and open JD, or register one loopback origin for smoke tests."""
        try:
            url = normalize_url(url)
            if allow_local:
                parsed = urlsplit(url)
                if parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port and not parsed.username:
                    self._local_origins.add(_origin(url))
            if not is_allowed_url(url, self._local_origins):
                raise DesktopError("url_not_allowed", "只允许京东域名或显式开启的本地测试页面。")
            self._init()
            try:
                self._find_owned_window()
            except DesktopError:
                self._handle = None
            if self._handle:
                current = ""
                try:
                    current = self._read_url(self._window) or ""
                except Exception:
                    current = ""
                if is_allowed_url(current, self._local_origins):
                    try:
                        self._focus()
                    except DesktopError as exc:
                        if exc.code not in {"focus_changed", "focus_acquisition_failed"}:
                            raise
                    return self._with_observation(self._observe())
                result = self.navigate(url, acquire_focus=True)
                if result.get("error") in {"focus_changed", "focus_acquisition_failed"}:
                    observation = self._observe()
                    if observation.get("status") == "ok":
                        return self._with_observation(observation)
                    result = dict(result)
                    result["observation"] = observation
                    result["message"] = "Edge 已打开，但未能切到前台。请点击你的 Edge 窗口后再继续。"
                return result
            self._launch(url)
            observation = self._observe()
            return self._with_observation(observation)
        except Exception as exc:
            return self._error(exc)

    def navigate(self, url: str, *, acquire_focus: bool = False) -> dict[str, Any]:
        """Physical navigation; running tasks never take focus back silently.

        Only explicit open/start/resume paths request activation. Ordinary model
        navigation must retain the existing foreground ownership.
        """
        try:
            url = normalize_url(url)
            if not is_allowed_url(url, self._local_origins):
                raise DesktopError("url_not_allowed", "导航地址不在京东或本地测试白名单中。")
            if self._url_gate(url):
                raise DesktopError("protected_navigation", "不会自动打开登录、支付或结算页面，请由你处理。", needs_user=True)
            self._init()
            if acquire_focus:
                self._focus()
            else:
                self._find_owned_window()
                self._assert_foreground()
            self._invalidate()
            self._keys("ctrl", "l")
            self._assert_foreground()
            self._send_unicode(url)
            self._assert_foreground()
            self._press("enter")
            self._wait_after_action(extra=0.7)
            return self._with_observation(self._observe())
        except Exception as exc:
            return self._error(exc)

    def _invalidate(self) -> None:
        self._targets = {}
        self._observation = None

    def _wait_after_action(self, extra: float = 0) -> None:
        deadline = time.monotonic() + self.action_delay + extra
        while time.monotonic() < deadline:
            self._checkpoint()
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    @staticmethod
    def _value(wrapper: Any) -> str:
        try:
            if wrapper.element_info.element.CurrentIsPassword:
                return ""
        except Exception:
            pass
        for method in (lambda: wrapper.get_value(), lambda: wrapper.iface_value.CurrentValue,
                       lambda: wrapper.iface_legacy_iaccessible.CurrentValue):
            try:
                value = method()
                if isinstance(value, str) and value:
                    return value
            except Exception:
                pass
        return ""

    def _read_url(self, window: Any) -> str:
        if self._pw_context is not None:
            try:
                pages = self._pw_context.current().get("pages", [])
            except Exception as exc:
                raise DesktopError("browser_read_unavailable", "现有浏览器的页面读取连接已失效。请确认浏览器仍已打开，再点击继续。", needs_user=True) from exc
            bounds = _rect(window.rectangle())
            title = window.window_text()
            matches = []
            for page in pages:
                if page.get("browser_pid") != window.process_id():
                    continue
                box = page.get("bounds", {})
                left, top = box.get("left", 0), box.get("top", 0)
                candidate = [left, top, left + box.get("width", 0), top + box.get("height", 0)]
                # Browser bounds are native screen units on this Windows
                # desktop. Allow the normal invisible 8px resize border.
                scale = page.get("screen", {}).get("dpr", 1)
                scaled_candidate = [v * scale for v in candidate]
                if min(max(abs(a-b) for a, b in zip(bounds, candidate)),
                       max(abs(a-b) for a, b in zip(bounds, scaled_candidate))) > 16:
                    continue
                page_title = page.get("title", "")
                if page_title and page_title not in title:
                    continue
                matches.append(page)
            if len(matches) != 1:
                raise DesktopError("tab_window_mismatch", "当前可见标签与项目浏览器窗口无法唯一匹配，请关闭多余项目窗口后继续。", needs_user=True)
            return matches[0]["url"]
        # Only browser chrome, not arbitrary web page inputs, is considered.
        queue = deque([(self._uia_window(window), 0)])
        visited = 0
        while queue and visited < 180:
            item, depth = queue.popleft()
            visited += 1
            try:
                role = item.element_info.control_type
                if role == "Document":
                    continue
                name = item.element_info.name or ""
                automation_id = item.element_info.automation_id or ""
                if item.is_visible() and role in {"Edit", "ComboBox"} and (
                    re.search(r"地址.*搜索|地址和搜索栏|地址栏|搜索或输入|输入网址|omnibox|address.*(?:bar|search)|search or enter web", name, re.I)
                    or automation_id in {"addressEditBox", "view_1022", "omnibox"}
                ):
                    value = self._value(item)
                    if value:
                        if "://" not in value:
                            local_value = "http://" + value
                            if _origin(local_value) in self._local_origins:
                                return local_value
                        return normalize_url(value)
                if depth < 10:
                    queue.extend((child, depth + 1) for child in item.children())
            except Exception:
                continue
        raise DesktopError("address_unavailable", "无法通过 UIA 读取当前浏览器的真实地址，且没有可用的页面读取连接。请把京东标签置于当前窗口，关闭浏览器提示后继续。", needs_user=True)

    @staticmethod
    def _url_gate(url: str) -> str | None:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if host in {"passport.jd.com", "plogin.m.jd.com", "login.jd.com", "auth.jd.com"}:
            return "页面需要登录。请在当前 Edge 窗口里登录京东，然后点击继续。"
        if host.startswith(("cashier.", "pay.", "trade.")) or re.search(r"/(?:checkout|pay|payment)(?:/|\?|$)", parsed.path, re.I):
            return "已到支付或结算页面，自动操作在购物车处结束。"
        return None

    def _document_roots(self, window: Any) -> list[Any]:
        roots = []
        queue = deque([(self._uia_window(window), 0)])
        visited = 0
        deadline = time.monotonic() + 5
        while queue and visited < 320 and time.monotonic() < deadline:
            self._checkpoint()
            wrapper, depth = queue.popleft()
            visited += 1
            try:
                role = wrapper.element_info.control_type
                if role == "Document" and wrapper.is_visible():
                    roots.append(wrapper)
                    continue
                if depth < 14:
                    queue.extend((child, depth + 1) for child in wrapper.children())
            except Exception:
                continue
        return roots

    def observe(self) -> dict[str, Any]:
        try:
            self._init()
            return self._observe()
        except Exception as exc:
            self._invalidate()
            return self._error(exc)

    def _observe(self) -> dict[str, Any]:
        self._checkpoint()
        window = self._find_owned_window()
        if self._pw_context is None:
            self._try_attach_user_cdp()
        url = self._read_url(window)
        self._counter += 1
        obs_id = f"{self._session}-{self._counter}"
        bounds = _rect(window.rectangle())
        observation: dict[str, Any] = {
            "status": "ok", "obs_id": obs_id, "url": url, "title": window.window_text()[:220],
            "window": {"handle": self._handle, "rect": bounds, "foreground": int(ctypes.windll.user32.GetForegroundWindow()) == self._handle},
            "elements": [], "text": "", "source": "uia", "screenshot": None,
        }
        self._targets = {}
        self._observation = observation
        if not is_allowed_url(url, self._local_origins):
            observation.update(status="needs_user", error="outside_allowed_site", message="当前页面已离开京东。请返回京东；不会读取或操作其他网站内容。")
            return observation
        gate = self._url_gate(url)
        if gate:
            observation.update(status="needs_user", error="protected_page", message=gate)
            return observation
        if self._pw_context is None:
            return self._observe_uia_content(observation)
        result = self._observe_dom(observation)
        if result.get("error") == "dom_unavailable":
            observation["status"] = "ok"
            observation.pop("error", None)
            observation.pop("message", None)
            observation["elements"] = []
            observation["text"] = ""
            self._targets = {}
            return self._observe_uia_content(observation)
        return result

    def _observe_dom(self, observation: dict[str, Any]) -> dict[str, Any]:
        from shopper.dom_reader import DomReader, map_css_rect, native_render_rect
        if self._dom_reader is None:
            self._dom_reader = DomReader(self.profile_dir, context=self._pw_context)
        try:
            if self._pw_context is None:
                port = self._dom_reader._port()
                listeners = [c for c in self._psutil.net_connections(kind="tcp") if c.status == "LISTEN" and c.laddr and c.laddr.port == port]
                if not any(c.pid in self._owned_pids and c.laddr.ip in {"127.0.0.1", "::1"} for c in listeners):
                    raise DesktopError("debug_port_mismatch", "页面读取端口不属于项目浏览器的本地监听，已停止读取。")
            snapshot = self._dom_reader.snapshot(observation["url"])
            render_rect = native_render_rect(self._handle, snapshot["viewport"])
            observation.update(source="dom", text=snapshot["text"], title=snapshot["title"],
                               viewport=snapshot["viewport"], render_rect=render_rect,
                               ready_state=snapshot.get("ready_state"), selected_specs=snapshot.get("selected_specs", []))
            if _CAPTCHA.search(snapshot["text"]) or _LOGIN.search(snapshot["title"]) or (
                _LOGIN.search(snapshot["text"]) and ("二维码" in snapshot["text"] or "密码" in snapshot["text"])
            ):
                observation.update(status="needs_user", error="authentication_required", message="页面出现登录或安全验证。请手动完成后继续。", text="检测到登录或安全验证，需要用户处理。")
                return observation
            if _SITE_ERROR.search(snapshot["text"]):
                observation.update(status="needs_user", error="site_error", message="京东页面显示异常提示，请在项目浏览器里处理后继续。")
                return observation
            interactive = [e for e in snapshot["elements"] if e["role"] != "Text"]
            text_nodes = [e for e in snapshot["elements"] if e["role"] == "Text"]
            interactive_budget = max(int(self.max_elements * 0.7), self.max_elements - len(text_nodes))
            picked = interactive[:interactive_budget] + text_nodes[:self.max_elements-min(len(interactive),interactive_budget)]
            for raw in picked:
                entry = dict(raw)
                entry["rect"] = map_css_rect(entry.pop("css_rect"), render_rect, snapshot["viewport"])
                entry["id"] = f"e{len(observation['elements']) + 1}"
                if blocked_target(entry["name"]):
                    entry["blocked"] = True
                observation["elements"].append(entry)
                self._targets[entry["id"]] = {"backend": "dom", "entry": entry.copy(), "url": observation["url"]}
            observation["truncated"] = len(snapshot["elements"]) > self.max_elements
            if not observation["elements"]:
                observation.update(status="needs_user", error="empty_page_tree", message="页面尚无可操作控件，请等待加载或手动处理后继续。")
            return observation
        except Exception as exc:
            observation.update(status="needs_user", error="dom_unavailable", message=f"页面结构读取暂不可用：{str(exc)[:250]}")
            return observation

    def _observe_uia_content(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Read the bound browser's actual UIA Document without restarting it."""
        observation["source"] = "uia"
        window = self._window
        bounds = observation["window"]["rect"]
        url = observation["url"]
        roots = []
        for _ in range(3):
            roots = self._document_roots(window)
            if roots:
                break
            time.sleep(0.35)
        if not roots:
            observation.update(status="needs_user", error="page_tree_unavailable", message="当前浏览器尚未提供可读取的网页 UIA 控件树，现有页面读取连接也不可用。请等待页面加载，或关闭浏览器提示后继续。")
            return observation
        documents = []
        for root in roots:
            try:
                box = _intersect(_rect(root.rectangle()), bounds)
                if root.is_visible() and box:
                    documents.append((root, box))
            except Exception:
                continue
        if not documents:
            observation.update(status="needs_user", error="page_unavailable", message="网页 UIA 区域不在可见窗口内，请恢复浏览器窗口后继续。")
            return observation
        # Use the main visible document, not browser chrome or a side panel, as
        # both the content boundary and the physical scrolling region.
        root, render_rect = max(documents, key=lambda pair: (pair[1][2]-pair[1][0]) * (pair[1][3]-pair[1][1]))
        observation["render_rect"] = render_rect
        texts: list[str] = []
        seen_text: set[str] = set()
        candidates: list[tuple[dict[str, Any], Any]] = []
        queue = deque([(root, 0, [])])
        visited = 0
        deadline = time.monotonic() + 10
        while queue and visited < 2200 and time.monotonic() < deadline:
            self._checkpoint()
            wrapper, depth, ancestors = queue.popleft()
            visited += 1
            try:
                info = wrapper.element_info
                role = info.control_type
                box = _rect(wrapper.rectangle())
                visible_box = _intersect(box, render_rect)
                visible = wrapper.is_visible() and visible_box is not None
                name = re.sub(r"\s+", " ", info.name or "").strip()[:500]
                if visible and name and role in _VISIBLE_TYPES:
                    if name not in seen_text:
                        texts.append(name)
                        seen_text.add(name)
                    if not _SENSITIVE_EDIT.search(name) or role != "Edit":
                        value = self._value(wrapper)[:500] if role in {"Edit", "ComboBox", "Hyperlink"} else ""
                        entry = {"id": "", "name": name[:300], "role": role, "value": value,
                                 "rect": visible_box, "enabled": bool(wrapper.is_enabled()), "visible": True,
                                 "parent_name": ancestors[-1] if ancestors else "", "ancestor_names": ancestors[-3:]}
                        if role == "Hyperlink" and value.startswith(("http://", "https://")):
                            entry["href"] = value
                        candidates.append((entry, wrapper))
                elif visible and role in {"Edit", "ComboBox"}:
                    # An unlabeled search box still exposes a placeholder/value.
                    value = self._value(wrapper)[:500]
                    if not self._is_password(wrapper):
                        candidates.append(({"id": "", "name": "未标注输入框", "role": role, "value": value,
                                            "rect": visible_box, "enabled": bool(wrapper.is_enabled()), "visible": True,
                                            "parent_name": ancestors[-1] if ancestors else "", "ancestor_names": ancestors[-3:]}, wrapper))
                if depth < 22:
                    # Offscreen containers can contain visible children; do not
                    # prune them merely because their own rect is empty.
                    next_ancestors = (ancestors + ([name[:160]] if name and role != "Document" else []))[-3:]
                    queue.extend((child, depth + 1, next_ancestors) for child in wrapper.children())
            except Exception:
                continue
        text = "\n".join(texts)[:18000]
        observation["text"] = text
        observation["truncated"] = bool(queue) or len(candidates) > self.max_elements
        observation["scanned_nodes"] = visited
        if _CAPTCHA.search(text) or _LOGIN.search(observation["title"]) or (
            _LOGIN.search(text) and ("登录" in text and ("二维码" in text or "密码" in text))
        ):
            observation.update(status="needs_user", error="authentication_required", message="页面出现登录或安全验证。请手动完成；Agent 不填写认证信息或绕过验证。")
            # Do not send authentication screen contents or input values upstream.
            observation["text"] = "检测到登录或安全验证，需要用户处理。"
            return observation
        # Interactive nodes first, then visible product text. Position remains
        # on each entry; no positional index is valid beyond this observation.
        candidates.sort(key=lambda pair: 0 if pair[0]["role"] in _INTERACTIVE else 1)
        for entry, wrapper in candidates[:self.max_elements]:
            element_id = f"e{len(observation['elements']) + 1}"
            entry["id"] = element_id
            if blocked_target(entry["name"]):
                entry["blocked"] = True
            observation["elements"].append(entry)
            self._targets[element_id] = {"wrapper": wrapper, "entry": entry.copy(), "url": url}
        if not observation["elements"]:
            observation.update(status="needs_user", error="empty_page_tree", message="当前网页未提供可读取控件。请等待加载或手动处理当前页面后继续。")
        return observation

    @staticmethod
    def _is_password(wrapper: Any) -> bool:
        try:
            return bool(wrapper.element_info.element.CurrentIsPassword)
        except Exception:
            return False

    @staticmethod
    def _with_observation(observation: dict[str, Any]) -> dict[str, Any]:
        result = {"status": observation.get("status", "ok"), "observation": observation}
        for field in ("message", "error"):
            if field in observation:
                result[field] = observation[field]
        return result

    def _validate_observation(self, action: dict[str, Any]) -> None:
        if not self._observation or action.get("obs_id") != self._observation.get("obs_id"):
            raise DesktopError("stale_observation", "页面观察编号已过期，请重新观察后再操作。")
        if self._observation.get("status") != "ok":
            raise DesktopError("page_needs_user", self._observation.get("message", "当前页面需要人工处理。"), needs_user=True)
        window = self._find_owned_window()
        if self._read_url(window) != self._observation["url"]:
            self._invalidate()
            raise DesktopError("page_changed", "当前网址已变化，请重新观察。")

    def _resolve_target(self, element_id: str, *, for_type: bool = False) -> dict[str, Any]:
        target = self._targets.get(str(element_id))
        if target is None:
            raise DesktopError("unknown_element", "控件不属于最新页面观察，不能点击。")
        if target.get("backend") == "dom":
            return self._resolve_dom_target(target, for_type=for_type)
        wrapper, entry = target["wrapper"], target["entry"]
        try:
            current_name = re.sub(r"\s+", " ", wrapper.element_info.name or "").strip()[:300] or "未标注输入框"
            role = wrapper.element_info.control_type
            current_rect = _intersect(_rect(wrapper.rectangle()), _rect(self._window.rectangle()))
            if current_name != entry["name"] or role != entry["role"] or current_rect is None or any(abs(a - b) > 4 for a, b in zip(current_rect, entry["rect"])):
                raise DesktopError("element_changed", "目标控件已变化或移动，请重新观察。")
            if not wrapper.is_visible() or not wrapper.is_enabled():
                raise DesktopError("element_unavailable", "目标控件不可见或不可操作，请重新观察。")
            if blocked_target(current_name):
                raise DesktopError("blocked_action", "此操作涉及下单、支付、账户变更、删除或发送消息，Agent 不执行。", needs_user=True)
            href = entry.get("href")
            if href and (not is_allowed_url(href, self._local_origins) or self._url_gate(href)):
                raise DesktopError("link_not_allowed", "目标链接离开京东或进入认证/结算区域，请由你处理。", needs_user=True)
            # A Text child can sit inside a forbidden Button/Hyperlink. Check
            # the actual ancestor semantics before a physical coordinate click.
            ancestor = wrapper
            for _ in range(3):
                ancestor = ancestor.parent()
                ancestor_role = ancestor.element_info.control_type
                if ancestor_role == "Document":
                    break
                if ancestor_role in {"Button", "Hyperlink"} and blocked_target(ancestor.element_info.name or ""):
                    raise DesktopError("blocked_action", "目标属于下单、支付、删除或账户操作控件，Agent 不执行。", needs_user=True)
            if for_type and (role not in {"Edit", "ComboBox"} or _SENSITIVE_EDIT.search(current_name) or self._is_password(wrapper)):
                raise DesktopError("not_safe_input", "只能向已识别的普通网页输入框输入，不填写认证或支付信息。")
            if entry["name"] == "未标注输入框" and not for_type:
                # It may be clicked only as the focus stage of a validated type.
                raise DesktopError("unlabeled_target", "目标缺少可识别名称，请使用明确的输入动作或重新观察。")
        except DesktopError:
            raise
        except Exception as exc:
            raise DesktopError("element_stale", "页面控件已失效，请重新观察。") from exc
        return target

    def _resolve_dom_target(self, target: dict[str, Any], *, for_type: bool = False) -> dict[str, Any]:
        from shopper.dom_reader import map_css_rect, native_render_rect
        entry = target["entry"]
        snapshot = self._dom_reader.snapshot(target["url"])
        fresh = next((e for e in snapshot["elements"] if e["dom_path"] == entry["dom_path"]), None)
        if not fresh:
            raise DesktopError("element_stale", "网页目标已消失，请重新观察。")
        rect = map_css_rect(fresh["css_rect"], native_render_rect(self._handle, snapshot["viewport"]), snapshot["viewport"])
        if fresh["name"] != entry["name"] or fresh["role"] != entry["role"] or fresh.get("href") != entry.get("href") or any(abs(a-b)>4 for a,b in zip(rect,entry["rect"])):
            raise DesktopError("element_changed", "网页目标已变化或移动，请重新观察。")
        if not fresh["enabled"]:
            raise DesktopError("element_unavailable", "目标控件不可操作。")
        if fresh.get("occluded"):
            raise DesktopError("target_occluded", "目标被网页内弹层遮挡，已停止点击。请处理弹层后重新观察。", needs_user=True)
        if blocked_target(fresh["name"]) or blocked_target(fresh.get("click_owner_name", "")):
            raise DesktopError("blocked_action", "此操作涉及下单、支付、账户变更、删除或发送消息，Agent 不执行。", needs_user=True)
        href = fresh.get("href")
        if href and (not is_allowed_url(href, self._local_origins) or self._url_gate(href)):
            raise DesktopError("link_not_allowed", "目标链接离开京东或进入认证/结算区域，请由你处理。", needs_user=True)
        if for_type and (fresh["role"] not in {"Edit", "ComboBox"} or _SENSITIVE_EDIT.search(fresh["name"])):
            raise DesktopError("not_safe_input", "只能向普通网页输入框输入。")
        return target

    def _click_target(self, target: dict[str, Any]) -> None:
        self._assert_foreground()
        box = target["entry"]["rect"]
        x, y = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
        if not self._gui.onScreen(x, y):
            raise DesktopError("off_primary_screen", "目标在主屏幕之外，请把项目浏览器移到主屏幕后继续。", needs_user=True)
        # Check the top-level native window under the physical target so an
        # overlay or another window cannot steal a mouse click.
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        hit = user32.WindowFromPoint(wintypes.POINT(x, y))
        root = user32.GetAncestor(hit, 2)  # GA_ROOT
        if int(root or 0) != self._handle:
            raise DesktopError("target_occluded", "目标被其他窗口遮挡，已停止点击。请移开遮挡窗口后继续。", needs_user=True)
        if self._pw_context is not None:
            self._pw_context.input(self._handle, "click", x=x, y=y)
            return
        self._gui.moveTo(x, y, duration=0.18)
        self._assert_foreground()
        self._gui.click()

    def _keys(self, *keys):
        if self._pw_context is not None:
            self._pw_context.input(self._handle, "keys", keys=list(keys))
        else:
            self._gui.hotkey(*keys)

    def _press(self, key):
        if self._pw_context is not None:
            self._pw_context.input(self._handle, "keys", keys=[key])
        else:
            self._gui.press(key)

    def _send_unicode(self, text: str) -> None:
        """Send Unicode keystrokes without reading or overwriting the clipboard."""
        if self._pw_context is not None:
            self._pw_context.input(self._handle, "type", text=text)
            return
        from ctypes import wintypes

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class INPUTUNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]

        encoded = text.encode("utf-16-le")
        events = []
        for index in range(0, len(encoded), 2):
            code = int.from_bytes(encoded[index:index + 2], "little")
            events.extend([INPUT(1, INPUTUNION(ki=KEYBDINPUT(0, code, 0x0004, 0, 0))),
                           INPUT(1, INPUTUNION(ki=KEYBDINPUT(0, code, 0x0004 | 0x0002, 0, 0)))])
        for start in range(0, len(events), 128):
            self._assert_foreground()
            self._gui.failSafeCheck()
            chunk = events[start:start + 128]
            array = (INPUT * len(chunk))(*chunk)
            sent = ctypes.windll.user32.SendInput(len(chunk), array, ctypes.sizeof(INPUT))
            if sent != len(chunk):
                raise DesktopError("input_failed", "Windows 未接受全部输入。请确保应用和浏览器权限级别一致。", needs_user=True)

    def act(self, action: dict[str, Any]) -> dict[str, Any]:
        try:
            self._init()
            if not isinstance(action, dict):
                raise DesktopError("invalid_action", "操作必须是结构化对象。")
            kind = action.get("action") or action.get("kind") or action.get("type")
            if kind not in {"click", "type", "press", "scroll", "back"}:
                raise DesktopError("invalid_action", "仅支持 click/type/press/scroll/back。")
            if kind == "back":
                # Recovery from external/login pages is allowed, still scoped
                # to a fresh observation and the owned browser.
                if not self._observation or action.get("obs_id") != self._observation.get("obs_id"):
                    raise DesktopError("stale_observation", "请重新观察后再后退。")
                self._find_owned_window()
            else:
                self._validate_observation(action)
            self._assert_foreground()
            target = None
            if kind in {"click", "type"}:
                target = self._resolve_target(str(action.get("element_id", "")), for_type=kind == "type")
            if kind == "type":
                text = action.get("text", "")
                if not isinstance(text, str) or len(text) > 2000 or any(ord(c) < 32 for c in text):
                    raise DesktopError("invalid_text", "输入需为不含控制字符、最多 2000 字符的文本。")
                self._click_target(target)
                self._assert_foreground()
                time.sleep(0.1)
                if not self._is_focused_edit(target):
                    raise DesktopError("input_focus_unverified", "点击后未确认输入框获得焦点，已停止输入。请重新观察。", needs_user=True)
                if action.get("clear", True):
                    self._keys("ctrl", "a")
                self._send_unicode(text)
                self._typed_target = target
            elif kind == "click":
                self._typed_target = None
                self._click_target(target)
            elif kind == "press":
                key = str(action.get("key", "")).lower()
                if key not in _SAFE_KEYS:
                    raise DesktopError("invalid_key", "该按键不在允许列表中，不执行快捷键组合。")
                # Enter can submit a form; allow only a known, focused search or
                # ordinary edit box, never a focus landed on a checkout button.
                if key == "enter":
                    if action.get("element_id"):
                        target = self._resolve_target(str(action["element_id"]), for_type=True)
                    else:
                        target = self._typed_target
                    if not target or not self._is_focused_edit(target):
                        raise DesktopError("unsafe_enter", "Enter 只能用于刚输入且仍获得焦点的普通网页输入框。")
                elif key not in {"esc", "pageup", "pagedown"}:
                    if not self._typed_target or not self._is_focused_edit(self._typed_target):
                        raise DesktopError("unsafe_key_focus", "该按键需要已验证的网页输入框焦点。")
                self._press(key)
                if key in {"enter", "tab", "esc"}:
                    self._typed_target = None
            elif kind == "scroll":
                amount = action.get("amount", -5)
                if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not -12 <= amount <= 12 or amount == 0:
                    raise DesktopError("invalid_scroll", "滚动量必须在 -12 至 12 之间且不为 0。")
                box = self._observation.get("render_rect")
                if not box:
                    raise DesktopError("page_unavailable", "网页区域不在可见窗口中。")
                if self._pw_context is not None:
                    self._pw_context.input(self._handle, "move", x=(box[0]+box[2])//2, y=(box[1]+box[3])//2)
                    self._pw_context.input(self._handle, "scroll", amount=int(amount))
                else:
                    self._gui.moveTo((box[0] + box[2]) // 2, (box[1] + box[3]) // 2, duration=0.15)
                    self._assert_foreground()
                    self._gui.scroll(int(amount))
                self._typed_target = None
            elif kind == "back":
                self._keys("alt", "left")
                self._typed_target = None
            self._invalidate()
            self._wait_after_action(extra=0.4 if kind in {"click", "back"} else 0)
            return self._with_observation(self._observe())
        except Exception as exc:
            return self._error(exc)

    def _is_focused_edit(self, target: dict[str, Any]) -> bool:
        try:
            if target.get("backend") == "dom":
                snapshot = self._dom_reader.snapshot(target["url"])
                return snapshot["active_edit"] and snapshot["active_path"] == target["entry"]["dom_path"]
            wrapper = target["wrapper"]
            return bool(wrapper.element_info.element.CurrentHasKeyboardFocus) and not self._is_password(wrapper) and not _SENSITIVE_EDIT.search(wrapper.element_info.name or "")
        except Exception:
            return False

    def screenshot(self, path: str | Path) -> dict[str, Any]:
        """Explicit, local evidence capture only; never invoked by observe()."""
        try:
            self._init()
            self._find_owned_window()
            self._assert_foreground()
            url = self._read_url(self._window)
            if not is_allowed_url(url, self._local_origins) or self._url_gate(url):
                raise DesktopError("screenshot_not_allowed", "仅能保存京东购物页面或本地测试页的证据截图。")
            destination = Path(path).resolve()
            if not destination.is_relative_to(self.root):
                raise DesktopError("path_not_allowed", "截图只能保存在项目目录内。")
            box = _rect(self._window.rectangle())
            screen = self._gui.size()
            box = _intersect(box, [0, 0, screen.width, screen.height])
            if not box:
                raise DesktopError("window_offscreen", "浏览器不在主屏幕，无法保存截图。")
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._gui.screenshot(region=(box[0], box[1], box[2] - box[0], box[3] - box[1])).save(str(destination))
            return {"status": "ok", "path": str(destination), "message": "已保存专用浏览器窗口截图。"}
        except Exception as exc:
            return self._error(exc)
