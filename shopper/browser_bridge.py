"""Authenticated loopback requests to the locally started browser host.

The host exposes fixed commands for a dedicated Playwright mouse/keyboard
executor and the optional native Windows input driver. Both revalidate observed
targets before input. No model JavaScript is accepted.
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.request import Request, ProxyHandler, build_opener


class BrowserBridgeError(RuntimeError):
    def __init__(self, message: str, code: str = "browser_error", needs_user: bool = True):
        super().__init__(message)
        self.code, self.needs_user = code, needs_user


class NodeBrowserBridge:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.client_id = uuid.uuid4().hex
        self._counter = 0
        self._launched = False
        self._attached = False
        self._opener = build_opener(ProxyHandler({}))

    @staticmethod
    def available(root: Path | str) -> bool:
        runtime = Path(root) / 'browser-runtime/node_modules'
        return bool(shutil.which('node') and all((runtime / package / 'package.json').is_file() for package in ('playwright','koffi')))

    def _request(self, command: str, timeout: float = 12, **kwargs) -> dict[str, Any]:
        try:
            connection = json.loads((self.root / 'data/browser-bridge.json').read_text(encoding='utf-8'))
            port, token = int(connection['port']), connection['token']
            if not 1024 <= port <= 65535 or len(token) != 64:
                raise ValueError('Invalid local broker configuration')
        except (OSError, ValueError, KeyError) as exc:
            raise RuntimeError('浏览器执行服务尚未启动，请通过启动入口打开本应用。') from exc
        self._counter += 1
        payload = {'id':self._counter,'client_id':self.client_id,'command':command,**kwargs}
        request = Request(f'http://127.0.0.1:{port}/command',
                          data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),
                          headers={'Content-Type':'application/json','Authorization':'Bearer '+token}, method='POST')
        try:
            with self._opener.open(request,timeout=timeout) as response:
                answer = json.load(response)
        except OSError as exc:
            raise RuntimeError('本地浏览器执行服务未响应，请重新启动应用。') from exc
        if answer.get('id') != self._counter or answer.get('error'):
            raise BrowserBridgeError(str(answer.get('error') or '浏览器响应顺序不匹配。'),
                                     str(answer.get('code') or 'browser_error'), bool(answer.get('needs_user', True)))
        return answer['result']

    def launch(self, profile_dir: Path | str, channel: str = 'msedge', app_url: str | None = None, start_url: str | None = None, executable: str | None = None):
        payload = {'profile_dir': str(Path(profile_dir).resolve()), 'channel': channel}
        if app_url:
            payload['app_url'] = app_url
        if start_url:
            payload['start_url'] = start_url
        if executable:
            payload['executable'] = executable
        result = self._request('launch', timeout=45, **payload)
        self._launched = True
        return result

    def connect(self, port: int) -> dict[str, Any]:
        result = self._request('connect', timeout=20, port=int(port))
        self._attached = True
        return result

    def detach(self) -> dict[str, Any]:
        if not self._attached:
            return {"status": "ok"}
        try:
            return self._request('close', timeout=8, detach_only=True)
        finally:
            self._attached = False
            self._launched = False

    def snapshot(self, expected_url: str) -> dict[str, Any]:
        return self._request('snapshot',expected_url=expected_url)

    def current(self) -> dict[str, Any]:
        return self._request('current')

    def input(self, hwnd: int, operation: str, **kwargs):
        return self._request('input',hwnd=hwnd,operation=operation,**kwargs)

    def check_input_access(self):
        """Check the actual Node sender, which may differ from Python's token."""
        return self._request('input_access')

    def virtual(self, operation: str, **kwargs):
        if operation not in {'open', 'navigate', 'snapshot', 'act', 'show', 'close', 'screenshot'}:
            raise ValueError('不支持的虚拟鼠标操作')
        return self._request('virtual_' + operation, timeout=50 if operation == 'open' else 35, **kwargs)

    def existing(self, operation: str, **kwargs):
        if operation not in {'open', 'navigate', 'snapshot', 'act', 'show', 'close', 'screenshot'}:
            raise ValueError('不支持的现有浏览器操作')
        # Edge waits for a human to approve this specific WebSocket request.
        # Keep the HTTP bridge alive beyond the browser's 180-second deadline.
        return self._request('existing_' + operation, timeout=195 if operation == 'open' else 35, **kwargs)

    def wait_closed(self):
        while self._launched:
            if self._request('status').get('closed'):
                return
            time.sleep(0.7)

    def close(self):
        if self._launched:
            try:
                self._request('close',timeout=8)
            finally:
                self._launched = False
