"""
LLM 状态解析模块（V2 拆分第四步）

职责：
- 将 LLM JSON payload 解析为结构化状态字段
- 在 Assist 未验证时做 Simplify 相关文案去幻觉清洗
"""

from __future__ import annotations

from typing import Callable


def parse_agent_response_payload(
    data: dict,
    *,
    simplify_state: str,
    assist_prefill_verified: bool,
    assist_prefill_delta: int,
    sanitize_claims: Callable[[str | None], str | None],
) -> dict:
    status = data.get("status", "continue")
    summary = data.get("summary", "")
    page_overview = data.get("page_overview")
    field_audit = data.get("field_audit")
    action_plan = data.get("action_plan")
    risk_or_blocker = data.get("risk_or_blocker")

    if not isinstance(page_overview, str):
        page_overview = None
    if not isinstance(field_audit, str):
        field_audit = None
    if not isinstance(action_plan, list):
        action_plan = None
    else:
        action_plan = [str(x) for x in action_plan[:8]]
    if not isinstance(risk_or_blocker, str):
        risk_or_blocker = None

    if simplify_state.lower() in ("unavailable", "unknown") or (
        not assist_prefill_verified and assist_prefill_delta <= 0
    ):
        summary = sanitize_claims(summary)
        page_overview = sanitize_claims(page_overview)
        field_audit = sanitize_claims(field_audit)
        risk_or_blocker = sanitize_claims(risk_or_blocker)
        if action_plan:
            action_plan = [sanitize_claims(x) or "" for x in action_plan]

    next_action = None
    if status == "continue" and data.get("next_action"):
        act = data["next_action"]
        target_question = act.get("target_question")
        if target_question is not None and not isinstance(target_question, str):
            target_question = str(target_question)
        next_action = {
            "action": act.get("action", ""),
            "ref": act.get("ref"),
            "selector": act.get("selector"),
            "value": act.get("value"),
            "target_question": target_question,
            "element_type": act.get("element_type"),
            "reason": act.get("reason"),
        }

    return {
        "status": status,
        "summary": summary,
        "page_overview": page_overview,
        "field_audit": field_audit,
        "action_plan": action_plan,
        "risk_or_blocker": risk_or_blocker,
        "next_action": next_action,
    }
