"""Attach to a user-opened browser without launching or replacing its profile."""
from .browser_bridge import NodeBrowserBridge
from .browser_mouse import BrowserMouseController


class ExistingBrowserController(BrowserMouseController):
    input_mode = "existing_browser"

    def _command(self, operation: str, **kwargs):
        return self._bridge.existing(operation, **kwargs)

    def health(self) -> dict:
        ready = NodeBrowserBridge.available(self.root)
        return {"status": "ok" if ready else "error", "input_mode": self.input_mode,
                "message": "连接你已经打开的 Edge 京东标签页，沿用现有登录状态。首次请在 Edge 的 edge://inspect/#remote-debugging 中允许此浏览器实例的调试连接。" if ready else "浏览器运行依赖缺失，请运行安装依赖.cmd。",
                "observation_mode": "当前京东页面文字和控件，默认不截图", "requires_foreground": False,
                "connection_required": True, "setup_url": "edge://inspect/#remote-debugging"}

    def open_jd(self, page_id: str | None = None) -> dict:
        try:
            self._checkpoint()
            self._observation = None
            payload = {"channel": "chrome" if self.browser == "chrome" else "msedge"}
            if page_id is not None:
                if not isinstance(page_id, str) or not 1 <= len(page_id) <= 128:
                    raise ValueError("请选择本次连接列出的京东标签页。")
                payload["page_id"] = page_id
            raw = self._command("open", **payload)
            self._opened = True
            return self._result(raw)
        except Exception as exc:
            return self._error(exc)

    def open_url(self, url: str, allow_local: bool = False) -> dict:
        return {"status": "needs_user", "error": "existing_browser_only",
                "message": "请先在自己的 Edge 中打开京东，然后连接已有标签页。程序不会另开购物浏览器。"}
