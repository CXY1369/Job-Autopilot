"""
可导航语义树（Phase A 最小版）

职责：
- 从 DOM 提取问题块（QuestionBlock）与选项（OptionNode）
- 将问题块摘要为文本，供 LLM 规划时绑定问题语义而非仅依赖按钮文案
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from .ui_snapshot import SnapshotItem


@dataclass
class OptionNode:
    text: str
    role: str
    selected: bool
    disabled: bool = False
    ref_id: str | None = None


@dataclass
class QuestionBlock:
    question_id: str
    question_text: str
    control_type: str
    required: bool
    has_error: bool
    options: list[OptionNode]
    selected_options: list[str]


@dataclass
class FieldNode:
    ref_id: str
    label: str
    role: str
    required: bool
    filled: bool
    has_error: bool


@dataclass
class FormGraph:
    page_scope: str
    fields: list[FieldNode]
    questions: list[QuestionBlock]
    submit_refs: list[str]
    required_unfilled: list[str]
    error_snippets: list[str]


def _normalize_text(value: str | None) -> str:
    return " ".join((value or "").split()).strip()


def _snapshot_ref_lookup(snapshot_map: dict[str, SnapshotItem]) -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = {}
    for ref, item in snapshot_map.items():
        key = (item.role.strip().lower(), _normalize_text(item.name).lower())
        out.setdefault(key, []).append(ref)
    return out


def _try_consume_ref(
    ref_lookup: dict[tuple[str, str], list[str]],
    *,
    role: str,
    text: str,
) -> str | None:
    key = (role.strip().lower(), _normalize_text(text).lower())
    refs = ref_lookup.get(key) or []
    if not refs:
        return None
    return refs.pop(0)


def build_question_blocks(page, snapshot_map: dict[str, SnapshotItem]) -> list[QuestionBlock]:
    """
    从页面中提取问题块（单选/多选/按钮组选项）。
    失败时返回空列表，保证不影响主流程。
    """
    ref_lookup = _snapshot_ref_lookup(snapshot_map)
    try:
        raw_blocks = page.evaluate(
            """
            () => {
              const clean = (v) => String(v || "").replace(/\\s+/g, " ").trim();
              const isVisible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (!st) return false;
                if (st.display === "none" || st.visibility === "hidden") return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
              };
              const controlCandidates = Array.from(document.querySelectorAll(
                "input[type='radio'], input[type='checkbox'], [role='radio'], [role='checkbox'], button[aria-pressed], [aria-pressed='true'], [aria-pressed='false']"
              )).filter(isVisible);

              const containerOf = (el) => {
                return (
                  el.closest("fieldset") ||
                  el.closest("[role='radiogroup']") ||
                  el.closest("[role='group']") ||
                  el.closest("[aria-labelledby]") ||
                  el.parentElement
                );
              };

              const questionTextOf = (container) => {
                if (!container) return "";
                const legend = clean(container.querySelector("legend")?.innerText);
                if (legend) return legend;
                const ariaLabel = clean(container.getAttribute("aria-label"));
                if (ariaLabel) return ariaLabel;
                const labelledBy = container.getAttribute("aria-labelledby");
                if (labelledBy) {
                  const parts = labelledBy.split(/\\s+/).map((id) => clean(document.getElementById(id)?.innerText)).filter(Boolean);
                  if (parts.length) return clean(parts.join(" "));
                }
                const labelLike = clean(
                  container.querySelector("label, h1, h2, h3, h4, p, span")?.innerText
                );
                return labelLike;
              };

              const optionTextOf = (el) => {
                const labelFromFor = (() => {
                  const id = el.getAttribute("id");
                  if (!id) return "";
                  return clean(document.querySelector(`label[for="${id}"]`)?.innerText);
                })();
                const own = clean(el.innerText);
                const aria = clean(el.getAttribute("aria-label"));
                const parent = clean(el.parentElement?.innerText);
                return labelFromFor || own || aria || parent;
              };

              const selectedOf = (el) => {
                if (el.matches("input[type='radio'], input[type='checkbox']")) {
                  return !!el.checked;
                }
                const ariaChecked = el.getAttribute("aria-checked");
                if (ariaChecked === "true") return true;
                if (ariaChecked === "false") return false;
                const ariaPressed = el.getAttribute("aria-pressed");
                if (ariaPressed === "true") return true;
                if (ariaPressed === "false") return false;
                const cls = String(el.className || "").toLowerCase();
                return cls.includes("selected") || cls.includes("active") || cls.includes("checked");
              };

              const roleOf = (el) => {
                const tag = (el.tagName || "").toLowerCase();
                const type = (el.getAttribute("type") || "").toLowerCase();
                if (tag === "input" && type === "radio") return "radio";
                if (tag === "input" && type === "checkbox") return "checkbox";
                const role = (el.getAttribute("role") || "").toLowerCase();
                if (role) return role;
                return tag || "unknown";
              };

              const groups = new Map();
              controlCandidates.forEach((el, idx) => {
                const container = containerOf(el);
                const key = container || el;
                const qText = clean(questionTextOf(container));
                const optText = clean(optionTextOf(el));
                if (!qText || !optText) return;
                const role = roleOf(el);
                const selected = selectedOf(el);
                const disabled = !!el.disabled || el.getAttribute("aria-disabled") === "true";
                const required = !!el.required || el.getAttribute("aria-required") === "true" || /\\*\\s*$/.test(qText);
                const invalid = !!el.ariaInvalid || el.getAttribute("aria-invalid") === "true";
                const bucket = groups.get(key) || {
                  question_id: `q${groups.size + 1}`,
                  question_text: qText,
                  control_type: role === "radio" ? "single_choice" : "choice_group",
                  required,
                  has_error: invalid,
                  options: []
                };
                bucket.options.push({
                  text: optText,
                  role,
                  selected,
                  disabled
                });
                if (invalid) bucket.has_error = true;
                groups.set(key, bucket);
              });

              return Array.from(groups.values())
                .filter((g) => g.options && g.options.length >= 2)
                .slice(0, 16);
            }
            """
        )
    except Exception:
        return []

    if not isinstance(raw_blocks, list):
        return []

    blocks: list[QuestionBlock] = []
    for idx, raw in enumerate(raw_blocks[:16], start=1):
        if not isinstance(raw, dict):
            continue
        question_text = _normalize_text(str(raw.get("question_text") or ""))
        if not question_text:
            continue
        options_raw = raw.get("options")
        if not isinstance(options_raw, list):
            continue
        options: list[OptionNode] = []
        selected_options: list[str] = []
        for opt in options_raw[:10]:
            if not isinstance(opt, dict):
                continue
            text = _normalize_text(str(opt.get("text") or ""))
            role = _normalize_text(str(opt.get("role") or "button")).lower() or "button"
            if not text:
                continue
            selected = bool(opt.get("selected", False))
            disabled = bool(opt.get("disabled", False))
            ref_id = _try_consume_ref(ref_lookup, role=role, text=text)
            option = OptionNode(
                text=text,
                role=role,
                selected=selected,
                disabled=disabled,
                ref_id=ref_id,
            )
            options.append(option)
            if selected:
                selected_options.append(text)
        if len(options) < 2:
            continue
        block = QuestionBlock(
            question_id=f"q{idx}",
            question_text=question_text,
            control_type=_normalize_text(str(raw.get("control_type") or "choice_group")),
            required=bool(raw.get("required", False)),
            has_error=bool(raw.get("has_error", False)),
            options=options,
            selected_options=selected_options,
        )
        blocks.append(block)
    return blocks


def format_question_blocks(question_blocks: list[QuestionBlock]) -> str:
    if not question_blocks:
        return "（未检测到结构化问题块）"
    lines: list[str] = []
    for block in question_blocks[:8]:
        required = "required" if block.required else "optional"
        err = "error" if block.has_error else "ok"
        selected = ", ".join(block.selected_options[:3]) if block.selected_options else "none"
        lines.append(
            f"- [{block.question_id}] {block.question_text} ({block.control_type}, {required}, {err}, selected={selected})"
        )
        option_parts = []
        for opt in block.options[:6]:
            mark = "selected" if opt.selected else "unselected"
            ref = f", ref={opt.ref_id}" if opt.ref_id else ""
            option_parts.append(f"{opt.text}<{opt.role},{mark}{ref}>")
        lines.append(f"  options: {' | '.join(option_parts)}")
    return "\n".join(lines)


def _build_page_scope(current_url: str) -> str:
    try:
        parsed = urlsplit(current_url or "")
    except Exception:
        return "unknown|/"
    domain = (parsed.netloc or "unknown").lower()
    path = (parsed.path or "/").lower().strip() or "/"
    return f"{domain}|{path}"


def build_form_graph(
    *,
    current_url: str,
    snapshot_map: dict[str, SnapshotItem],
    question_blocks: list[QuestionBlock],
    error_snippets: list[str] | None = None,
) -> FormGraph:
    fields: list[FieldNode] = []
    required_unfilled: list[str] = []
    submit_refs: list[str] = []
    for ref, item in snapshot_map.items():
        role = (item.role or "").strip().lower()
        name = _normalize_text(item.name)
        value_hint = _normalize_text(item.value_hint)
        if role in ("textbox", "combobox", "file_input"):
            filled = bool(value_hint)
            has_error = bool(item.required and not filled)
            node = FieldNode(
                ref_id=ref,
                label=name or ref,
                role=role,
                required=bool(item.required),
                filled=filled,
                has_error=has_error,
            )
            fields.append(node)
            if node.required and not node.filled:
                required_unfilled.append(f"{node.label}<{node.role}>")
        if role in ("button", "link"):
            lowered = name.lower()
            if any(k in lowered for k in ("submit", "apply", "continue", "next")):
                submit_refs.append(ref)
    return FormGraph(
        page_scope=_build_page_scope(current_url),
        fields=fields[:80],
        questions=question_blocks[:20],
        submit_refs=submit_refs[:10],
        required_unfilled=required_unfilled[:20],
        error_snippets=[str(x)[:180] for x in (error_snippets or [])[:6]],
    )


def format_form_graph(form_graph: FormGraph) -> str:
    lines: list[str] = [
        f"scope={form_graph.page_scope}",
        f"required_unfilled={len(form_graph.required_unfilled)}",
        f"questions={len(form_graph.questions)}",
        f"submit_candidates={len(form_graph.submit_refs)}",
    ]
    if form_graph.required_unfilled:
        lines.append("required_fields: " + " | ".join(form_graph.required_unfilled[:8]))
    if form_graph.error_snippets:
        lines.append("errors: " + " | ".join(form_graph.error_snippets[:4]))
    return "\n".join(lines)
