from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    host: str = "127.0.0.1"
    port: int = 8766
    browser: str = "edge"
    input_mode: str = "existing_browser"
    max_steps: int = 400
    max_run_seconds: int = 3600
    max_no_progress: int = 5
    choice_timeout_seconds: int = 10
    choice_policy: str = "recommend_only"
    candidates_per_category: int = 3
    screenshot_evidence: bool = False
    max_model_calls: int = 240
    max_input_tokens: int = 2000000
    max_elements: int = 180
    action_delay: float = 0.6
    api_timeout: int = 90
    max_output_tokens: int = 4096
    qwen_model: str = "qwen2.5:3b"
    qwen_base_url: str = "http://127.0.0.1:11434"
    ollama_models: str = ""
    max_local_calls: int = 80
    max_local_input_chars: int = 8000

    @property
    def key_configured(self) -> bool:
        return bool(self.api_key.strip()) and self.api_key.strip() not in {"your_api_key", "填入你的Key", "sk-"}

    def public(self) -> dict:
        return {
            "model": self.model,
            "model_label": "DeepSeek 4.1 Flash" if self.model == "deepseek-flash" else self.model,
            "base_url": self.base_url,
            "key_configured": self.key_configured,
            "observation_mode": "页面结构与控件文字，默认不截图",
            "browser": self.browser,
            "input_mode": self.input_mode,
            "input_mode_label": {"existing_browser": "现有 Edge 标签页虚拟鼠标", "browser_mouse": "专用浏览器虚拟鼠标", "native": "Windows 系统鼠标"}[self.input_mode],
            "max_steps": self.max_steps,
            "choice_timeout_seconds": self.choice_timeout_seconds,
            "choice_policy": self.choice_policy,
            "max_input_tokens": self.max_input_tokens,
            "candidates_per_category": self.candidates_per_category,
            "screenshot_evidence": self.screenshot_evidence,
            "qwen_model": self.qwen_model,
            "ollama_models": self.ollama_models,
            "max_local_calls": self.max_local_calls,
        }


def load_settings(root: Path = ROOT) -> Settings:
    # Re-read .env on each task so filling in the key does not require a restart.
    values = {**dotenv_values(root / ".env", encoding="utf-8-sig"), **os.environ}

    def value(name: str, default: str) -> str:
        return str(values.get(name) or default).strip()

    def integer(name: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(value(name, str(default)))))
        except ValueError:
            return default

    base_url = value("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if not base_url.startswith("https://"):
        raise ValueError("DEEPSEEK_BASE_URL 必须是 HTTPS 地址。")
    try:
        delay = max(0.2, min(3.0, float(value("ACTION_DELAY", "0.6"))))
    except ValueError:
        delay = 0.6
    input_mode = value("INPUT_MODE", "existing_browser").lower()
    if input_mode not in {"existing_browser", "browser_mouse", "native"}:
        raise ValueError("INPUT_MODE 只支持 existing_browser、browser_mouse 或 native。")
    choice_policy = value("CHOICE_POLICY", "recommend_only").lower()
    if choice_policy not in {"recommend_only", "low_risk_auto"}:
        raise ValueError("CHOICE_POLICY 只支持 recommend_only 或 low_risk_auto。")
    qwen_base = value("QWEN_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    if not (qwen_base.startswith("http://127.0.0.1") or qwen_base.startswith("http://localhost")):
        raise ValueError("QWEN_BASE_URL 只允许本机 Ollama 地址。")
    return Settings(
        root=root,
        api_key=value("DEEPSEEK_API_KEY", ""),
        base_url=base_url,
        model=value("DEEPSEEK_MODEL", "deepseek-flash"),
        port=integer("APP_PORT", 8766, 1024, 65535),
        browser=value("BROWSER", "edge"),
        input_mode=input_mode,
        max_steps=integer("MAX_STEPS", 400, 10, 1000),
        max_run_seconds=integer("MAX_RUN_SECONDS", 3600, 60, 14400),
        max_no_progress=integer("MAX_NO_PROGRESS", 5, 2, 15),
        choice_timeout_seconds=integer("CHOICE_TIMEOUT_SECONDS", 10, 5, 60),
        choice_policy=choice_policy,
        candidates_per_category=integer("CANDIDATES_PER_CATEGORY", 3, 1, 5),
        screenshot_evidence=value("SCREENSHOT_EVIDENCE", "false").lower() == "true",
        max_model_calls=integer("MAX_MODEL_CALLS", 240, 5, 500),
        max_input_tokens=integer("MAX_INPUT_TOKENS", 2000000, 10000, 50000000),
        max_elements=integer("MAX_ELEMENTS", 180, 50, 500),
        action_delay=delay,
        api_timeout=integer("API_TIMEOUT", 90, 10, 300),
        max_output_tokens=integer("MAX_OUTPUT_TOKENS", 4096, 1024, 16384),
        qwen_model=value("QWEN_MODEL", "qwen2.5:3b"),
        qwen_base_url=qwen_base,
        ollama_models=value("OLLAMA_MODELS", str(root / "data" / "ollama-models")),
        max_local_calls=integer("MAX_LOCAL_CALLS", 80, 5, 400),
        max_local_input_chars=integer("MAX_LOCAL_INPUT_CHARS", 8000, 1000, 40000),
    )
