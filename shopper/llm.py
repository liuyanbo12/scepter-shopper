from __future__ import annotations

import re

from openai import AsyncOpenAI, APIConnectionError, APIStatusError, APITimeoutError

from .config import Settings


class ModelError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: AsyncOpenAI | None = None

    async def complete(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False) -> dict:
        if not self.settings.key_configured:
            raise ModelError("请在项目 .env 中填写 DEEPSEEK_API_KEY，再开始任务。")
        if self.client is None:
            self.client = AsyncOpenAI(
                api_key=self.settings.api_key, base_url=self.settings.base_url,
                timeout=self.settings.api_timeout, max_retries=1,
            )
        args = dict(
            model=self.settings.model, messages=messages,
            temperature=0.1, max_tokens=self.settings.max_output_tokens,
            extra_body={"thinking": {"type": "disabled"}},
        )
        if tools:
            args.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        if json_mode:
            args["response_format"] = {"type": "json_object"}
        try:
            result = await self.client.chat.completions.create(**args)
        except APIStatusError as exc:
            descriptions = {
                401: "API Key 无效或已失效，请修改 .env。",
                402: "DeepSeek 账户余额不足。",
                429: "DeepSeek 请求频率或额度受限，请稍后重试。",
                400: "DeepSeek 拒绝了模型或请求参数；请检查模型名称与接口配置。",
                404: "DeepSeek 模型或接口不存在，请检查 .env 配置。",
            }
            raise ModelError(descriptions.get(exc.status_code, f"DeepSeek 服务返回 HTTP {exc.status_code}。")) from None
        except (APIConnectionError, APITimeoutError):
            raise ModelError("无法连接 DeepSeek 或请求超时，请检查网络后重试。") from None
        if not result.choices:
            raise ModelError("DeepSeek 返回了空结果。")
        choice = result.choices[0]
        if choice.finish_reason == "length":
            raise ModelError("模型输出被截断；请提高 MAX_OUTPUT_TOKENS 后重试。")
        message = choice.message.model_dump(exclude_none=True)
        # Only preserve fields accepted back as an assistant message.
        message = {k: v for k, v in message.items() if k in {"role", "content", "tool_calls", "reasoning_content"}}
        usage = result.usage
        counts = {
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
        }
        if usage:
            for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
                value = getattr(usage, key, None)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    counts[key] = value
        return {"message": message, "usage": counts}

    async def close(self):
        if self.client is not None:
            await self.client.close()


def safe_error(error: Exception, settings: Settings | None = None) -> str:
    text = str(error)
    if settings and settings.api_key:
        text = text.replace(settings.api_key, "[已隐藏]")
    return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[已隐藏]", text)[:1000]
