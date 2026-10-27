from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import ROOT, Settings, load_settings
from .llm import DeepSeekClient, safe_error
from .local_qwen import LocalQwenService
from .storage import Store, TERMINAL
from .runtime_build import build_fingerprint


def desktop_class(settings):
    mode = getattr(settings, "input_mode", "existing_browser")
    if mode == "existing_browser":
        from .existing_browser import ExistingBrowserController
        return ExistingBrowserController
    if mode == "browser_mouse":
        from .browser_mouse import BrowserMouseController
        return BrowserMouseController
    from .desktop import DesktopController
    return DesktopController


class StartRequest(BaseModel):
    prompt: str = Field(min_length=4, max_length=6000)

    @field_validator("prompt")
    @classmethod
    def meaningful_prompt(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 4:
            raise ValueError("请至少输入4个字符的购物需求")
        return value


class ControlRequest(BaseModel):
    action: str = Field(pattern=r"^(pause|resume|stop|approve|revise_search)$")
    selection: list[str] | None = None


class ValidateRequest(BaseModel):
    selection: list[str] = Field(max_length=40)


class BrowserOpenRequest(BaseModel):
    page_id: str | None = Field(default=None, min_length=1, max_length=200)


class Runtime:
    def __init__(self, root: Path):
        self.root = root
        self.store = Store(root / "data" / "runs")
        self.engine = None
        self.client = None
        self.desktop = None
        self.settings = load_settings(root)
        # Fixed for this process: recomputing in /health would falsely report
        # that an old imported server has loaded recently edited source files.
        self.runtime_build = build_fingerprint(root)
        host_build = os.environ.get("SCEPTER_HOST_BUILD", "")
        self.host_build = host_build if re.fullmatch(r"[a-f0-9]{64}", host_build) else None
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.browser_open_task: asyncio.Task | None = None
        self.browser_open_page_id: str | None = None
        self.replanning: set[str] = set()
        self.local_model = LocalQwenService(
            root, model=getattr(self.settings, "qwen_model", "qwen2.5:3b"),
            base_url=getattr(self.settings, "qwen_base_url", "http://127.0.0.1:11434"),
            models_dir=getattr(self.settings, "ollama_models", None) or None)

    async def get_engine(self, refresh: bool = False):
        if self.engine is not None and refresh:
            settings = load_settings(self.root)
            if (settings.browser == self.settings.browser
                and getattr(settings, "input_mode", "browser_mouse") == getattr(self.settings, "input_mode", "browser_mouse")):
                # Keep the browser connection and COM apartment on
                # their original worker for the entire app session.
                if self.client is not None:
                    await self.client.close()
                self.settings = settings
                self.client = DeepSeekClient(settings)
                self.engine.llm = self.client
                self.engine.settings = settings
                self.engine.judge = self.local_model
                self.desktop.max_elements = min(500, settings.max_elements)
                self.desktop.action_delay = settings.action_delay
                return self.engine
        if self.engine is None or refresh:
            if self.engine is not None:
                await self.engine.close()
            if self.client is not None:
                await self.client.close()
            from .engine import ShoppingEngine
            self.settings = load_settings(self.root)
            self.desktop = desktop_class(self.settings)(
                self.root, browser=self.settings.browser,
                max_elements=self.settings.max_elements, action_delay=self.settings.action_delay,
            )
            self.client = DeepSeekClient(self.settings)
            self.engine = ShoppingEngine(self.store, self.client, self.desktop, self.settings,
                                        judge=self.local_model)
        return self.engine

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def browser_connecting(self) -> bool:
        return self.browser_open_task is not None and not self.browser_open_task.done()

    async def close(self):
        if self.engine:
            await self.engine.close()
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.client:
            await self.client.close()
        await self.local_model.stop()


def create_app(root: Path = ROOT) -> FastAPI:
    runtime = Runtime(root)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if runtime.local_model.status().get("enabled"):
            runtime.spawn(runtime.local_model.ensure_running())
        async def choice_deadline_watcher():
            while True:
                try:
                    for record in runtime.store.history():
                        if record.get("status") != "awaiting_choice" or not record.get("choice_deadline"):
                            continue
                        try:
                            deadline = datetime.fromisoformat(str(record["choice_deadline"]).replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if deadline > datetime.now(timezone.utc):
                            continue
                        engine = await runtime.get_engine()
                        await engine.auto_approve(record["id"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A transient browser/model error must not kill the watcher;
                    # the next poll can retry while the choice remains pending.
                    pass
                await asyncio.sleep(0.5)
        runtime.spawn(choice_deadline_watcher())
        yield
        await runtime.close()

    app = FastAPI(title="Scepter Shopper", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.runtime = runtime

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # A local desktop controller must not accept a cross-site browser POST.
        host = request.url.hostname or ""
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"detail": "仅支持本机访问"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            allowed = {f"http://127.0.0.1:{runtime.settings.port}", f"http://localhost:{runtime.settings.port}",
                       "http://127.0.0.1:5173", "http://localhost:5173"}
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin not in allowed):
                return JSONResponse({"detail": "拒绝跨站操作请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/") or request.url.path in {"/", "/index.html"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "任务不存在"}, status_code=404)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "app": "scepter-shopper", "version": "0.1.0",
                "runtime_build": runtime.runtime_build, "host_build": runtime.host_build,
                "input_mode": getattr(runtime.settings, "input_mode", "browser_mouse"), "pid": os.getpid()}

    @app.get("/api/status")
    async def status():
        settings = load_settings(root)
        try:
            desktop = runtime.desktop or desktop_class(settings)(root, browser=settings.browser)
            info = desktop.health()
            info["available"] = info.get("status") == "ok"
            info["detail"] = info.get("message", "")
        except Exception as exc:
            info = {"available": False, "detail": safe_error(exc, settings)}
        frontend_entry = None
        try:
            html = (root / "frontend" / "dist" / "index.html").read_text(encoding="utf-8")
            entry = re.search(r'<script\b[^>]*\bsrc=[\"\'](/assets/[^\"\'<>\s]+\.js)[\"\']', html, re.I)
            if entry:
                frontend_entry = entry.group(1)
        except OSError:
            pass
        config = settings.public()
        # Connecting Edge must not probe or restart Ollama; reuse the last snapshot.
        config["local_model"] = runtime.local_model.status(cached=runtime.browser_connecting())
        return {"config": config, "desktop": info, "active_run": runtime.store.active(),
                "runtime_build": runtime.runtime_build, "frontend_entry": frontend_entry}

    @app.get("/api/settings")
    async def settings():
        config = load_settings(root).public()
        config["local_model"] = runtime.local_model.status()
        return config

    @app.get("/api/local-model")
    async def local_model_status():
        return runtime.local_model.status()

    @app.post("/api/local-model/enable")
    async def enable_local_model():
        status = runtime.local_model.status()
        if status.get("state") == "missing_runtime":
            raise HTTPException(400, status.get("message") or "未检测到 Ollama。")
        if status.get("ready") or status.get("state") in {"downloading", "starting"}:
            return status
        runtime.spawn(runtime.local_model.enable())
        return runtime.local_model.status()

    @app.get("/api/history")
    async def history():
        return runtime.store.history()

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        return runtime.store.get(run_id)

    @app.post("/api/runs")
    async def start(body: StartRequest):
        async with runtime.lock:
            if runtime.browser_connecting():
                raise HTTPException(409, "正在连接 Edge，请完成本次连接后再开始任务。")
            if runtime.replanning:
                raise HTTPException(409, "正在细化型号方案，请完成后再开始任务。")
            if runtime.store.active():
                raise HTTPException(409, "已有任务正在执行或等待处理，请先完成或停止它。")
            if runtime.engine is not None and getattr(runtime.engine, "_active_id", None):
                raise HTTPException(409, "上一个任务正在停止并收尾，请稍后再开始新任务。")
            settings = load_settings(root)
            if not settings.key_configured:
                raise HTTPException(400, "请在项目 .env 中填写 DEEPSEEK_API_KEY，保存后再开始。")
            engine = await runtime.get_engine(refresh=True)
            record = runtime.store.create(body.prompt.strip())

            async def guarded_start():
                try:
                    await engine.start(record["id"])
                except asyncio.CancelledError:
                    if runtime.store.get(record["id"])["status"] not in TERMINAL:
                        runtime.store.update(record["id"], status="stopped", phase="已停止")
                    raise
                except Exception as exc:
                    detail = safe_error(exc, settings)
                    runtime.store.event(record["id"], detail, "error")
                    runtime.store.update(record["id"], status="failed", error=detail, phase="执行中断")

            runtime.spawn(guarded_start())
            return record

    @app.post("/api/runs/{run_id}/control")
    async def control(run_id: str, body: ControlRequest):
        async with runtime.lock:
            if runtime.browser_connecting() and body.action in {"resume", "approve"}:
                raise HTTPException(409, "正在连接 Edge，请完成本次连接后再继续执行。")
            if run_id in runtime.replanning:
                raise HTTPException(409, "正在细化型号方案，请稍后再操作。")
            record = runtime.store.get(run_id)
            if record["status"] in TERMINAL:
                raise HTTPException(409, "此任务已结束，请新建任务。")
            engine = await runtime.get_engine()
            try:
                return await engine.control(run_id, body.action, body.selection)
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(409, safe_error(exc, runtime.settings)) from None

    @app.post("/api/runs/{run_id}/replan", status_code=202)
    async def replan(run_id: str):
        """Refresh model suggestions only; never resume browser/cart actions."""
        async with runtime.lock:
            if runtime.browser_connecting():
                raise HTTPException(409, "正在连接 Edge，请完成本次连接后再细化方案。")
            record = runtime.store.get(run_id)
            active = runtime.store.active()
            if runtime.replanning or (active and active["id"] != run_id):
                raise HTTPException(409, "已有任务正在处理，请先完成或停止。")
            if record["status"] not in {"paused", "needs_user", "awaiting_choice", "awaiting_approval", "stopped"}:
                raise HTTPException(409, "请先暂停任务，再细化型号方案。")
            if record.get("approved_ids") or record.get("approved_at") or record.get("add_journal") or record.get("stage") == "cart":
                raise HTTPException(409, "已确认加购的方案不能重整，请新建任务。")
            if runtime.engine is not None and getattr(runtime.engine, "_active_id", None):
                raise HTTPException(409, "执行器正在收尾，请稍后重试。")
            settings = load_settings(root)
            if not settings.key_configured:
                raise HTTPException(400, "请先在 .env 中填写 DEEPSEEK_API_KEY。")
            engine = await runtime.get_engine(refresh=True)
            runtime.replanning.add(run_id)

            async def guarded_replan():
                try:
                    await engine.regenerate_plan(run_id)
                except Exception as exc:
                    detail = safe_error(exc, settings)
                    runtime.store.event(run_id, detail, "error")
                    runtime.store.update(run_id, planning_only=False, error=detail)
                finally:
                    runtime.replanning.discard(run_id)

            runtime.spawn(guarded_replan())
            return {"run_id": run_id, "status": "replanning"}

    @app.post("/api/runs/{run_id}/validate")
    async def validate(run_id: str, body: ValidateRequest):
        async with runtime.lock:
            record = runtime.store.get(run_id)
            if record["status"] not in {"awaiting_choice", "awaiting_approval"}:
                raise HTTPException(409, "只能在确认配置阶段修改选品。")
            engine = await runtime.get_engine()
            try:
                return engine.validate_selection(run_id, body.selection)
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(409, safe_error(exc, runtime.settings)) from None

    @app.post("/api/browser/open")
    async def open_browser(body: BrowserOpenRequest | None = None):
        page_id = body.page_id if body else None
        task = runtime.browser_open_task
        if task is not None and not task.done():
            if page_id != runtime.browser_open_page_id:
                raise HTTPException(409, "正在连接 Edge，请等待本次连接完成，再选择其他标签页。")
        else:
            async def connect_once():
                async with runtime.lock:
                    active = runtime.store.active()
                    if active and active["status"] not in {"paused", "needs_user", "awaiting_approval"}:
                        raise HTTPException(409, "任务执行中，请先暂停，再连接或选择京东标签页。")
                    engine = await runtime.get_engine()
                # Do not hold the mutation lock while waiting for human approval:
                # task stop/pause must remain responsive. Operations that replace
                # this engine or resume shopping reject while the attempt is live.
                result = await engine.call_desktop("open_jd", page_id=page_id) if page_id is not None else await engine.call_desktop("open_jd")
                if result.get("status") == "error" and result.get("error") != "browser_tab_selection_required":
                    raise HTTPException(409, result.get("message") or result.get("error") or "连接当前 Edge 失败；不会自动另开浏览器。")
                return result

            task = asyncio.create_task(connect_once())
            runtime.browser_open_task = task
            runtime.browser_open_page_id = page_id
            runtime.tasks.add(task)
            def finished(done):
                runtime.tasks.discard(done)
                if not done.cancelled():
                    done.exception()  # Observe errors even if the HTTP caller left.
            task.add_done_callback(finished)
        # A refreshed/disconnected UI must not invalidate the permission prompt.
        # Concurrent callers await this same attempt instead of queuing another.
        return await asyncio.shield(task)

    dist = root / "frontend" / "dist"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    @app.get("/index.html")
    async def index():
        if (dist / "index.html").exists():
            return FileResponse(dist / "index.html")
        return JSONResponse({"message": "前端尚未构建，请运行 安装依赖.cmd。"}, status_code=503)

    return app
