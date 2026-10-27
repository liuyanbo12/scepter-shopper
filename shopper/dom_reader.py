"""Fixed read-only DOM inspection over an isolated browser's private pipe.

No model-supplied JavaScript, selectors, network URLs, or CDP methods are accepted.
Mouse clicks and typing are intentionally absent: desktop.py performs those with
physical Windows input. Only the project browser context is read; legacy recovery can use loopback CDP.
"""
from __future__ import annotations

import ctypes
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, build_opener


SNAPSHOT_SCRIPT = (Path(__file__).with_name("dom_snapshot.js")).read_text(encoding="utf-8")


def same_page_url(left: str, right: str) -> bool:
    """Chromium may display decoded Unicode while CDP reports percent escapes."""
    try:
        a, b = urlsplit(left), urlsplit(right)
        return (a.scheme.lower(), a.netloc.lower(), unquote(a.path or "/"), unquote(a.query)) == (
            b.scheme.lower(), b.netloc.lower(), unquote(b.path or "/"), unquote(b.query))
    except ValueError:
        return False


def map_css_rect(css_rect: list[float], render_rect: list[int], viewport: dict[str, Any]) -> list[int]:
    width, height = float(viewport["width"]), float(viewport["height"])
    if width <= 0 or height <= 0:
        raise ValueError("页面视口尺寸无效。")
    scale_x = (render_rect[2] - render_rect[0]) / width
    scale_y = (render_rect[3] - render_rect[1]) / height
    if not 0.4 <= scale_x <= 5 or abs(scale_x - scale_y) > max(0.08, scale_x * 0.04):
        raise ValueError("浏览器视口比例不一致，无法安全映射鼠标坐标。")
    if abs(float(viewport.get("visual_scale", 1)) - 1) > 0.02:
        raise ValueError("请关闭页面捏合缩放后继续。普通浏览器缩放仍受支持。")
    return [round(render_rect[0] + css_rect[0] * scale_x), round(render_rect[1] + css_rect[1] * scale_y),
            round(render_rect[0] + css_rect[2] * scale_x), round(render_rect[1] + css_rect[3] * scale_y)]


def native_render_rect(handle: int, viewport: dict[str, Any]) -> list[int]:
    """Read native HWND geometry without traversing webpage accessibility nodes."""
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    candidates: list[list[int]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    def visit(child, _):
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, buffer, 256)
        if buffer.value == "Chrome_RenderWidgetHostHWND" and user32.IsWindowVisible(child):
            box = wintypes.RECT()
            if user32.GetWindowRect(child, ctypes.byref(box)):
                if box.right - box.left > 200 and box.bottom - box.top > 100:
                    candidates.append([box.left, box.top, box.right, box.bottom])
        return True

    callback = callback_type(visit)
    user32.EnumChildWindows(handle, callback, 0)
    if candidates:
        return max(candidates, key=lambda r: (r[2]-r[0]) * (r[3]-r[1]))
    # Chromium versions without a native renderer child: bottom-align the web
    # viewport inside the native browser client using the reported DPR. Never
    # infer a fixed toolbar height; this also handles Windows DPI and page zoom.
    box, point = wintypes.RECT(), wintypes.POINT(0, 0)
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    if not user32.GetClientRect(handle, ctypes.byref(box)) or not user32.ClientToScreen(handle, ctypes.byref(point)):
        raise ValueError("无法读取浏览器原生客户区。")
    scale = (box.right - box.left) / float(viewport["width"])
    if abs(scale - float(viewport["dpr"])) > max(0.08, scale * 0.04):
        raise ValueError("浏览器边栏或缩放使视口无法对齐，请关闭边栏后继续。")
    height = round(float(viewport["height"]) * scale)
    bottom = point.y + box.bottom
    if height > box.bottom - box.top:
        raise ValueError("页面视口超出浏览器客户区。")
    return [point.x, bottom-height, point.x+box.right, bottom]


class DomReader:
    def __init__(self, profile_dir: Path, context=None):
        self.profile_dir = Path(profile_dir)
        self.context = context
        self._opener = build_opener(ProxyHandler({}))

    def _port(self) -> int:
        try:
            first_line = (self.profile_dir / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0]
            port = int(first_line)
            if not 1024 <= port <= 65535:
                raise ValueError("port out of range")
            return port
        except (OSError, ValueError, IndexError) as exc:
            raise RuntimeError("专用浏览器尚未开放本地页面读取端口，请关闭项目浏览器后重新打开。") from exc

    def snapshot(self, expected_url: str) -> dict[str, Any]:
        if self.context is not None and hasattr(self.context, "snapshot"):
            return self.context.snapshot(expected_url)
        if self.context is not None:
            for page in self.context.pages:
                if not same_page_url(page.url, expected_url):
                    continue
                if page.evaluate("document.visibilityState") != "visible":
                    continue
                snapshot = page.evaluate(SNAPSHOT_SCRIPT)
                if not isinstance(snapshot, dict) or not same_page_url(snapshot.get("url", ""), expected_url):
                    raise RuntimeError("读取过程中页面已跳转，请重新观察。")
                return snapshot
            raise RuntimeError("当前标签与前台浏览器地址不一致，请重新观察。")
        import websocket
        port = self._port()
        with self._opener.open(f"http://127.0.0.1:{port}/json/list", timeout=4) as response:
            targets = json.load(response)
        matches = [t for t in targets if t.get("type") == "page" and same_page_url(t.get("url", ""), expected_url)]
        if not matches:
            raise RuntimeError("页面正在跳转或当前标签与浏览器地址不一致，请重新观察。")
        # Several tabs may have the same URL. Read-only visibility inspection
        # chooses the active tab, preventing clicks against a background tab.
        for target in matches:
            address = target.get("webSocketDebuggerUrl", "")
            parsed = urlsplit(address)
            if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != port:
                continue
            # Use an explicit loopback host irrespective of debugger response.
            address = f"ws://127.0.0.1:{port}{parsed.path}"
            connection = websocket.create_connection(address, timeout=6, suppress_origin=True, http_proxy_host=None)
            try:
                connection.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
                    "expression": "document.visibilityState", "returnByValue": True}}))
                visibility = self._response(connection, 1)
                if visibility.get("result", {}).get("value") != "visible":
                    continue
                connection.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
                    "expression": SNAPSHOT_SCRIPT, "returnByValue": True}}))
                response = self._response(connection, 2)
                if response.get("exceptionDetails"):
                    raise RuntimeError("页面结构读取失败，请等待页面加载后继续。")
                snapshot = response.get("result", {}).get("value")
                if not isinstance(snapshot, dict) or not same_page_url(snapshot.get("url", ""), expected_url):
                    raise RuntimeError("读取过程中页面已跳转，请重新观察。")
                return snapshot
            finally:
                connection.close()
        raise RuntimeError("没有与前台地址相符的可见标签页。")

    @staticmethod
    def _response(connection, message_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            payload = json.loads(connection.recv())
            if payload.get("id") == message_id:
                if "error" in payload:
                    raise RuntimeError("浏览器拒绝读取页面结构：" + str(payload["error"].get("message", ""))[:200])
                return payload.get("result", {})
        raise TimeoutError("浏览器页面读取超时。")
