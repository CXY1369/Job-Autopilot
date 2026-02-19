"""
UI 快照：生成可交互元素列表，提供 ref 供 LLM 选择。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Tuple

from playwright.sync_api import Page


@dataclass
class SnapshotItem:
    ref: str
    role: str
    name: str
    nth: int
    input_type: str | None = None
    tag: str | None = None
    required: bool = False
    in_form: bool = False
    in_assist_panel: bool = False
    checked: bool | None = None
    value_hint: str = ""


ROLE_ORDER = [
    "button",
    "link",
    "checkbox",
    "radio",
    "combobox",
    "textbox",
    "option",
]


def build_ui_snapshot(
    page: Page,
    max_per_role: int = 30,
    max_total: int = 160,
) -> Tuple[str, Dict[str, SnapshotItem]]:
    """
    生成可交互元素快照（文本 + ref 映射）。
    """
    items: List[SnapshotItem] = []
    ref_map: Dict[str, SnapshotItem] = {}
    name_counters: Dict[tuple[str, str], int] = {}

    def _next_ref(idx: int) -> str:
        return f"e{idx + 1}"

    def _describe(el) -> dict:
        try:
            return el.evaluate(
                """
                (el) => {
                  const label = el.labels && el.labels.length ? el.labels[0].innerText : "";
                  const aria = el.getAttribute("aria-label") || "";
                  const placeholder = el.getAttribute("placeholder") || "";
                  const text = (el.innerText || "").trim();
                  const name = el.getAttribute("name") || "";
                  const type = el.getAttribute("type") || "";
                  const tag = (el.tagName || "").toLowerCase();
                  const required = !!(el.required || el.getAttribute("aria-required") === "true");
                  const inForm = !!el.closest("form");
                  const hasAssistKeyword = (v) => {
                    const t = String(v || "").toLowerCase();
                    return t.includes("simplify") || t.includes("autofill") || t.includes("copilot");
                  };
                  const inAssistPanel = (() => {
                    let cur = el;
                    for (let i = 0; i < 8 && cur; i++) {
                      const attrs = [
                        cur.id || "",
                        cur.className || "",
                        cur.getAttribute("data-testid") || "",
                        cur.getAttribute("aria-label") || "",
                        cur.getAttribute("title") || "",
                        cur.getAttribute("name") || ""
                      ].join(" ");
                      const text = String(cur.innerText || "").slice(0, 180);
                      const st = window.getComputedStyle(cur);
                      const rect = cur.getBoundingClientRect();
                      const fixedRightPanel =
                        (st.position === "fixed" || st.position === "sticky") &&
                        rect.width > 120 &&
                        rect.width <= 460 &&
                        rect.left > window.innerWidth * 0.45;
                      if (hasAssistKeyword(attrs)) return true;
                      if (fixedRightPanel && hasAssistKeyword(text)) return true;
                      cur = cur.parentElement;
                    }
                    return false;
                  })();
                  // Toggle / value state for fingerprinting
                  let checked = null;
                  if (type === "checkbox" || type === "radio") {
                    checked = !!el.checked;
                  } else if (el.getAttribute("aria-pressed") !== null) {
                    checked = el.getAttribute("aria-pressed") === "true";
                  } else if (el.getAttribute("aria-selected") !== null) {
                    checked = el.getAttribute("aria-selected") === "true";
                  } else if (el.getAttribute("aria-checked") !== null) {
                    checked = el.getAttribute("aria-checked") === "true";
                  }
                  const rawVal = el.value || "";
                  const valueHint = rawVal.length > 20 ? rawVal.substring(0, 20) : rawVal;
                  return { label, aria, placeholder, text, name, type, tag, required, inForm, inAssistPanel, checked, valueHint };
                }
                """
            )
        except Exception:
            return {
                "label": "",
                "aria": "",
                "placeholder": "",
                "text": "",
                "name": "",
                "type": "",
                "tag": "",
                "required": False,
                "inForm": False,
                "inAssistPanel": False,
                "checked": None,
                "valueHint": "",
            }

    for role in ROLE_ORDER:
        locator = page.get_by_role(role)
        try:
            count = locator.count()
        except Exception:
            continue
        if count <= 0:
            continue

        for i in range(min(count, max_per_role)):
            if len(items) >= max_total:
                break
            try:
                el = locator.nth(i)
                if not el.is_visible(timeout=100):
                    continue
                meta = _describe(el)
                name = (
                    meta.get("label")
                    or meta.get("aria")
                    or meta.get("text")
                    or meta.get("placeholder")
                    or meta.get("name")
                )
                name = (name or "").strip()
                if not name:
                    continue
                key = (role, name)
                nth = name_counters.get(key, 0)
                name_counters[key] = nth + 1
                raw_checked = meta.get("checked")
                item = SnapshotItem(
                    ref="",
                    role=role,
                    name=name,
                    nth=nth,
                    input_type=meta.get("type") or None,
                    tag=meta.get("tag") or None,
                    required=bool(meta.get("required")),
                    in_form=bool(meta.get("inForm")),
                    in_assist_panel=bool(meta.get("inAssistPanel")),
                    checked=bool(raw_checked) if raw_checked is not None else None,
                    value_hint=str(meta.get("valueHint") or ""),
                )
                items.append(item)
            except Exception:
                continue

    # 补充 file input（很多上传控件是隐藏 input[type=file]，不能仅依赖可见 role）
    try:
        file_locator = page.locator("input[type='file']")
        file_count = file_locator.count()
    except Exception:
        file_count = 0

    for i in range(min(file_count, max_per_role)):
        if len(items) >= max_total:
            break
        try:
            el = file_locator.nth(i)
            meta = _describe(el)
            name = (
                meta.get("label")
                or meta.get("aria")
                or meta.get("name")
                or meta.get("placeholder")
                or f"file upload input {i + 1}"
            )
            name = (name or "").strip()
            key = ("file_input", name)
            nth = name_counters.get(key, 0)
            name_counters[key] = nth + 1
            item = SnapshotItem(
                ref="",
                role="file_input",
                name=name,
                nth=nth,
                input_type="file",
                tag=meta.get("tag") or "input",
                required=bool(meta.get("required")),
                in_form=bool(meta.get("inForm")),
                in_assist_panel=bool(meta.get("inAssistPanel")),
                checked=None,
                value_hint=str(meta.get("valueHint") or ""),
            )
            items.append(item)
        except Exception:
            continue

    assist_filter_mode = os.getenv("SNAPSHOT_ASSIST_FILTER_MODE", "exclude").lower()
    # 默认排除插件侧面板（例如 Simplify）元素，避免污染主页面决策
    if assist_filter_mode in {"exclude", "on", "1", "true"}:
        non_assist_items = [item for item in items if not item.in_assist_panel]
        if non_assist_items:
            items = non_assist_items

    # 优先保留表单内元素（若存在）
    in_form_items = [item for item in items if item.in_form]
    if in_form_items:
        items = in_form_items

    # 必填优先
    items = sorted(items, key=lambda x: (not x.required, x.role))

    for idx, item in enumerate(items):
        ref = _next_ref(idx)
        item.ref = ref
        ref_map[ref] = item

    snapshot_lines = []
    for item in items:
        type_hint = f", type={item.input_type}" if item.input_type else ""
        req_hint = ", required" if item.required else ""
        snapshot_lines.append(
            f"{item.ref} | role={item.role}{type_hint}{req_hint} | name={item.name}"
        )

    snapshot_text = "\n".join(snapshot_lines) if snapshot_lines else "（无可交互元素）"
    return snapshot_text, ref_map
