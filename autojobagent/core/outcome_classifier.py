"""
提交结果分类模块（V2 拆分第二步）

职责：
- 提交结果分类（success/validation/external/transient/unknown）
- 成功文案识别
- 结构化 manual_reason 组装
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class SubmissionOutcome:
    classification: Literal[
        "success_confirmed",
        "validation_error",
        "external_blocked",
        "transient_network",
        "unknown_blocked",
    ]
    reason_code: str
    evidence_snippet: str


@dataclass
class CompletionAssessment:
    confirmed: bool
    score: float
    reason: str
    signals: dict[str, bool | float]


def looks_like_completion_text(lower_text: str) -> bool:
    success_indicators = [
        "thank you for applying",
        "thanks for your application",
        "application submitted",
        "application received",
        "successfully submitted",
        "your application has been submitted",
        "application complete",
        "thanks for submitting",
    ]
    return any(token in lower_text for token in success_indicators)


def assess_completion_confidence(
    *,
    body_text: str,
    current_url: str,
    has_submit_button: bool,
    has_error: bool,
) -> CompletionAssessment:
    lower = (body_text or "").lower()
    url_lower = (current_url or "").lower()

    success_text = looks_like_completion_text(lower) or any(
        token in lower
        for token in (
            "we'll be in touch",
            "we will review your application",
            "application complete",
        )
    )
    external_blocked = any(
        token in lower
        for token in (
            "flagged as possible spam",
            "couldn't submit your application",
            "suspicious activity",
            "try again",
            "rate limit",
        )
    )
    url_success_hint = any(
        token in url_lower
        for token in (
            "/thanks",
            "/thank-you",
            "/success",
            "/submitted",
            "/complete",
            "/confirmation",
        )
    )

    score = 0.0
    if success_text:
        score += 0.68
    if not has_submit_button:
        score += 0.16
    else:
        score -= 0.30
    if has_error:
        score -= 0.48
    else:
        score += 0.10
    if url_success_hint:
        score += 0.12
    if external_blocked:
        score -= 0.60

    score = max(0.0, min(1.0, score))
    confirmed = score >= 0.72 and not has_error and not external_blocked
    reason = "high_confidence_success" if confirmed else "not_confident_enough"
    return CompletionAssessment(
        confirmed=confirmed,
        score=score,
        reason=reason,
        signals={
            "success_text": success_text,
            "submit_button_visible": has_submit_button,
            "has_error": has_error,
            "url_success_hint": url_success_hint,
            "external_blocked": external_blocked,
            "score": round(score, 3),
        },
    )


def classify_submission_outcome(
    *,
    evidence_text: str,
    action_success: bool,
    progression_block_reason: str | None,
    progression_block_snippets: list[str],
) -> SubmissionOutcome:
    lower = (evidence_text or "").lower()
    if looks_like_completion_text(lower):
        return SubmissionOutcome(
            classification="success_confirmed",
            reason_code="completion_detected",
            evidence_snippet=evidence_text[:220],
        )
    if any(
        k in lower
        for k in [
            "flagged as possible spam",
            "suspicious activity",
            "anti-spam",
            "risk",
            "rate limit",
            "too many requests",
            "try again later",
        ]
    ):
        return SubmissionOutcome(
            classification="external_blocked",
            reason_code="anti_spam_or_risk_blocked",
            evidence_snippet=evidence_text[:220],
        )
    if any(
        k in lower
        for k in [
            "network error",
            "temporarily unavailable",
            "timeout",
            "timed out",
            "connection error",
            "server error",
            "5xx",
        ]
    ):
        return SubmissionOutcome(
            classification="transient_network",
            reason_code="network_or_server_transient",
            evidence_snippet=evidence_text[:220],
        )
    if progression_block_reason:
        snippets = " | ".join((progression_block_snippets or [])[:2])
        snippet = snippets or progression_block_reason
        return SubmissionOutcome(
            classification="validation_error",
            reason_code="missing_required_field",
            evidence_snippet=snippet[:220],
        )
    if action_success:
        return SubmissionOutcome(
            classification="unknown_blocked",
            reason_code="submit_clicked_without_confirmed_transition",
            evidence_snippet=evidence_text[:220],
        )
    return SubmissionOutcome(
        classification="unknown_blocked",
        reason_code="submit_action_failed",
        evidence_snippet=evidence_text[:220],
    )


def build_submission_manual_reason(
    outcome: SubmissionOutcome | None,
    *,
    action_name: str,
    action_target: str,
) -> str:
    if not outcome:
        return "提交连续失败达到重试上限，需要人工处理"
    return (
        "提交连续受阻达到重试上限；"
        f"classification={outcome.classification}; "
        f"code={outcome.reason_code}; "
        f"action={action_name}:{action_target}; "
        f"evidence={outcome.evidence_snippet[:160]}"
    )
