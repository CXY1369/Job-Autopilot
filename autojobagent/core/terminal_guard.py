"""
终态兜底判定（Phase A）

职责：
- 当 LLM 返回非 JSON 时，优先判断是否已经到达“提交成功”终态
"""

from __future__ import annotations


def raw_response_implies_completion(raw: str) -> bool:
    text = (raw or "").strip().lower()
    if not text:
        return False
    indicators = [
        "successfully submitted",
        "application was successfully submitted",
        "your application has been submitted",
        "thanks for your application",
        "thank you for applying",
        "process is complete",
        "application complete",
    ]
    return any(token in text for token in indicators)
