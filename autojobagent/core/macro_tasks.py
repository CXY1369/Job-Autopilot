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


def _looks_required_label(label: str) -> bool:
    return bool(_norm(label).endswith("*"))


def _is_resume_upload_label(label: str) -> bool:
    lower = _norm(label)
    if not lower:
        return False
    return "resume" in lower or lower == "cv" or "curriculum vitae" in lower


def _is_cover_letter_label(label: str) -> bool:
    return "cover letter" in _norm(label)


def _is_upload_label(label: str) -> bool:
    lower = _norm(label)
    if not lower:
        return False
    return _contains_any(
        lower,
        ["upload", "attach", "drop file", "drag and drop", "choose file", "replace"],
    )


def _is_optional_prompt_label(label: str) -> bool:
    lower = _norm(label)
    return _contains_any(
        lower,
        [
            "why",
            "interested",
            "motivation",
            "cover letter",
            "skills",
            "experience",
            "about you",
        ],
    )


def _is_location_question_label(label: str) -> bool:
    lower = _norm(label)
    if not lower:
        return False
    return lower in ("location", "start typing...") or _contains_any(
        lower,
        [
            "where are you located",
            "your location",
            "current location",
            "location preference",
        ],
    )


def _preferred_resume_value(
    profile: dict,
    preferred_resume_path: str | None = None,
) -> str | None:
    preferred = str(preferred_resume_path or "").strip()
    if preferred:
        return preferred
    if not isinstance(profile, dict):
        return None
    files = profile.get("files", {})
    if not isinstance(files, dict):
        return None
    value = str(files.get("default_resume") or "").strip()
    return value or None


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
    task_type: str  # combobox_select | field_fill | field_fill_optional | question_single | question_multi | inference_required | file_upload | manual_required
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
    wait_count: int = 0
    completed_options: list[str] = field(default_factory=list)
    last_attempt_option: str | None = None
    required: bool = False


@dataclass(frozen=True)
class MappingRule:
    rule_id: str
    keywords: tuple[str, ...]
    value_type: str  # bool | text | list
    profile_path: tuple[str, ...]
    aliases: dict[str, list[str]] = field(default_factory=dict)


