"""Playwright page mouse adapter: rendered DOM reads, no Windows foreground input.

The local Node service owns a dedicated visible shopping browser. Python sends
only fixed commands and observed element IDs, never model scripts or selectors.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .browser_bridge import NodeBrowserBridge
from .desktop import DesktopError, blocked_target, is_allowed_url, normalize_url


class BrowserMouseController:
    input_mode = "browser_mouse"

    def __init__(self, root: Path | str, browser: str = "edge", max_elements: int = 180,
                 action_delay: float = 0.6, stop_check: Callable[[], Any] | None = None):
        self.root = Path(root).resolve()
        self.browser = "chrome" if browser.lower() == "chrome" else "edge"
        self.max_elements = max(20, min(int(max_elements), 500))
        self.action_delay = max(0.1, min(float(action_delay), 3))
        self.stop_check = stop_check
        self.profile_dir = self.root / "data" / "browser-profile" / ("virtual-" + self.browser)
        self._bridge = NodeBrowserBridge(self.root)
        self._opened = False
        self._local_origins: set[str] = set()
        self._observation: dict | None = None

    def health(self) -> dict:
        ready = NodeBrowserBridge.available(self.root)
        return {"status": "ok" if ready else "error", "input_mode": "browser_mouse",
                "message": "使用专用京东窗口和浏览器内虚拟鼠标；Agent 页面可以保持打开。" if ready else "浏览器运行依赖缺失，请运行安装依赖.cmd。",
                "observation_mode": "页面文字和控件，默认不截图", "requires_foreground": False}

    def _checkpoint(self):
        if self.stop_check and self.stop_check():
            raise DesktopError("stopped", "任务已暂停或停止。", needs_user=True)

    def _command(self, operation: str, **kwargs):
        return self._bridge.virtual(operation, **kwargs)

    @staticmethod
    def _error(exc: Exception) -> dict:
        return {"status": "needs_user", "error": getattr(exc, "code", "browser_error"),
                "message": str(exc)[:800]}

    def _result(self, raw: dict) -> dict:
        self._checkpoint()
        observation = copy.deepcopy(raw.get("observation", raw))
        if not observation.get("obs_id"):
            if observation.get("status") in {"needs_user", "error", "blocked"}:
                return observation
            raise DesktopError("invalid_snapshot", "浏览器未返回页面观察编号。", needs_user=True)
        if not is_allowed_url(observation.get("url", ""), self._local_origins):
            observation.update(status="needs_user", error="outside_allowed_site", text="", elements=[],
                               message="购物窗口已离开京东，请手动返回京东后继续。")
        observation.setdefault("status", "ok")
        observation.update(source="dom", input_mode=self.input_mode, screenshot=None,
                           window={"mode": self.input_mode, "requires_foreground": False})
        elements = observation.get("elements", [])
        interactive = [entry for entry in elements if entry.get("role") != "Text"]
        texts = [entry for entry in elements if entry.get("role") == "Text"]
        reserve = max(int(self.max_elements * 0.7), self.max_elements - len(texts))
        chosen = interactive[:reserve] + texts[:self.max_elements-min(len(interactive), reserve)]
        for entry in chosen:
            entry["rect"] = entry.get("css_rect", entry.get("rect", []))
            if blocked_target(entry.get("name", "")):
                entry["blocked"] = True
        observation["elements"] = chosen
        observation["truncated"] = len(chosen) < len(elements)
        self._observation = observation
        result = {"status": observation["status"], "observation": observation}
        for key in ("message", "error", "tabs"):
            if key in observation:
                result[key] = observation[key]
            elif key in raw:
                result[key] = raw[key]
        return result

    def open_jd(self) -> dict:
        return self.open_url("https://www.jd.com/")

    def open_url(self, url: str, allow_local: bool = False) -> dict:
        try:
            self._checkpoint()
            url = normalize_url(url)
            parsed = urlsplit(url)
            if allow_local and parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port and not parsed.username:
                self._local_origins.add(f"http://127.0.0.1:{parsed.port}")
            if not is_allowed_url(url, self._local_origins):
                raise DesktopError("url_not_allowed", "仅允许京东页面或明确登记的本地测试页面。", needs_user=True)
            raw = self._command("open", profile_dir=str(self.profile_dir),
                                       channel="chrome" if self.browser == "chrome" else "msedge",
                                       start_url=url, allowed_local_origins=sorted(self._local_origins))
            self._opened = True
            return self._result(raw)
        except Exception as exc:
            return self._error(exc)

    def navigate(self, url: str) -> dict:
        try:
            self._checkpoint()
            if not is_allowed_url(url, self._local_origins):
                raise DesktopError("url_not_allowed", "页面地址不在允许范围。", needs_user=True)
            self._observation = None
            return self._result(self._command("navigate", url=url))
        except Exception as exc:
            return self._error(exc)

    def observe(self) -> dict:
        try:
            self._checkpoint()
            self._observation = None
            return self._result(self._command("snapshot"))
        except Exception as exc:
            return self._error(exc)

    def focus(self) -> dict:
        """Show only the shopping page; Windows focus is not an input condition."""
        try:
            self._checkpoint()
            self._observation = None
            return self._result(self._command("show"))
        except Exception as exc:
            return self._error(exc)

    def act(self, action: dict) -> dict:
        # Model-originated actions cannot request an implicit form submission.
        return self._act(action)

    def search(self, action: dict) -> dict:
        """Submit one engine-selected search input within one focus scope."""
        return self._act(action, submit_search=True)

    def return_to_search(self, obs_id: str) -> dict:
        """Return only to this task's saved results page, never an arbitrary tab."""
        try:
            self._checkpoint()
            observation = self._observation
            if not observation or observation.get("status") != "ok" or obs_id != observation.get("obs_id"):
                raise DesktopError("stale_observation", "页面观察已过期，请重新读取后操作。", needs_user=True)
            self._observation = None
            return self._result(self._command("act", kind="return_search", obs_id=obs_id,
                                              expected_url=observation["url"]))
        except Exception as exc:
            return self._error(exc)

    def _act(self, action: dict, submit_search: bool = False) -> dict:
        try:
            self._checkpoint()
            obs = self._observation
            if not obs or obs.get("status") != "ok" or action.get("obs_id") != obs["obs_id"]:
                raise DesktopError("stale_observation", "页面观察已过期，请重新读取后操作。", needs_user=True)
            kind = action.get("action", action.get("kind"))
            if kind not in {"click", "type", "press", "scroll", "back"}:
                raise ValueError("不支持的页面操作")
            if submit_search and kind != "type":
                raise ValueError("搜索提交必须包含搜索框和关键词")
            payload = {"kind": kind, "obs_id": obs["obs_id"], "expected_url": obs["url"]}
            if kind in {"click", "type"}:
                target = next((e for e in obs["elements"] if e["id"] == action.get("element_id")), None)
                if not target or target.get("blocked") or target.get("occluded") or target.get("enabled") is False:
                    raise DesktopError("target_unavailable", "目标不在当前可操作页面中，或被页面弹窗遮挡。", needs_user=True)
                payload["element_id"] = target["id"]
                if submit_search:
                    if kind != "type" or target.get("role") not in {"Edit", "ComboBox"} or not re.search(r"搜索|search|关键词", str(target.get("name", "")), re.I) or action.get("allow_quantity") or action.get("allow_add"):
                        raise ValueError("仅可提交已识别的商品搜索框")
                    payload["submit_search"] = True
                if kind == "type":
                    value = action.get("text", "")
                    if not isinstance(value, str) or len(value) > 400 or re.search(r"[\x00-\x1f]", value):
                        raise ValueError("输入内容无效")
                    payload.update(text=value, clear=action.get("clear", True) is not False)
                # These fields are added by the engine only after selection
                # approval. guard_action strips model-provided authorization.
                payload.update(allow_add=action.get("allow_add") is True,
                               allow_quantity=action.get("allow_quantity") is True,
                               approved_sku=str(action.get("approved_sku") or ""),
                               approved_quantity=action.get("approved_quantity", 1))
            elif kind == "press":
                key = str(action.get("key", "")).lower()
                if key not in {"enter", "esc", "escape", "pageup", "pagedown", "up", "down", "left", "right"}:
                    raise ValueError("按键不在允许范围")
                payload["key"] = key
            elif kind == "scroll":
                amount = action.get("amount", -5)
                if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 < abs(amount) <= 12:
                    raise ValueError("滚动幅度无效")
                payload["amount"] = int(amount)
            self._observation = None  # Outcome is uncertain after dispatch; never replay an old action.
            return self._result(self._command("act", **payload))
        except Exception as exc:
            return self._error(exc)

    def screenshot(self, path: str | Path) -> dict:
        try:
            self._checkpoint()
            destination = Path(path).resolve()
            if not destination.is_relative_to(self.root):
                raise ValueError("证据文件必须位于项目目录")
            return self._command("screenshot", path=str(destination))
        except Exception as exc:
            return self._error(exc)

    def close(self) -> dict:
        try:
            if self._opened:
                self._command("close")
            return {"status": "ok"}
        except Exception as exc:
            return self._error(exc)
        finally:
            self._opened = False
            self._observation = None
