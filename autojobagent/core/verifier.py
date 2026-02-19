"""
动作后验验证模块（V2 拆分第一步）

职责：
- 通用输入读取/状态判断工具
- ref 动作后验校验主逻辑
"""

from __future__ import annotations

from typing import Any, Callable

from .ui_snapshot import SnapshotItem


def get_input_value(locator) -> str:
    """尽力获取输入框当前值。"""
    try:
        return locator.input_value(timeout=500)
    except Exception:
        try:
            return locator.evaluate("(el) => el.value || el.textContent || ''")
        except Exception:
            return ""


def is_dropdown_open(locator) -> bool:
    """检测 autocomplete 下拉是否打开（aria-expanded）。"""
    try:
        expanded = locator.get_attribute("aria-expanded")
        return str(expanded).lower() == "true"
    except Exception:
        return False


def normalize_answer_label(text: str | None) -> str:
    normalized = (text or "").strip().lower()
    if normalized in ("yes", "y"):
        return "yes"
    if normalized in ("no", "n"):
        return "no"
    return ""


def verify_ref_action_effect(
    action: Any,
    locator,
    item: SnapshotItem,
    *,
    is_answer_click_action: Callable[[Any, SnapshotItem | None], bool],
    verify_question_answer_state: Callable[[str, str], bool],
) -> bool:
    """对 ref 动作进行基础后验校验。"""
    try:
        if action.action == "click":
            if item.role in ("checkbox", "radio"):
                return locator.is_checked()
            if is_answer_click_action(action, item):
                if action.target_question:
                    expected = normalize_answer_label(action.selector or item.name)
                    return verify_question_answer_state(
                        action.target_question, expected
                    )
                # 对未绑定问题文本的回答型按钮，至少确认该按钮出现可见选中态
                try:
                    if item.role in ("checkbox", "radio"):
                        return locator.is_checked()
                except Exception:
                    pass
                try:
                    pressed = str(locator.get_attribute("aria-pressed") or "").lower()
                    if pressed in ("true", "1"):
                        return True
                except Exception:
                    pass
                try:
                    checked = str(locator.get_attribute("aria-checked") or "").lower()
                    if checked in ("true", "1"):
                        return True
                except Exception:
                    pass
                try:
                    class_name = str(locator.get_attribute("class") or "").lower()
                    if any(k in class_name for k in ("selected", "active", "checked")):
                        return True
                except Exception:
                    pass
                return False
            return True
        if action.action in ("fill", "type", "select"):
            if action.value is None:
                return True
            current = get_input_value(locator)
            target = str(action.value).strip()
            if target and target in (current or ""):
                return True
            if action.action == "type" and item.role in ("combobox", "textbox"):
                return is_dropdown_open(locator)
            return False
        if action.action == "upload":
            # upload 由上层 _verify_upload_success 统一确认
            return False
    except Exception:
        return False
    return True
