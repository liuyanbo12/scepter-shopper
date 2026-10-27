"""Local Qwen via Ollama: candidate judgments only, never browser actions."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .llm import ModelError

# A single failed /api/tags ping (Edge connect, GPU busy, VPN proxy) must not
# flip ready off or spawn another `ollama serve`. Keep the last good answer.
_TAGS_STICKY_SECONDS = 15.0
_LOCAL_NO_PROXY = "127.0.0.1,localhost,::1"
_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
# Windows VPN clients often set a system proxy. Local Ollama must never use it.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


JUDGE_PROMPT = """只判断给定候选是否符合当前需求。返回JSON：
{"items":[{"id":"listing_编号","verdict":"accept|reject|unverified","reason":"不超过40字"}]}
只能使用提供的标题、规格、价格、店铺和 SKU。读不到的价格或版本必须 unverified，不能补写。
不要评价未提供的商品，不要给出购买建议或价格估算。"""


def find_ollama() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    home = Path(os.environ.get("LOCALAPPDATA") or "")
    for candidate in (
        home / "Programs" / "Ollama" / "ollama.exe",
        Path(r"C:\Program Files\Ollama\ollama.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def resolve_models_dir(root: Path, models_dir: str | Path | None = None) -> Path:
    if models_dir:
        return Path(models_dir)
    env = (os.environ.get("OLLAMA_MODELS") or "").strip()
    if env:
        return Path(env)
    return Path(root) / "data" / "ollama-models"


class LocalQwenService:
    def __init__(self, root: Path, model: str = "qwen2.5:3b",
                 base_url: str = "http://127.0.0.1:11434", timeout: int = 120,
                 models_dir: str | Path | None = None):
        self.root = Path(root)
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.models_dir = resolve_models_dir(self.root, models_dir)
        self.path = self.root / "data" / "local-model.json"
        self._state = "idle"
        self._message = "未启用本地 Qwen。"
        self._progress = ""
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._owned_process = None
        self._last_status: dict | None = None
        self._last_good_tags: dict | None = None
        self._last_good_at = 0.0
        saved = self._read()
        if saved.get("enabled"):
            self.model = str(saved.get("model") or self.model)
            self._state = "starting"
            self._message = "打开 Agent 时会启动本地 Qwen，连接 Edge 不会重新启动。"

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _http_client(self, timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, trust_env=False)

    def _live_tags(self, timeout: float = 2.0) -> dict | None:
        try:
            request = urllib.request.Request(self.base_url + "/api/tags")
            with _NO_PROXY_OPENER.open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

    def _tags(self) -> dict | None:
        payload = self._live_tags()
        if payload is not None:
            if self._model_present(payload):
                self._last_good_tags = payload
                self._last_good_at = time.monotonic()
                return payload
            if self._last_good_tags is not None and (time.monotonic() - self._last_good_at) < _TAGS_STICKY_SECONDS:
                return self._last_good_tags
            return payload
        if self._last_good_tags is not None and (time.monotonic() - self._last_good_at) < _TAGS_STICKY_SECONDS:
            return self._last_good_tags
        return None

    def _port_up(self) -> bool:
        parsed = urlparse(self.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 11434
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            return False

    def _model_present(self, payload: dict | None) -> bool:
        names = [str(item.get("name") or item.get("model") or "") for item in (payload or {}).get("models", [])]
        return any(self.model == name or name.startswith(self.model + ":") or name.startswith(self.model) for name in names)

    def _model_files_present(self) -> bool:
        name, _, tag = self.model.partition(":")
        tag = tag or "latest"
        library = self.models_dir / "manifests" / "registry.ollama.ai" / "library" / name / tag
        return library.is_file()

    def available(self) -> bool:
        if not self._read().get("enabled"):
            return False
        return self._model_present(self._tags())

    def status(self, cached: bool = False) -> dict:
        # Only reuse a ready snapshot. A "starting" freeze would hide Qwen in
        # the footer for the whole Edge-connect wait.
        if cached and self._last_status and self._last_status.get("ready"):
            return dict(self._last_status)
        runtime = find_ollama()
        tags = self._tags()
        enabled = bool(self._read().get("enabled"))
        files = self._model_files_present()
        ready = self._model_present(tags) or (enabled and files and (self._port_up() or self._last_good_tags is not None))
        if not runtime and self._state != "downloading":
            state, message = "missing_runtime", "未检测到 Ollama。请先安装 Ollama，再点击下载并启用本地模型。"
        elif self._state == "downloading":
            state, message = self._state, self._message
        elif ready and enabled:
            state, message = "ready", "本地 Qwen 已就绪，候选筛选不再调用 DeepSeek。"
            self._state, self._message = state, message
        elif enabled and tags is not None and not files:
            state, message = "idle", "Ollama 已在运行，还需要下载千问 3B。"
        elif enabled or self._state == "starting":
            state, message = "starting", self._message if self._state == "starting" else "已启用本地模型，正在等待 Ollama 启动。"
        else:
            state, message = "idle", "未启用本地 Qwen。筛选仍会调用 DeepSeek，可能产生费用。"
        result = {
            "state": state,
            "model": self.model,
            "label": f"本地 Qwen（{self.model}）",
            "enabled": enabled,
            "ready": ready and enabled,
            "runtime_found": bool(runtime),
            "progress": self._progress,
            "message": message,
            "setup_url": "https://ollama.com/download",
            "models_dir": str(self.models_dir),
        }
        self._last_status = result
        return result

    def _serve_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["OLLAMA_MODELS"] = str(self.models_dir)
        for key in _PROXY_ENV:
            env.pop(key, None)
        env["NO_PROXY"] = _LOCAL_NO_PROXY
        env["no_proxy"] = _LOCAL_NO_PROXY
        return env

    def _serve_running(self) -> bool:
        process = self._owned_process
        return process is not None and process.returncode is None

    async def _wait_for_model(self, seconds: float = 20.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            payload = self._live_tags(timeout=3.0)
            if self._model_present(payload):
                self._last_good_tags = payload
                self._last_good_at = time.monotonic()
                return True
            await asyncio.sleep(0.4)
        return self._model_present(self._tags())

    async def _start_serve(self, runtime: str) -> None:
        if self._live_tags() is not None or self._serve_running() or self._port_up():
            return
        self.models_dir.mkdir(parents=True, exist_ok=True)
        kwargs: dict = {
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.DEVNULL,
            "env": self._serve_env(),
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self._owned_process = await asyncio.create_subprocess_exec(runtime, "serve", **kwargs)
        for _ in range(20):
            if self._live_tags() is not None:
                return
            await asyncio.sleep(0.5)

    async def _ensure_running_locked(self) -> None:
        tags = self._tags()
        if self._model_present(tags):
            if self._read().get("enabled"):
                self._state, self._message = "ready", "本地 Qwen 已就绪，候选筛选不再调用 DeepSeek。"
            return
        if self._serve_running() or self._port_up() or (tags is not None):
            # A server is already bound. Wait for the model; never spawn another.
            if await self._wait_for_model():
                self._state, self._message = "ready", "本地 Qwen 已就绪，候选筛选不再调用 DeepSeek。"
            return
        runtime = find_ollama()
        if not runtime:
            self._state, self._message = "missing_runtime", "未检测到 Ollama。请先安装 Ollama，再点击下载并启用本地模型。"
            return
        self._state, self._message = "starting", "打开 Agent 时会启动本地 Qwen，连接 Edge 不会重新启动。"
        await self._start_serve(runtime)
        if await self._wait_for_model():
            self._state, self._message = "ready", "本地 Qwen 已就绪，候选筛选不再调用 DeepSeek。"

    async def ensure_running(self) -> None:
        async with self._lock:
            await self._ensure_running_locked()

    async def enable(self) -> dict:
        async with self._lock:
            runtime = find_ollama()
            if not runtime:
                self._state = "missing_runtime"
                self._message = "未检测到 Ollama。请先安装 https://ollama.com/download 中的 Ollama，安装后再次点击。"
                return self.status()
            if (self._model_present(self._tags()) or self._model_files_present()) and self._read().get("enabled"):
                self._state, self._message, self._progress = "ready", "本地 Qwen 已就绪，候选筛选不再调用 DeepSeek。", ""
                return self.status()
            self._state, self._message, self._progress = "downloading", "正在下载并启动本地 Qwen…", ""
            try:
                await self._ensure_running_locked()
                if self._live_tags() is None and not self._port_up():
                    raise ModelError("Ollama 已安装但未能启动服务。")
                if not self._model_present(self._tags()) and not self._model_files_present():
                    await self._pull()
                self._write({"enabled": True, "model": self.model,
                             "models_dir": str(self.models_dir),
                             "enabled_at": datetime.now(timezone.utc).isoformat()})
                self._state, self._message, self._progress = "ready", "本地 Qwen 已就绪，候选筛选不再调用 DeepSeek。", ""
            except Exception as exc:
                self._state, self._message = "error", str(exc)[:300]
            return self.status()

    async def stop(self) -> None:
        async with self._lock:
            process = self._owned_process
            self._owned_process = None
            if process is None or process.returncode is not None:
                return
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            if self._state != "downloading":
                self._state, self._message = "idle", "本地 Qwen 已随 Agent 关闭。"

    async def _pull(self) -> None:
        async with self._http_client(timeout=None) as client:
            async with client.stream("POST", self.base_url + "/api/pull",
                                     json={"name": self.model, "stream": True}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    status = str(payload.get("status") or "")
                    completed, total = payload.get("completed"), payload.get("total")
                    if isinstance(completed, int) and isinstance(total, int) and total > 0:
                        self._progress = f"{completed * 100 // total}%"
                    self._message = status or self._message
                    if payload.get("error"):
                        raise ModelError(str(payload["error"])[:300])

    async def judge(self, requirement: dict, records: list[dict]) -> dict:
        if not self.available():
            raise ModelError("本地 Qwen 尚未就绪。")
        payload = {"requirement": requirement, "candidates": records}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with self._http_client(timeout=self.timeout) as client:
            response = await client.post(self.base_url + "/api/chat", json={
                "model": self.model,
                "messages": [{"role": "system", "content": JUDGE_PROMPT},
                             {"role": "user", "content": body}],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1},
            })
        if response.status_code >= 400:
            raise ModelError(f"本地 Qwen 返回 HTTP {response.status_code}。")
        data = response.json()
        content = ((data.get("message") or {}).get("content") or "").strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ModelError("本地 Qwen 没有返回可解析的 JSON。") from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
            raise ModelError("本地 Qwen 返回了无法核验的判断结果。")
        usage = {
            "prompt_tokens": int(data.get("prompt_eval_count") or 0),
            "completion_tokens": int(data.get("eval_count") or 0),
        }
        return {"items": parsed["items"], "usage": usage, "elapsed_ms": int((data.get("total_duration") or 0) / 1e6)}

    async def complete_json(self, system: str, data: dict) -> dict:
        if not self.available():
            raise ModelError("本地 Qwen 尚未就绪。")
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        async with self._http_client(timeout=self.timeout) as client:
            response = await client.post(self.base_url + "/api/chat", json={
                "model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": body}],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1},
            })
        if response.status_code >= 400:
            raise ModelError(f"本地 Qwen 返回 HTTP {response.status_code}。")
        payload = response.json()
        content = ((payload.get("message") or {}).get("content") or "").strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ModelError("本地 Qwen 没有返回可解析的 JSON。") from exc
        if not isinstance(parsed, dict):
            raise ModelError("本地 Qwen 返回了无法核验的 JSON。")
        usage = {
            "prompt_tokens": int(payload.get("prompt_eval_count") or 0),
            "completion_tokens": int(payload.get("eval_count") or 0),
        }
        return {"data": parsed, "usage": usage}
