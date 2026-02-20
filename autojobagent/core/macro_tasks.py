"""
Macro task planning (Phase B / generalized option mapper).

Goal:
- Build a stable task chain from semantic question blocks + profile rules.
- Keep selection logic generic across sites, not tied to Yes/No-only wording.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .semantic_tree import QuestionBlock
from .ui_snapshot import SnapshotItem


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).strip().lower()


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(k in text for k in keywords)


def _city_seed(text: str | None) -> str:
    value = _norm(text)
    if not value:
        return ""
    return value.split(",")[0].strip()


def _get_path(data: dict, path: tuple[str, ...], default: Any = None) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def _as_text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = _norm(value)
        if v in ("true", "yes", "y", "1"):
            return True
        if v in ("false", "no", "n", "0"):
            return False
    return None


def _match_option_text(options: list[str], wanted: str) -> str | None:
    w = _norm(wanted)
    if not w:
        return None
    exact = [opt for opt in options if _norm(opt) == w]
    if exact:
        return exact[0]
    starts = [opt for opt in options if _norm(opt).startswith(w)]
    if starts:
        return starts[0]
    contains = [opt for opt in options if w in _norm(opt) or _norm(opt) in w]
    if contains:
        return contains[0]
    return None


def _match_preferred_locations(
    *,
    question_options: list[str],
    preferred_locations: list[str],
) -> list[str]:
    if not question_options or not preferred_locations:
        return []
    matched: list[str] = []
    option_norm_map = {_norm(opt): opt for opt in question_options}
    for raw_pref in preferred_locations:
        pref = _city_seed(raw_pref)
        if not pref:
            continue
        pick = None
        for key, original in option_norm_map.items():
            if pref in key or key.startswith(pref):
                pick = original
                break
        if pick and pick not in matched:
            matched.append(pick)
    return matched


_POSITIVE_OPTION_CUES = [
    "yes",
    "authorized",
    "eligible",
    "require",
    "will",
    "have",
    "agree",
    "accept",
    "true",
]
_NEGATIVE_OPTION_CUES = [
    "no",
    "not",
    "do not",
    "don't",
    "without",
    "decline",
    "never",
    "false",
]
_NEUTRAL_OPTION_CUES = [
    "prefer not",
    "rather not",
    "decline to answer",
    "not specified",
    "n/a",
]


def _option_polarity(option: str) -> str | None:
    n = _norm(option)
    if not n:
        return None
    if n in ("yes", "y", "true"):
        return "positive"
    if n in ("no", "n", "false"):
        return "negative"
    if _contains_any(n, _NEUTRAL_OPTION_CUES):
        return "neutral"
    pos = sum(1 for k in _POSITIVE_OPTION_CUES if k in n)
    neg = sum(1 for k in _NEGATIVE_OPTION_CUES if k in n)
    if pos > neg and pos > 0:
        return "positive"
    if neg > pos and neg > 0:
        return "negative"
    return None


def _pick_boolean_option(options: list[str], desired: bool) -> str | None:
    direct = _match_option_text(options, "yes" if desired else "no")
    if direct:
        return direct
    target = "positive" if desired else "negative"
    for option in options:
        if _option_polarity(option) == target:
            return option
    return None


def _alias_candidates(value: str, aliases: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    val_norm = _norm(value)
    if not val_norm:
        return out
    out.append(value)
    for k, v in aliases.items():
        if _norm(k) == val_norm:
            out.extend(v)
    return [x for x in out if _norm(x)]


def _pick_by_candidates(options: list[str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        hit = _match_option_text(options, candidate)
        if hit:
            return hit
    return None


@dataclass
class MacroTask:
    task_id: str
    task_type: str  # combobox_select | question_single | question_multi
    title: str
    question_text: str | None = None
    field_ref: str | None = None
    field_selector: str | None = None
    target_value: str | None = None
    expected_options: list[str] = field(default_factory=list)
    mapping_reason: str | None = None
    precondition: str | None = None
    postcondition: str | None = None
    status: str = "pending"  # pending | in_progress | done | blocked
    retry_count: int = 0


@dataclass(frozen=True)
class MappingRule:
    rule_id: str
    keywords: tuple[str, ...]
    value_type: str  # bool | text | list
    profile_path: tuple[str, ...]
    aliases: dict[str, list[str]] = field(default_factory=dict)


_MAPPING_RULES: tuple[MappingRule, ...] = (
    MappingRule(
        rule_id="authorized_to_work",
        keywords=(
            "authorized to work",
            "legally authorized",
            "work in the united states",
        ),
        value_type="bool",
        profile_path=("work_authorization", "authorized_to_work_in_us"),
    ),
    MappingRule(
        rule_id="visa_sponsorship",
        keywords=("visa sponsorship", "require sponsorship", "need sponsorship"),
        value_type="bool",
        profile_path=("work_authorization", "require_visa_sponsorship"),
    ),
    MappingRule(
        rule_id="willing_relocate",
        keywords=("willing to relocate", "open to relocation", "relocate"),
        value_type="bool",
        profile_path=("work_preferences", "willing_to_relocate"),
    ),
    MappingRule(
        rule_id="over_18",
        keywords=("at least 18", "18 years old", "over 18"),
        value_type="bool",
        profile_path=("common_answers", "is_over_18"),
    ),
    MappingRule(
        rule_id="drivers_license",
        keywords=("driver's license", "drivers license", "driving licence"),
        value_type="bool",
        profile_path=("common_answers", "has_drivers_license"),
    ),
    MappingRule(
        rule_id="background_check",
        keywords=("background check",),
        value_type="bool",
        profile_path=("common_answers", "willing_background_check"),
    ),
    MappingRule(
        rule_id="drug_test",
        keywords=("drug test",),
        value_type="bool",
        profile_path=("common_answers", "willing_drug_test"),
    ),
    MappingRule(
        rule_id="has_relative",
        keywords=("relative at this company", "related to anyone", "family member"),
        value_type="bool",
        profile_path=("common_answers", "has_relative_at_company"),
    ),
    MappingRule(
        rule_id="previously_worked_here",
        keywords=("previously worked", "worked at this company"),
        value_type="bool",
        profile_path=("common_answers", "previously_worked_at_company"),
    ),
    MappingRule(
        rule_id="remote_preference",
        keywords=("work preference", "work arrangement", "remote", "onsite", "hybrid"),
        value_type="text",
        profile_path=("work_preferences", "remote_work_preference"),
        aliases={
            "remote": ["remote only", "fully remote", "work from home"],
            "onsite": ["on-site", "in office", "office"],
            "hybrid": ["hybrid", "mixed", "remote + onsite"],
            "flexible": ["flexible", "either", "open"],
        },
    ),
    MappingRule(
        rule_id="gender",
        keywords=("gender",),
        value_type="text",
        profile_path=("demographics", "gender"),
    ),
    MappingRule(
        rule_id="ethnicity",
        keywords=("ethnicity", "race", "hispanic", "asian", "white", "black"),
        value_type="text",
        profile_path=("demographics", "ethnicity"),
    ),
    MappingRule(
        rule_id="veteran_status",
        keywords=("veteran",),
        value_type="text",
        profile_path=("demographics", "veteran_status"),
    ),
    MappingRule(
        rule_id="disability_status",
        keywords=("disability", "disabled"),
        value_type="text",
        profile_path=("demographics", "disability_status"),
    ),
    MappingRule(
        rule_id="referral_source",
        keywords=("how did you hear", "referral source", "where did you hear"),
        value_type="text",
        profile_path=("common_answers", "referral_source"),
    ),
)


def _resolve_rule_mapping(
    *,
    question_text: str,
    options: list[str],
    profile: dict,
) -> tuple[list[str], str | None]:
    lower_q = _norm(question_text)
    for rule in _MAPPING_RULES:
        if not _contains_any(lower_q, list(rule.keywords)):
            continue
        raw_value = _get_path(profile, rule.profile_path, None)
        if raw_value is None:
            continue

        if rule.value_type == "bool":
            desired = _to_bool(raw_value)
            if desired is None:
                continue
            hit = _pick_boolean_option(options, desired)
            if hit:
                return [hit], rule.rule_id
            continue

        if rule.value_type == "text":
            value = str(raw_value).strip()
            if not value:
                continue
            candidates = _alias_candidates(value, rule.aliases)
            hit = _pick_by_candidates(options, candidates)
            if hit:
                return [hit], rule.rule_id
            continue

        if rule.value_type == "list":
            wanted_values = _as_text_list(raw_value)
            picked: list[str] = []
            for wanted in wanted_values:
                hit = _match_option_text(options, wanted)
                if hit and hit not in picked:
                    picked.append(hit)
            if picked:
                return picked, rule.rule_id

    return [], None


def _resolve_custom_option_rules(
    *,
    question_text: str,
    options: list[str],
    profile: dict,
) -> tuple[list[str], str | None]:
    raw_rules = profile.get("option_rules", []) if isinstance(profile, dict) else []
    if not isinstance(raw_rules, list):
        return [], None
    lower_q = _norm(question_text)
    for idx, rule in enumerate(raw_rules, start=1):
        if not isinstance(rule, dict):
            continue
        keywords_raw = rule.get("question_keywords", [])
        keywords = [str(x).strip().lower() for x in keywords_raw if str(x).strip()]
        if keywords and not _contains_any(lower_q, keywords):
            continue
        answers = _as_text_list(rule.get("answers", []))
        if not answers:
            answers = _as_text_list(rule.get("answer", ""))
        if not answers:
            continue
        picked: list[str] = []
        for wanted in answers:
            hit = _match_option_text(options, wanted)
            if hit and hit not in picked:
                picked.append(hit)
        if picked:
            return picked, f"custom_rule_{idx}"
    return [], None


def _resolve_question_mapping(
    *,
    qb: QuestionBlock,
    profile: dict,
) -> tuple[list[str], str | None]:
    options = [opt.text for opt in qb.options if opt.text]
    if not options:
        return [], None

    lower_q = _norm(qb.question_text)
    work_pref = profile.get("work_preferences", {}) if isinstance(profile, dict) else {}
    preferred_locations = (
        work_pref.get("preferred_locations", [])
        if isinstance(work_pref.get("preferred_locations", []), list)
        else []
    )

    # Priority 1: office-like multi-selection questions
    if _contains_any(
        lower_q, ["which office", "willing to work out of", "work out of"]
    ):
        expected = _match_preferred_locations(
            question_options=options,
            preferred_locations=preferred_locations,
        )
        if expected:
            return expected, "preferred_locations"

    # Priority 2: built-in profile rules
    expected, reason = _resolve_rule_mapping(
        question_text=qb.question_text,
        options=options,
        profile=profile,
    )
    if expected:
        return expected, reason

    # Priority 3: user custom rules (supports A/B/C and arbitrary site vocab)
    expected, reason = _resolve_custom_option_rules(
        question_text=qb.question_text,
        options=options,
        profile=profile,
    )
    if expected:
        return expected, reason

    return [], None


def build_macro_tasks(
    *,
    profile: dict,
    snapshot_map: dict[str, SnapshotItem],
    question_blocks: list[QuestionBlock],
) -> list[MacroTask]:
    tasks: list[MacroTask] = []
    task_idx = 1

    location_cfg = profile.get("location", {}) if isinstance(profile, dict) else {}
    target_location = (location_cfg.get("full_location") or "").strip() or (
        location_cfg.get("current_city") or ""
    ).strip()

    # 1) Combobox location task
    if target_location:
        combos = [it for it in snapshot_map.values() if it.role == "combobox"]
        if combos:
            combo = combos[0]
            tasks.append(
                MacroTask(
                    task_id=f"t{task_idx}",
                    task_type="combobox_select",
                    title="Fill location combobox",
                    field_ref=combo.ref,
                    field_selector=combo.name,
                    target_value=target_location,
                    mapping_reason="location.full_location",
                    precondition="location_combobox_present",
                    postcondition="combobox_value_selected",
                )
            )
            task_idx += 1

    # 2) Question tasks from semantic blocks (generalized option mapper)
    for qb in question_blocks:
        expected, reason = _resolve_question_mapping(qb=qb, profile=profile)
        if not expected:
            continue
        option_roles = {(_norm(opt.role) or "button") for opt in qb.options}
        is_multi = len(expected) > 1 or "checkbox" in option_roles
        task_type = "question_multi" if is_multi else "question_single"
        tasks.append(
            MacroTask(
                task_id=f"t{task_idx}",
                task_type=task_type,
                title="Answer required question",
                question_text=qb.question_text,
                expected_options=expected,
                mapping_reason=reason,
                precondition="question_block_present",
                postcondition="expected_option_selected",
            )
        )
        task_idx += 1

    return tasks


def summarize_macro_tasks(tasks: list[MacroTask]) -> list[str]:
    out: list[str] = []
    for task in tasks:
        status = task.status
        if task.task_type == "combobox_select":
            out.append(
                f"{task.task_id}:{status}: combobox -> {task.target_value or ''}"
            )
        else:
            reason = f" [{task.mapping_reason}]" if task.mapping_reason else ""
            out.append(
                f"{task.task_id}:{status}: {task.question_text or task.title} -> {', '.join(task.expected_options[:4])}{reason}"
            )
    return out[:10]