_MAPPING_RULES: tuple[MappingRule, ...] = (
    MappingRule(
        rule_id="visa_sponsorship",
        keywords=("visa sponsorship", "require sponsorship", "need sponsorship"),
        value_type="bool",
        profile_path=("work_authorization", "require_visa_sponsorship"),
    ),
    MappingRule(
        rule_id="authorized_to_work",
        keywords=(
            "authorized to work",
            "legally authorized",
            "employment authorized",
        ),
        value_type="bool",
        profile_path=("work_authorization", "authorized_to_work_in_us"),
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


def _build_generic_motivation(profile: dict) -> str:
    skills = profile.get("skills", {}) if isinstance(profile, dict) else {}
    domains = skills.get("domains", []) if isinstance(skills, dict) else []
    domain_text = ""
    if isinstance(domains, list):
        picked = [str(x).strip() for x in domains if str(x).strip()][:2]
        if picked:
            domain_text = " and ".join(picked)
    experience = profile.get("experience", {}) if isinstance(profile, dict) else {}
    current_title = ""
    if isinstance(experience, dict):
        current_title = str(experience.get("current_title") or "").strip()
    if domain_text:
        return (
            f"I am excited about this opportunity and can contribute with strong "
            f"experience in {domain_text}."
        )
    if current_title:
        return (
            f"I am excited about this opportunity and believe my background as "
            f"{current_title} aligns well with this role."
        )
    return "I am excited about this opportunity and believe I can add meaningful value to the team."


def _build_generic_experience_summary(profile: dict) -> str:
    experience = profile.get("experience", {}) if isinstance(profile, dict) else {}
    current_title = ""
    years = ""
    if isinstance(experience, dict):
        current_title = str(experience.get("current_title") or "").strip()
        years = str(experience.get("years_total") or "").strip()
    skills = profile.get("skills", {}) if isinstance(profile, dict) else {}
    domains = skills.get("domains", []) if isinstance(skills, dict) else []
    domain_text = ""
    if isinstance(domains, list):
        picked = [str(x).strip() for x in domains if str(x).strip()][:2]
        if picked:
            domain_text = " and ".join(picked)

    sentence = "I have hands-on experience delivering end-to-end initiatives"
    if years:
        sentence = f"I have {years} years of hands-on experience delivering end-to-end initiatives"
    if current_title:
        sentence += f" as a {current_title}"
    if domain_text:
        sentence += f", especially in {domain_text}"
    return sentence + "."


def _resolve_text_field_mapping(
    *,
    profile: dict,
    field_label: str,
    input_type: str | None,
) -> tuple[str | None, str | None]:
    common = profile.get("common_answers", {}) if isinstance(profile, dict) else {}
    lower = _norm(field_label)
    in_type = _norm(input_type)

    def _pick(path: tuple[str, ...]) -> str:
        value = _get_path(profile, path, "")
        return str(value or "").strip()

    if in_type == "email" or "email" in lower:
        value = _pick(("personal", "email"))
        if value:
            return value, "personal.email"
    if "first name" in lower:
        value = _pick(("personal", "first_name"))
        if value:
            return value, "personal.first_name"
    if "last name" in lower:
        value = _pick(("personal", "last_name"))
        if value:
            return value, "personal.last_name"
    if "name" in lower:
        value = _pick(("personal", "full_name"))
        if value:
            return value, "personal.full_name"
    if any(k in lower for k in ("phone", "mobile", "telephone")):
        value = _pick(("personal", "phone"))
        if value:
            return value, "personal.phone"
    if any(k in lower for k in ("linkedin", "linked in")):
        value = _pick(("personal", "linkedin"))
        if value:
            return value, "personal.linkedin"
    if any(k in lower for k in ("website", "portfolio", "github")):
        value = _pick(("personal", "website"))
        if value:
            return value, "personal.website"
    if "location" in lower:
        value = _pick(("location", "full_location")) or _pick(("location", "current_city"))
        if value:
            return value, "location.full_location"
    if any(k in lower for k in ("why", "interested", "motivation", "cover letter")):
        value = ""
        if isinstance(common, dict):
            value = str(
                common.get("why_this_company")
                or common.get("why_interested")
                or common.get("motivation")
                or ""
            ).strip()
        if not value:
            value = _build_generic_motivation(profile)
        if value:
            return value, "common_answers.why_or_motivation"
    if _contains_any(
        lower,
        [
            "describe your work experience",
            "tell us about your experience",
            "relevant experience",
            "in a couple sentences",
            "in 4 sentences or fewer",
            "4 sentences or fewer",
            "briefly describe",
        ],
    ):
        value = ""
        if isinstance(common, dict):
            value = str(
                common.get("experience_summary_short")
                or common.get("experience_summary")
                or common.get("relevant_experience")
                or ""
            ).strip()
        if not value:
            value = _build_generic_experience_summary(profile)
        if value:
            return value, "common_answers.experience_summary"
    # 有些站点把 LinkedIn 放在普通文本字段名里，做最后一次兜底
    if "profile" in lower and ("link" in lower or "url" in lower):
        value = _pick(("personal", "linkedin")) or _pick(("personal", "website"))
        if value:
            return value, "personal.profile_link"
    return None, None


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
    preferred_resume_path: str | None = None,
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

    # 2) Required/optional text field fill tasks (deterministic, profile-driven)
    for item in sorted(snapshot_map.values(), key=lambda x: x.ref):
        if item.role != "textbox":
            continue
        if _norm(item.value_hint):
            continue
        label = (item.name or "").strip()
        input_type = (item.input_type or "").strip().lower()
        if input_type == "file":
            continue
        if _contains_any(_norm(label), ["resume", "cv", "upload"]):
            continue
        is_required = bool(item.required) or _looks_required_label(label)
        value, reason = _resolve_text_field_mapping(
            profile=profile,
            field_label=label,
            input_type=input_type,
        )
        if not value:
            continue
        if not is_required and not _is_optional_prompt_label(label):
            continue
        task_type = "field_fill" if is_required else "field_fill_optional"
        title = (
            "Fill required field from profile"
            if is_required
            else "Fill optional prompt field from profile"
        )
        precondition = (
            "required_text_field_empty" if is_required else "optional_text_field_empty"
        )
        tasks.append(
            MacroTask(
                task_id=f"t{task_idx}",
                task_type=task_type,
                title=title,
                field_ref=item.ref,
                field_selector=label,
                target_value=value,
                mapping_reason=reason,
                precondition=precondition,
                postcondition="field_value_filled",
            )
        )
        task_idx += 1

    # 3) Upload handling policy:
    # - Resume/CV upload is always treated as mandatory.
    # - Required non-resume file upload => manual_required.
    # - Cover letter file upload: required => manual_required, optional => skip.
    has_replace_btn = any(
        item.role == "button" and "replace" in _norm(item.name)
        for item in snapshot_map.values()
    )
    preferred_resume = _preferred_resume_value(profile, preferred_resume_path)
    resume_task_added = False
    for item in sorted(snapshot_map.values(), key=lambda x: x.ref):
        role = (item.role or "").strip().lower()
        input_type = (item.input_type or "").strip().lower()
        is_file_like = role == "file_input" or input_type == "file"
        if not is_file_like:
            continue
        label = (item.name or "").strip()
        if not label:
            continue
        is_required = bool(item.required) or _looks_required_label(label)
        if _is_cover_letter_label(label):
            if is_required:
                tasks.append(
                    MacroTask(
                        task_id=f"t{task_idx}",
                        task_type="manual_required",
                        title="Required cover letter upload requires manual handling",
                        field_ref=item.ref,
                        field_selector=label,
                        mapping_reason="required_cover_letter_upload",
                        precondition="required_file_upload_present",
                        postcondition="manual_required",
                    )
                )
                task_idx += 1
            continue
        if _is_resume_upload_label(label):
            if has_replace_btn:
                continue
            if not resume_task_added:
                tasks.append(
                    MacroTask(
                        task_id=f"t{task_idx}",
                        task_type="file_upload",
                        title="Upload required resume",
                        field_ref=item.ref,
                        field_selector=label,
                        target_value=preferred_resume,
                        mapping_reason="required_resume_upload",
                        precondition="resume_upload_needed",
                        postcondition="file_uploaded",
                    )
                )
                task_idx += 1
                resume_task_added = True
            continue
        if is_required:
            tasks.append(
                MacroTask(
                    task_id=f"t{task_idx}",
                    task_type="manual_required",
                    title="Unsupported required file upload",
                    field_ref=item.ref,
                    field_selector=label,
                    mapping_reason="required_non_resume_upload",
                    precondition="required_file_upload_present",
                    postcondition="manual_required",
                )
            )
            task_idx += 1

    if not resume_task_added and not has_replace_btn:
        resume_buttons = [
            item
            for item in snapshot_map.values()
            if item.role == "button"
            and _is_upload_label(item.name)
            and (
                "resume" in _norm(item.name)
                or "upload file" in _norm(item.name)
                or "upload cv" in _norm(item.name)
            )
        ]
        if resume_buttons:
            target = sorted(resume_buttons, key=lambda x: x.ref)[0]
            tasks.append(
                MacroTask(
                    task_id=f"t{task_idx}",
                    task_type="file_upload",
                    title="Upload required resume",
                    field_ref=target.ref,
                    field_selector=target.name,
                    target_value=preferred_resume,
                    mapping_reason="required_resume_upload_button_fallback",
                    precondition="resume_upload_needed",
                    postcondition="file_uploaded",
                )
            )
            task_idx += 1

    # 4) Question tasks from semantic blocks (generalized option mapper)
    has_location_task = any(t.task_type == "combobox_select" for t in tasks)
    seen_question_signatures: set[str] = set()
    for qb in question_blocks:
        signature = _norm(qb.question_text)
        if not signature or signature in seen_question_signatures:
            continue
        seen_question_signatures.add(signature)
        if has_location_task and _is_location_question_label(qb.question_text):
            # Location 已由 combobox 任务覆盖，避免把下拉候选误当成独立问题。
            continue
        expected, reason = _resolve_question_mapping(qb=qb, profile=profile)
        if not expected:
            inferred_options = [opt.text for opt in qb.options if opt.text][:8]
            if len(inferred_options) < 2:
                continue
            mapping_reason = (
                "inference_required_unmapped"
                if qb.required
                else "inference_unmapped_question"
            )
            tasks.append(
                MacroTask(
                    task_id=f"t{task_idx}",
                    task_type="inference_required",
                    title="Infer answer for unmapped question",
                    question_text=qb.question_text,
                    expected_options=inferred_options,
                    mapping_reason=mapping_reason,
                    precondition="question_block_present",
                    postcondition=(
                        "required_question_answered"
                        if qb.required
                        else "question_answered"
                    ),
                    required=bool(qb.required),
                )
            )
            task_idx += 1
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
                required=bool(qb.required),
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
        elif task.task_type in ("field_fill", "field_fill_optional"):
            out.append(
                f"{task.task_id}:{status}: fill {task.field_selector or ''} -> {task.target_value or ''}"
            )
        elif task.task_type == "file_upload":
            out.append(
                f"{task.task_id}:{status}: upload {task.field_selector or 'resume'} -> {task.target_value or 'auto'}"
            )
        elif task.task_type == "manual_required":
            out.append(
                f"{task.task_id}:{status}: manual_required {task.field_selector or task.title}"
            )
        elif task.task_type == "inference_required":
            reason = f" [{task.mapping_reason}]" if task.mapping_reason else ""
            options = ", ".join(task.expected_options[:4]) if task.expected_options else "auto-infer"
            out.append(
                f"{task.task_id}:{status}: infer {task.question_text or task.title} -> {options}{reason}"
            )
        else:
            reason = f" [{task.mapping_reason}]" if task.mapping_reason else ""
            out.append(
                f"{task.task_id}:{status}: {task.question_text or task.title} -> {', '.join(task.expected_options[:4])}{reason}"
            )
    return out
