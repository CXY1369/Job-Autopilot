"""
通用启发式规则：登录/验证码检测等。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ManualRequiredAssessment:
    """页面是否需要人工介入的结构化判定结果。"""

    manual_required: bool
    reason: str
    confidence: float
    evidence: dict[str, int | bool]


def detect_manual_required(visible_text: str) -> bool:
    """兼容旧接口：返回是否需要人工介入。"""
    return assess_manual_required(visible_text).manual_required


def assess_manual_required(
    visible_text: str,
    *,
    password_input_count: int = 0,
    captcha_element_count: int = 0,
    has_login_button: bool = False,
    has_apply_cta: bool = False,
) -> ManualRequiredAssessment:
    """检测登录/验证码等需要人工介入的场景（证据化判定）。"""
    text = (visible_text or "").lower()
    evidence: dict[str, int | bool] = {
        "password_input_count": max(password_input_count, 0),
        "captcha_element_count": max(captcha_element_count, 0),
        "has_login_button": bool(has_login_button),
        "has_apply_cta": bool(has_apply_cta),
    }
    if not text and captcha_element_count <= 0 and password_input_count <= 0:
        return ManualRequiredAssessment(
            manual_required=False,
            reason="no_text_no_dom_signal",
            confidence=0.0,
            evidence=evidence,
        )

    captcha_keywords = [
        "captcha",
        "recaptcha",
        "verify you are human",
        "i am not a robot",
    ]
    has_captcha_text = any(k in text for k in captcha_keywords)
    if captcha_element_count > 0 or has_captcha_text:
        return ManualRequiredAssessment(
            manual_required=True,
            reason="captcha_detected",
            confidence=0.98 if captcha_element_count > 0 else 0.9,
            evidence=evidence,
        )

    login_keywords = [
        "sign in",
        "log in",
        "login",
        "sign-in",
        "authentication required",
    ]
    password_keywords = [
        "password",
        "verification code",
        "two-factor",
        "2fa",
        "one-time code",
        "otp",
    ]
    has_login_text = any(k in text for k in login_keywords)
    has_password_text = any(k in text for k in password_keywords)

    # 强信号：真实登录表单（密码框 + 登录语义）
    if password_input_count > 0 and (has_login_text or has_login_button):
        return ManualRequiredAssessment(
            manual_required=True,
            reason="login_form_detected",
            confidence=0.95,
            evidence=evidence,
        )

    # 详情页常见文案误报保护：存在 Apply 入口且无密码框时不应直接判定为登录页
    if has_apply_cta and password_input_count == 0 and has_login_text:
        return ManualRequiredAssessment(
            manual_required=False,
            reason="apply_entry_page_not_login",
            confidence=0.8,
            evidence=evidence,
        )

    # 纯文本弱信号，降低误判强度
    if has_login_text and has_password_text and not has_apply_cta:
        return ManualRequiredAssessment(
            manual_required=True,
            reason="login_text_signal",
            confidence=0.7,
            evidence=evidence,
        )

    return ManualRequiredAssessment(
        manual_required=False,
        reason="no_manual_required_signal",
        confidence=0.2,
        evidence=evidence,
    )
