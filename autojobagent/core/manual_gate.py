"""
人工介入门控辅助模块（V2 拆分）

职责：
- 收集登录/验证码门控证据
- 页面状态轻量分类
"""

from __future__ import annotations

from .ui_snapshot import SnapshotItem


def safe_locator_count(page, selector: str) -> int:
    try:
        return page.locator(selector).count()
    except Exception:
        return 0


def collect_selector_details(page, selectors: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for selector in selectors:
        try:
            details = page.evaluate(
                """
                (sel) => {
                  const nodes = Array.from(document.querySelectorAll(sel));
                  const isVisible = (el) => {
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    if (st.display === "none" || st.visibility === "hidden") return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };
                  const samples = nodes.slice(0, 3).map((el) => ({
                    tag: (el.tagName || "").toLowerCase(),
                    id: el.id || "",
                    className: String(el.className || "").slice(0, 80),
                    text: String(el.textContent || "").trim().slice(0, 120),
                    visible: isVisible(el),
                    rect: (() => {
                      const r = el.getBoundingClientRect();
                      return { w: Math.round(r.width), h: Math.round(r.height) };
                    })()
                  }));
                  return {
                    total: nodes.length,
                    visible: samples.filter((s) => s.visible).length,
                    samples,
                  };
                }
                """,
                selector,
            )
        except Exception as exc:
            details = {"error": str(exc)}
        out[selector] = details
    return out


def count_visible_captcha_challenge(page, selectors: list[str]) -> int:
    """只统计可见验证码挑战节点，排除 recaptcha 法律声明文本。"""
    total = 0
    for selector in selectors:
        try:
            count = page.evaluate(
                """
                (sel) => {
                  const nodes = Array.from(document.querySelectorAll(sel));
                  const isVisible = (el) => {
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    if (st.display === "none" || st.visibility === "hidden") return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };
                  const isLegalNotice = (el) => {
                    const cls = String(el.className || "").toLowerCase();
                    const text = String(el.textContent || "").toLowerCase();
                    return (
                      cls.includes("recaptchalegal") ||
                      (text.includes("protected by recaptcha") &&
                       text.includes("privacy policy") &&
                       text.includes("terms of service"))
                    );
                  };
                  return nodes.filter((el) => isVisible(el) && !isLegalNotice(el)).length;
                }
                """,
                selector,
            )
            total += int(count or 0)
        except Exception:
            continue
    return total


def collect_manual_required_evidence(
    *,
    page,
    visible_text: str,
    snapshot_map: dict[str, SnapshotItem],
    snapshot_intents: dict[str, set[str]],
    page_text_intents: set[str],
) -> tuple[dict[str, int | bool], dict]:
    """收集登录/验证码判定所需 DOM+文本证据。"""
    password_input_count = safe_locator_count(page, "input[type='password']")
    captcha_selectors = [
        "iframe[src*='recaptcha']",
        ".g-recaptcha",
        "iframe[src*='hcaptcha']",
        ".h-captcha",
        "[data-sitekey][data-callback]",
        "iframe[title*='captcha' i]",
    ]
    captcha_element_count = count_visible_captcha_challenge(page, captcha_selectors)
    lower_text = (visible_text or "").lower()
    captcha_challenge_phrases = [
        "i am not a robot",
        "verify you are human",
        "security check",
        "complete the challenge",
        "select all images",
        "are you human",
    ]
    has_captcha_challenge_text = any(p in lower_text for p in captcha_challenge_phrases)

    has_login_button = any(
        ref in snapshot_map
        and snapshot_map[ref].role in ("button", "link")
        and "login_action" in intents
        for ref, intents in snapshot_intents.items()
    )
    has_apply_cta = any(
        ref in snapshot_map
        and snapshot_map[ref].role in ("button", "link")
        and "apply_entry" in intents
        for ref, intents in snapshot_intents.items()
    )
    has_login_button = has_login_button or ("login_action" in page_text_intents)

    evidence = {
        "password_input_count": password_input_count,
        "captcha_element_count": captcha_element_count,
        "has_captcha_challenge_text": has_captcha_challenge_text,
        "has_login_button": has_login_button,
        "has_apply_cta": has_apply_cta,
    }
    details = {
        "captcha_selector_details": collect_selector_details(page, captcha_selectors),
        "page_text_intents": sorted(page_text_intents),
    }
    return evidence, details


def classify_page_state(
    *,
    snapshot_map: dict[str, SnapshotItem],
    evidence: dict[str, int | bool],
    manual_required: bool,
    current_url: str,
) -> tuple[str, dict]:
    """轻量页面状态分类：login/captcha、职位详情页、申请页。"""
    if manual_required:
        return "manual_gate", {}

    form_roles = {"textbox", "combobox", "checkbox", "radio", "file_input"}
    form_item_count = sum(
        1
        for item in snapshot_map.values()
        if item.role in form_roles and (item.in_form or item.required)
    )
    has_form_fields = form_item_count >= 2
    has_apply_cta = bool(evidence.get("has_apply_cta", False))
    current_url = (current_url or "").lower()
    looks_like_application_url = (
        "/application" in current_url
        or "/apply" in current_url
        or "greenhouse.io" in current_url
    )
    stats = {
        "form_item_count": form_item_count,
        "has_form_fields": has_form_fields,
        "has_apply_cta": has_apply_cta,
        "looks_like_application_url": looks_like_application_url,
        "url": current_url,
    }
    if looks_like_application_url:
        return "application_or_form_page", stats
    if has_apply_cta and not has_form_fields:
        return "job_detail_with_apply", stats
    return "application_or_form_page", stats


def select_apply_entry_candidate(
    *,
    snapshot_map: dict[str, SnapshotItem],
    snapshot_intents: dict[str, set[str]],
    current_url: str,
) -> SnapshotItem | None:
    """在职位详情页中定位进入申请流程的 Apply 候选。"""
    current_url = (current_url or "").lower()
    if "/application" in current_url or "/apply" in current_url:
        return None

    candidates: list[SnapshotItem] = []
    for ref, item in snapshot_map.items():
        if item.role not in ("button", "link"):
            continue
        intents = snapshot_intents.get(ref, set())
        if "apply_entry" not in intents:
            continue
        label = (item.name or "").lower()
        if any(
            bad in label
            for bad in [
                "replace",
                "upload",
                "autofill",
                "tailor",
                "settings",
                "profile",
                "close",
            ]
        ):
            continue
        candidates.append(item)
    if not candidates:
        return None

    candidates.sort(
        key=lambda it: (
            it.role != "button",
            len(it.name),
        )
    )
    return candidates[0]
