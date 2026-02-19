"""
LLM 调用运行时（V2 拆分）

职责：
- 统一处理模型回退链路
- 分类常见错误（限流/能力不匹配/其他）
- 返回结构化结果供调用方决定后续状态
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class LLMCallResult:
    ok: bool
    raw: str = ""
    model: str = ""
    model_index: int = 0
    error_summary: str | None = None
    error_code: str | None = None


def run_chat_with_fallback(
    *,
    client,
    fallback_models: list[str],
    start_model_index: int,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    top_p: float = 0.8,
    on_log: Callable[[str, str], None] | None = None,
    sleep_seconds: float = 1.0,
) -> LLMCallResult:
    """
    在候选模型列表上执行回退调用。
    - 限流或能力不匹配：尝试切换到下一模型
    - 其他错误：立即失败返回
    """
    model_index = max(0, int(start_model_index))
    if model_index >= len(fallback_models):
        model_index = 0

    def _log(level: str, message: str) -> None:
        if on_log:
            on_log(level, message)

    while model_index < len(fallback_models):
        model = fallback_models[model_index]
        try:
            completion = client.chat.completions.create(
                model=model,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                messages=messages,
            )
            raw = completion.choices[0].message.content or ""
            return LLMCallResult(
                ok=True,
                raw=raw,
                model=model,
                model_index=model_index,
            )
        except Exception as exc:
            error_str = str(exc)
            error_lower = error_str.lower()
            is_rate_limit = "429" in error_str or "rate_limit" in error_lower
            is_capability_mismatch = any(
                kw in error_lower
                for kw in (
                    "does not support",
                    "unsupported",
                    "multimodal",
                    "vision",
                    "image_url",
                    "invalid model",
                    "model_not_found",
                    "not found",
                )
            )

            if is_rate_limit:
                _log("warn", f"⚠️ 模型 {model} 遇到速率限制")
                model_index += 1
                if model_index < len(fallback_models):
                    _log("info", f"🔄 切换到模型: {fallback_models[model_index]}")
                    time.sleep(max(0.0, sleep_seconds))
                    continue
                return LLMCallResult(
                    ok=False,
                    model=model,
                    model_index=model_index,
                    error_summary="所有模型都遇到速率限制",
                    error_code="rate_limit_exhausted",
                )

            if is_capability_mismatch:
                _log("warn", f"⚠️ 模型 {model} 能力不匹配或不可用，尝试回退")
                model_index += 1
                if model_index < len(fallback_models):
                    _log("info", f"🔄 切换到模型: {fallback_models[model_index]}")
                    time.sleep(max(0.0, sleep_seconds))
                    continue
                return LLMCallResult(
                    ok=False,
                    model=model,
                    model_index=model_index,
                    error_summary="所有候选模型都不支持当前请求",
                    error_code="model_unsupported_exhausted",
                )

            return LLMCallResult(
                ok=False,
                model=model,
                model_index=model_index,
                error_summary=f"LLM 调用失败: {exc}",
                error_code="llm_call_failed",
            )

    return LLMCallResult(
        ok=False,
        model=fallback_models[-1] if fallback_models else "",
        model_index=max(0, len(fallback_models) - 1),
        error_summary="LLM 未返回结果",
        error_code="llm_no_result",
    )
