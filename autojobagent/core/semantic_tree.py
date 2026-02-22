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


def _snapshot_ref_lookup(
    snapshot_map: dict[str, SnapshotItem],
) -> dict[tuple[str, str], list[str]]:
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


def _is_option_like_snapshot_label(label: str) -> bool:
    lower = _normalize_text(label).lower()
    if not lower:
        return False
    nav_words = (
        "submit",
        "apply",
        "upload",
        "replace",
        "next",
        "continue",
        "back",
        "cancel",
        "save",
        "autofill",
        "preview",
    )
    if any(token in lower for token in nav_words):
        return False
    return 0 < len(lower) <= 40


def _build_question_blocks_from_visible_text(
    *,
    visible_text: str,
    snapshot_map: dict[str, SnapshotItem],
    ref_lookup: dict[tuple[str, str], list[str]],
    start_idx: int,
) -> list["QuestionBlock"]:
    """
    文本线索兜底：
    - 从页面可见文本中提取“问题行 + 选项行”结构
    - 与 snapshot 的短标签选项交叉匹配，尽量补齐漏检的问题块
    """
    if not visible_text:
        return []

    option_candidates: dict[str, tuple[str, str]] = {}
    for item in snapshot_map.values():
        role = (item.role or "").strip().lower()
        if role not in ("button", "radio", "checkbox"):
            continue
        text = _normalize_text(item.name)
        if not _is_option_like_snapshot_label(text):
            continue
        key = text.lower()
        option_candidates.setdefault(key, (text, role))
    if len(option_candidates) < 2:
        return []

    lines = [
        _normalize_text(raw)
        for raw in (visible_text or "").splitlines()
        if _normalize_text(raw)
    ]
    if len(lines) < 3:
        return []

    def _looks_like_question_line(line: str) -> bool:
        lower = line.lower()
        if len(line) < 6 or len(line) > 240:
            return False
        return (
            line.endswith("?")
            or line.endswith("*")
            or " are you " in lower
            or lower.startswith("do you")
            or lower.startswith("which ")
            or lower.startswith("what ")
        )

    blocks: list[QuestionBlock] = []
    block_idx = start_idx
    seen: set[tuple[str, tuple[str, ...]]] = set()
    total = len(lines)
    for idx, line in enumerate(lines):
        if not _looks_like_question_line(line):
            continue
        option_hits: list[OptionNode] = []
        selected_options: list[str] = []
        for next_idx in range(idx + 1, min(total, idx + 8)):
            candidate = lines[next_idx]
            if _looks_like_question_line(candidate):
                break
            key = candidate.lower()
            item = option_candidates.get(key)
            if not item:
                continue
            text, role = item
            ref_id = _try_consume_ref(ref_lookup, role=role, text=text)
            option_hits.append(
                OptionNode(
                    text=text,
                    role=role,
                    selected=False,
                    disabled=False,
                    ref_id=ref_id,
                )
            )
        if len(option_hits) < 2:
            continue
        option_key = tuple(sorted(_normalize_text(opt.text).lower() for opt in option_hits))
        signature = (_normalize_text(line).lower(), option_key)
        if signature in seen:
            continue
        seen.add(signature)
        blocks.append(
            QuestionBlock(
                question_id=f"q{block_idx}",
                question_text=_normalize_text(line),
                control_type="choice_group",
                required=line.endswith("*") or ("required" in line.lower()),
                has_error=False,
                options=option_hits,
                selected_options=selected_options,
            )
        )
        block_idx += 1
        if len(blocks) >= 16:
            break

    return blocks


def _should_probe_question_fallback(
    snapshot_map: dict[str, SnapshotItem],
    blocks: list["QuestionBlock"],
) -> bool:
    if len(blocks) >= 2:
        return False
    labels = [
        _normalize_text(item.name).lower()
        for item in snapshot_map.values()
        if item.role in ("button", "radio", "checkbox")
        and _is_option_like_snapshot_label(item.name)
    ]
    if len(labels) < 4:
        return False
    has_dup = len(set(labels)) < len(labels)
    has_binary = any(x in ("yes", "no", "true", "false") for x in labels)
    return has_dup or has_binary


def _parse_question_blocks(
    raw_blocks: object,
    *,
    ref_lookup: dict[tuple[str, str], list[str]],
    max_blocks: int = 16,
    start_idx: int = 1,
) -> list["QuestionBlock"]:
    if not isinstance(raw_blocks, list):
        return []

    blocks: list[QuestionBlock] = []
    for idx, raw in enumerate(raw_blocks[:max_blocks], start=start_idx):
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
        blocks.append(
            QuestionBlock(
                question_id=f"q{idx}",
                question_text=question_text,
                control_type=_normalize_text(
                    str(raw.get("control_type") or "choice_group")
                ),
                required=bool(raw.get("required", False)),
                has_error=bool(raw.get("has_error", False)),
                options=options,
                selected_options=selected_options,
            )
        )
    return blocks


