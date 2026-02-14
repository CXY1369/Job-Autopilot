"""
通用启发式规则：登录/验证码检测等。
"""

from __future__ import annotations


def detect_manual_required(visible_text: str) -> bool:
    """检测登录/验证码等需要人工介入的场景。"""
    text = (visible_text or "").lower()
    if not text:
        return False
    captcha_keywords = [
        "captcha",
        "recaptcha",
        "verify you are human",
        "i am not a robot",
    ]
    if any(k in text for k in captcha_keywords):
        return True
    login_keywords = ["sign in", "log in", "login"]
    verify_keywords = ["password", "verification code", "two-factor", "2fa", "verify"]
    if any(k in text for k in login_keywords) and any(k in text for k in verify_keywords):
        return True
    return False
