"""
动作执行模块（V2 拆分第三步）

职责：
- 选择器路径动作执行细节（click/fill/type/select/upload helper/scroll）
- 保持与 Agent 解耦，便于后续替换底层执行器
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable


def smart_click(
    page,
    selector: str,
    *,
    element_type: str | None = None,
    log_fn: Callable[[str, str], None] | None = None,
) -> bool:
    if not selector:
        return False

    timeout = 1000
    check_timeout = 200

    words = selector.split()
    seen = set()
    unique_words = []
    for w in words:
        if w.lower() not in seen:
            seen.add(w.lower())
            unique_words.append(w)
    clean_selector = " ".join(unique_words)

    short_selector = clean_selector
    if " " in clean_selector and len(clean_selector) > 20:
        if unique_words and unique_words[-1] in ["Yes", "No", "yes", "no"]:
            short_selector = unique_words[-1]
    first_word = unique_words[0] if unique_words else clean_selector

    if element_type == "button":
        strategies = [
            lambda: page.get_by_role("button", name=clean_selector).first,
            lambda: page.get_by_text(clean_selector, exact=False).first,
        ]
    elif element_type == "link":
        strategies = [
            lambda: page.get_by_role("link", name=clean_selector).first,
            lambda: page.get_by_text(clean_selector, exact=False).first,
        ]
    elif element_type in ("checkbox", "radio"):
        strategies = [
            lambda: page.get_by_role("button", name=short_selector).first,
            lambda: page.get_by_text(short_selector, exact=True).first,
            lambda: page.get_by_role(element_type, name=short_selector).first,
            lambda: page.get_by_label(short_selector).first,
            lambda: page.get_by_text(clean_selector, exact=True).first,
            lambda: page.get_by_text(clean_selector, exact=False).first,
            lambda: page.get_by_text(first_word, exact=False).first,
            lambda: page.get_by_label(first_word, exact=False).first,
            lambda: page.locator(f"label:has-text('{first_word}')").first,
            lambda: page.locator(f"[data-testid*='{first_word}' i]").first,
        ]
    elif element_type == "option":
        strategies = [
            lambda: page.get_by_role("option", name=clean_selector).first,
            lambda: page.get_by_text(clean_selector, exact=True).first,
            lambda: page.get_by_text(clean_selector, exact=False).first,
            lambda: page.locator(f"li:has-text('{clean_selector}')").first,
            lambda: page.get_by_role("option", name=first_word).first,
            lambda: page.get_by_text(first_word, exact=False).first,
            lambda: page.locator(f"li:has-text('{first_word}')").first,
        ]
    else:
        strategies = [
            lambda: page.get_by_text(clean_selector, exact=True).first,
            lambda: page.get_by_role("button", name=clean_selector).first,
            lambda: page.get_by_text(clean_selector, exact=False).first,
            lambda: page.get_by_text(first_word, exact=False).first,
        ]

    max_scroll_attempts = 2
    for scroll_attempt in range(max_scroll_attempts + 1):
        for strategy in strategies:
            try:
                locator = strategy()
                if locator and locator.is_visible(timeout=check_timeout):
                    locator.click(timeout=timeout)
                    return True
            except Exception:
                continue

        if scroll_attempt < max_scroll_attempts:
            try:
                page.evaluate("window.scrollBy(0, 300)")
                page.wait_for_timeout(300)
                if log_fn:
                    log_fn(
                        f"   🔄 滚动页面，重试定位 ({scroll_attempt + 1}/{max_scroll_attempts})",
                        "info",
                    )
            except Exception:
                break

    return False


def smart_fill(page, selector: str, value: str) -> bool:
    if not selector or value is None:
        return False

    timeout = 1500
    value_str = str(value)
    clean_selector = selector.replace("*", "").strip()
    strategies = [
        lambda: page.get_by_label(selector, exact=False).first,
        lambda: page.get_by_label(clean_selector, exact=False).first,
        lambda: page.get_by_role("textbox", name=selector).first,
        lambda: page.get_by_role("textbox", name=clean_selector).first,
        lambda: (
            page.locator(f"label:has-text('{clean_selector}')")
            .locator("..")
            .locator("input")
            .first
        ),
    ]

    for strategy in strategies:
        try:
            locator = strategy()
            if locator.is_visible(timeout=200):
                locator.fill(value_str, timeout=timeout)
                return True
        except Exception:
            continue
    return False


def smart_type(
    page,
    selector: str,
    value: str,
    *,
    log_fn: Callable[[str, str], None] | None = None,
) -> bool:
    if not selector or value is None:
        return False

    clean_selector = selector.replace("*", "").strip()
    input_elem = None
    strategies = [
        lambda: page.get_by_label(selector, exact=False).first,
        lambda: page.get_by_label(clean_selector, exact=False).first,
        lambda: page.get_by_role("combobox", name=selector).first,
        lambda: page.get_by_role("combobox", name=clean_selector).first,
        lambda: page.get_by_role("textbox", name=selector).first,
        lambda: page.get_by_role("textbox", name=clean_selector).first,
        lambda: (
            page.locator(f"label:has-text('{clean_selector}')")
            .locator("..")
            .locator("input, [role='combobox']")
            .first
        ),
        lambda: page.locator(f"[aria-label*='{clean_selector}' i]").first,
    ]
    for strategy in strategies:
        try:
            elem = strategy()
            if elem.is_visible(timeout=300):
                input_elem = elem
                if log_fn:
                    log_fn(f"   📍 定位成功: {selector}", "info")
                break
        except Exception:
            continue

    if not input_elem:
        if log_fn:
            log_fn(f"   ⚠️ 无法定位输入框: {selector}", "warn")
        return False

    try:
        input_elem.click(timeout=800)
        page.wait_for_timeout(100)
        input_elem.press("Control+a")
        page.wait_for_timeout(30)
        input_elem.press("Backspace")
        page.wait_for_timeout(50)
        input_elem.type(str(value), delay=40)
        page.wait_for_timeout(600)
        return True
    except Exception as e:
        if log_fn:
            log_fn(f"   ⚠️ 输入失败: {e}", "warn")
        return False


def do_select(page, selector: str, value: str) -> bool:
    if not selector or not value:
        return False
    try:
        select = page.get_by_label(selector).first
        if select.is_visible(timeout=500):
            select.select_option(label=value, timeout=2000)
            return True
    except Exception:
        pass
    try:
        option = page.get_by_role("option", name=value).first
        if option.is_visible(timeout=300):
            option.click(timeout=1500)
            return True
    except Exception:
        pass
    return False


def locate_file_input(
    page,
    selector: str | None,
    *,
    click_fn: Callable[[str, str | None], bool] | None = None,
):
    try:
        file_inputs = page.locator("input[type='file']")
        if file_inputs.count() > 0:
            return file_inputs.first
    except Exception:
        pass

    if selector:
        if click_fn:
            click_fn(selector, "button")
        page.wait_for_timeout(300)
        try:
            file_inputs = page.locator("input[type='file']")
            if file_inputs.count() > 0:
                return file_inputs.first
        except Exception:
            pass
    return None


def verify_upload_success(page, file_path: str) -> bool:
    filename = Path(file_path).name
    try:
        count = page.locator("input[type='file']").count()
    except Exception:
        count = 0

    for i in range(count):
        try:
            locator = page.locator("input[type='file']").nth(i)
            ok = locator.evaluate(
                "(el, expected) => (el.files && el.files.length > 0 && el.files[0].name === expected)",
                filename,
            )
            if ok:
                return True
        except Exception:
            continue

    try:
        body_text = page.inner_text("body")
        if filename in body_text:
            return True
    except Exception:
        pass
    return False


def do_scroll(page, direction: str) -> bool:
    try:
        if "down" in (direction or "").lower():
            page.evaluate("window.scrollBy(0, 500)")
        else:
            page.evaluate("window.scrollBy(0, -500)")
        return True
    except Exception:
        return False