def build_question_blocks(
    page, snapshot_map: dict[str, SnapshotItem], visible_text: str = ""
) -> list[QuestionBlock]:
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
              const optionLikeButton = (el) => {
                const tag = (el.tagName || "").toLowerCase();
                if (tag !== "button" && el.getAttribute("role") !== "button") return false;
                const text = clean(el.innerText || el.textContent || el.getAttribute("aria-label") || "");
                if (!text || text.length > 60) return false;
                const lower = text.toLowerCase();
                const navWords = ["submit", "apply", "upload", "replace", "next", "continue", "back", "cancel", "save", "autofill", "preview"];
                if (navWords.some((w) => lower.includes(w))) return false;
                if (!el.closest("form")) return false;
                return true;
              };
              const isControlCandidate = (el) => {
                const tag = (el.tagName || "").toLowerCase();
                const type = (el.getAttribute("type") || "").toLowerCase();
                const role = (el.getAttribute("role") || "").toLowerCase();
                if (tag === "input" && (type === "radio" || type === "checkbox")) return true;
                if (role === "radio" || role === "checkbox") return true;
                if (el.hasAttribute("aria-pressed") || el.hasAttribute("aria-checked")) return true;
                return optionLikeButton(el);
              };
              const allCandidates = Array.from(document.querySelectorAll(
                "input[type='radio'], input[type='checkbox'], [role='radio'], [role='checkbox'], button, [role='button'], [aria-pressed], [aria-checked]"
              )).filter((el) => isVisible(el) && isControlCandidate(el));

              const controlsInside = (root) => {
                if (!root) return [];
                return allCandidates.filter((node) => root.contains(node));
              };

              const findGroupContainer = (el) => {
                let cur = el;
                let depth = 0;
                while (cur && depth < 8) {
                  const controls = controlsInside(cur);
                  if (controls.length >= 2) {
                    return cur;
                  }
                  cur = cur.parentElement;
                  depth += 1;
                }
                return el.parentElement;
              };
              const controlCandidates = allCandidates;

              const containerOf = (el) => {
                const hinted = (
                  el.closest("fieldset") ||
                  el.closest("[role='radiogroup']") ||
                  el.closest("[role='group']") ||
                  el.closest("[data-testid*='question' i]") ||
                  el.closest("[class*='question' i]") ||
                  el.closest("[aria-labelledby]") ||
                  el.closest("li") ||
                  el.closest("section")
                );
                if (hinted) {
                  const hintedControls = controlsInside(hinted);
                  if (hintedControls.length >= 2) return hinted;
                }
                return findGroupContainer(el);
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
                const optionsTexts = new Set(
                  Array.from(container.querySelectorAll("button, [role='button'], label"))
                    .map((n) => clean(n.innerText || n.textContent || n.getAttribute("aria-label")))
                    .filter(Boolean)
                );
                const candidates = Array.from(
                  container.querySelectorAll("label, h1, h2, h3, h4, p, span, strong, legend")
                )
                  .map((n) => clean(n.innerText || n.textContent))
                  .filter((t) => t && t.length >= 6 && !optionsTexts.has(t));
                if (candidates.length) {
                  const withQuestion = candidates.find((t) => t.includes("?"));
                  return withQuestion || candidates[0];
                }
                const previousText = clean(
                  container.previousElementSibling?.innerText ||
                  container.previousElementSibling?.textContent
                );
                if (previousText && previousText.length >= 6) return previousText;
                return "";
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
                const raw = labelFromFor || own || aria || parent;
                return clean(raw.split("\\n")[0] || raw);
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
                const containerText = clean(container?.innerText || container?.textContent || "");
                const required = (
                  !!el.required ||
                  el.getAttribute("aria-required") === "true" ||
                  /\\*\\s*$/.test(qText) ||
                  /\\brequired\\b/i.test(containerText)
                );
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
                .filter((g) => g.options && g.options.length >= 2 && g.question_text && g.question_text.length >= 6)
                .slice(0, 16);
            }
            """
        )
    except Exception:
        return []

    blocks = _parse_question_blocks(raw_blocks, ref_lookup=ref_lookup)
    if _should_probe_question_fallback(snapshot_map, blocks):
        try:
            fallback_raw = page.evaluate(
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
                  const navWords = ["submit", "apply", "upload", "replace", "next", "continue", "back", "cancel", "save", "autofill", "preview"];
                  const isOptionLike = (el) => {
                    if (!isVisible(el)) return false;
                    const tag = (el.tagName || "").toLowerCase();
                    const role = (el.getAttribute("role") || "").toLowerCase();
                    const type = (el.getAttribute("type") || "").toLowerCase();
                    if (!(tag === "button" || role === "button" || role === "radio" || role === "checkbox" || (tag === "input" && (type === "radio" || type === "checkbox")))) return false;
                    const txt = clean(el.innerText || el.textContent || el.getAttribute("aria-label"));
                    if (!txt || txt.length > 40) return false;
                    const lower = txt.toLowerCase();
                    if (navWords.some((w) => lower.includes(w))) return false;
                    return !!el.closest("form");
                  };
                  const textOf = (el) => clean(el?.innerText || el?.textContent || el?.getAttribute("aria-label") || "");
                  const controls = Array.from(document.querySelectorAll("input[type='radio'], input[type='checkbox'], button, [role='button'], [role='radio'], [role='checkbox']"))
                    .filter(isOptionLike);
                  if (controls.length < 2) return [];

                  const questionNodes = Array.from(document.querySelectorAll("label, legend, p, span, strong, h1, h2, h3, h4, div"))
                    .filter((n) => isVisible(n))
                    .map((n) => {
                      const text = clean(n.innerText || n.textContent || "");
                      const rect = n.getBoundingClientRect();
                      return { text, rect };
                    })
                    .filter((x) => x.text && x.text.length >= 6 && x.text.length <= 220)
                    .filter((x) => x.text.includes("?") || /\\*\\s*$/.test(x.text));

                  const nearestQuestion = (ctrl) => {
                    const rect = ctrl.getBoundingClientRect();
                    let best = null;
                    let score = Number.POSITIVE_INFINITY;
                    questionNodes.forEach((q) => {
                      const verticalGap = rect.top - q.rect.bottom;
                      if (verticalGap < -6 || verticalGap > 220) return;
                      const horizontalGap = Math.abs(q.rect.left - rect.left);
                      if (horizontalGap > 380) return;
                      const curScore = verticalGap * 2 + horizontalGap * 0.2;
                      if (curScore < score) {
                        score = curScore;
                        best = q.text;
                      }
                    });
                    return best || "";
                  };

                  const selectedOf = (el) => {
                    if (el.matches("input[type='radio'], input[type='checkbox']")) return !!el.checked;
                    const ariaChecked = String(el.getAttribute("aria-checked") || "").toLowerCase();
                    if (ariaChecked === "true") return true;
                    if (ariaChecked === "false") return false;
                    const ariaPressed = String(el.getAttribute("aria-pressed") || "").toLowerCase();
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
                    return role || tag || "button";
                  };

                  const groups = new Map();
                  controls.forEach((ctrl) => {
                    const qText = nearestQuestion(ctrl);
                    const optText = clean(textOf(ctrl));
                    if (!qText || !optText) return;
                    const key = qText.toLowerCase();
                    const bucket = groups.get(key) || {
                      question_id: `fq${groups.size + 1}`,
                      question_text: qText,
                      control_type: "choice_group",
                      required: /\\*\\s*$/.test(qText) || /\\brequired\\b/i.test(qText) || !!ctrl.required || ctrl.getAttribute("aria-required") === "true",
                      has_error: false,
                      options: []
                    };
                    const optionKey = optText.toLowerCase();
                    if (!bucket.options.some((o) => String(o.text || "").toLowerCase() === optionKey)) {
                      bucket.options.push({
                        text: optText,
                        role: roleOf(ctrl),
                        selected: selectedOf(ctrl),
                        disabled: !!ctrl.disabled || ctrl.getAttribute("aria-disabled") === "true"
                      });
                    }
                    groups.set(key, bucket);
                  });
                  return Array.from(groups.values())
                    .filter((g) => g.options && g.options.length >= 2)
                    .slice(0, 16);
                }
                """
            )
        except Exception:
            fallback_raw = []
        fallback_blocks = _parse_question_blocks(
            fallback_raw,
            ref_lookup=ref_lookup,
            start_idx=len(blocks) + 1,
        )
        if fallback_blocks:
            existing_keys = {
                (
                    _normalize_text(b.question_text).lower(),
                    tuple(sorted(_normalize_text(o.text).lower() for o in b.options)),
                )
                for b in blocks
            }
            for fb in fallback_blocks:
                key = (
                    _normalize_text(fb.question_text).lower(),
                    tuple(sorted(_normalize_text(o.text).lower() for o in fb.options)),
                )
                if key in existing_keys:
                    continue
                blocks.append(fb)
                existing_keys.add(key)
                if len(blocks) >= 16:
                    break
        text_blocks = _build_question_blocks_from_visible_text(
            visible_text=visible_text,
            snapshot_map=snapshot_map,
            ref_lookup=ref_lookup,
            start_idx=len(blocks) + 1,
        )
        if text_blocks:
            existing_keys = {
                (
                    _normalize_text(b.question_text).lower(),
                    tuple(sorted(_normalize_text(o.text).lower() for o in b.options)),
                )
                for b in blocks
            }
            for tb in text_blocks:
                key = (
                    _normalize_text(tb.question_text).lower(),
                    tuple(sorted(_normalize_text(o.text).lower() for o in tb.options)),
                )
                if key in existing_keys:
                    continue
                blocks.append(tb)
                existing_keys.add(key)
                if len(blocks) >= 16:
                    break
    return blocks


def format_question_blocks(question_blocks: list[QuestionBlock]) -> str:
    if not question_blocks:
        return "（未检测到结构化问题块）"
    lines: list[str] = []
    for block in question_blocks[:8]:
        required = "required" if block.required else "optional"
        err = "error" if block.has_error else "ok"
        selected = (
            ", ".join(block.selected_options[:3]) if block.selected_options else "none"
        )
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
