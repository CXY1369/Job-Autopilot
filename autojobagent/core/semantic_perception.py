"""
语义感知模块（V2 拆分第一步）

职责：
- 定义语义快照数据结构
- 从 UI 快照构建结构化语义快照
- 生成错误摘要片段（文本级）
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from .ui_snapshot import SnapshotItem


@dataclass
class SemanticElement:
    ref_id: str
    role: str
    name: str
    label: Optional[str] = None
    value: Optional[str] = None
    required: bool = False
    disabled: bool = False
    checked: Optional[bool] = None
    visible: bool = True
    group_signature: Optional[str] = None


@dataclass
class SemanticSnapshot:
    page_id: str
    url: str
    domain: str
    normalized_path: str
    title: str
    elements: list[SemanticElement]
    errors: list[str]
    required_unfilled: list[str]
    submit_candidates: list[str]


def extract_semantic_error_snippets(
    visible_text: str, last_progression_block_snippets: list[str] | None = None
) -> list[str]:
    snippets: list[str] = []
    keywords = (
        "required",
        "missing",
        "invalid",
        "error",
        "please complete",
        "please fill",
        "failed",
    )
    for raw in (visible_text or "").splitlines():
        line = " ".join(raw.split()).strip()
        if len(line) < 8 or len(line) > 200:
            continue
        lower = line.lower()
        if any(k in lower for k in keywords):
            snippets.append(line[:180])
        if len(snippets) >= 6:
            break
    if not snippets and last_progression_block_snippets:
        snippets = [str(s)[:180] for s in last_progression_block_snippets[:3]]
    return snippets


def build_semantic_snapshot(
    current_url: str,
    snapshot_map: dict[str, SnapshotItem],
    *,
    page_title: str = "",
    visible_text: str = "",
    last_progression_block_snippets: list[str] | None = None,
) -> SemanticSnapshot:
    parsed = urlsplit(current_url or "")
    domain = (parsed.netloc or "unknown").lower()
    path = (parsed.path or "/").lower()
    stable_parts = [p for p in path.split("/") if p and p not in {"jobs", "job"}]
    normalized_path = "/" + "/".join(stable_parts[:3]) if stable_parts else "/"

    elements: list[SemanticElement] = []
    required_unfilled: list[str] = []
    submit_candidates: list[str] = []
    sorted_items = sorted(snapshot_map.values(), key=lambda x: x.ref)[:160]
    for item in sorted_items:
        value = (item.value_hint or "").strip()
        elem = SemanticElement(
            ref_id=item.ref,
            role=item.role,
            name=(item.name or "")[:120],
            label=(item.name or "")[:120],
            value=value or None,
            required=bool(item.required),
            disabled=False,
            checked=item.checked,
            visible=True,
            group_signature=f"{item.role}:{(item.name or '')[:40].lower()}",
        )
        elements.append(elem)
        if (
            item.required
            and item.role in ("textbox", "combobox", "file_input")
            and not value
        ):
            required_unfilled.append(f"{item.role}:{(item.name or '')[:80]}")
        if item.role in ("button", "link"):
            label_lower = (item.name or "").lower()
            if any(
                kw in label_lower
                for kw in ("submit", "apply", "continue", "review", "next")
            ):
                submit_candidates.append(f"{item.ref}:{(item.name or '')[:80]}")

    errors = extract_semantic_error_snippets(
        visible_text,
        last_progression_block_snippets,
    )
    page_id_seed = json.dumps(
        {
            "url": (current_url or "").split("#")[0],
            "elements": [
                {"ref": e.ref_id, "role": e.role, "name": e.name[:48]}
                for e in elements[:24]
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    page_id = hashlib.sha1(page_id_seed.encode("utf-8")).hexdigest()
    return SemanticSnapshot(
        page_id=page_id,
        url=current_url or "",
        domain=domain,
        normalized_path=normalized_path,
        title=page_title or "",
        elements=elements,
        errors=errors,
        required_unfilled=required_unfilled[:12],
        submit_candidates=submit_candidates[:8],
    )
