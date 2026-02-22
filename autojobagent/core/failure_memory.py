"""
Failure Memory Store

Purpose:
- Persist failure cases as reusable memory, so similar future failures can be
  handled with proven strategies instead of ad-hoc retries.
- Keep storage append-safe and compact for local replay/regression usage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MEMORY_DIR = Path(__file__).parent.parent / "storage" / "memory"
MEMORY_FILE = MEMORY_DIR / "failure_memory.ndjson"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(value: str | None) -> str:
    text = " ".join(str(value or "").split()).strip().lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_scope(scope: str | None) -> str:
    raw = " ".join(str(scope or "").split()).strip().lower()
    if not raw:
        return ""
    if "|" in raw:
        domain, path = raw.split("|", 1)
    else:
        domain, path = raw, ""
    path = re.sub(r"/[0-9a-f]{8}-[0-9a-f-]{27,}", "/{id}", path)
    path = re.sub(r"/\d{3,}", "/{num}", path)
    path = re.sub(r"/+", "/", path).strip()
    return f"{domain}|{path}" if path else domain


def _token_set(text: str | None) -> set[str]:
    return {tok for tok in _normalize_text(text).split() if tok}


def _jaccard(a: str | None, b: str | None) -> float:
    sa = _token_set(a)
    sb = _token_set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    if union == 0:
        return 0.0
    return inter / union


def build_failure_signature(
    *,
    page_scope: str,
    classification: str,
    reason_code: str,
    question_text: str = "",
    action: str = "",
) -> str:
    scope_key = _normalize_scope(page_scope)
    class_key = _normalize_text(classification) or "unknown"
    reason_key = _normalize_text(reason_code) or "unspecified"
    question_key = _normalize_text(question_text)[:120]
    action_key = _normalize_text(action)[:40]
    return "||".join([scope_key, class_key, reason_key, question_key, action_key])


@dataclass
class FailureMemoryEntry:
    signature: str
    page_scope: str
    classification: str
    reason_code: str
    symptom: str
    root_cause: str
    successful_strategy: str
    guardrails: str
    evidence_snippet: str
    question_text: str
    action: str
    selector: str
    source_event: str
    status: str
    first_seen_at: str
    last_seen_at: str
    hit_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "page_scope": self.page_scope,
            "classification": self.classification,
            "reason_code": self.reason_code,
            "symptom": self.symptom,
            "root_cause": self.root_cause,
            "successful_strategy": self.successful_strategy,
            "guardrails": self.guardrails,
            "evidence_snippet": self.evidence_snippet,
            "question_text": self.question_text,
            "action": self.action,
            "selector": self.selector,
            "source_event": self.source_event,
            "status": self.status,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "hit_count": self.hit_count,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "FailureMemoryEntry":
        return FailureMemoryEntry(
            signature=str(data.get("signature") or ""),
            page_scope=str(data.get("page_scope") or ""),
            classification=str(data.get("classification") or "unknown"),
            reason_code=str(data.get("reason_code") or "unspecified"),
            symptom=str(data.get("symptom") or ""),
            root_cause=str(data.get("root_cause") or ""),
            successful_strategy=str(data.get("successful_strategy") or ""),
            guardrails=str(data.get("guardrails") or ""),
            evidence_snippet=str(data.get("evidence_snippet") or ""),
            question_text=str(data.get("question_text") or ""),
            action=str(data.get("action") or ""),
            selector=str(data.get("selector") or ""),
            source_event=str(data.get("source_event") or ""),
            status=str(data.get("status") or "active"),
            first_seen_at=str(data.get("first_seen_at") or _now_iso()),
            last_seen_at=str(data.get("last_seen_at") or _now_iso()),
            hit_count=int(data.get("hit_count") or 1),
        )


class FailureMemoryStore:
    def __init__(self, path: Path | None = None, *, max_entries: int = 2000):
        self.path = path or MEMORY_FILE
        self.max_entries = max_entries

    def _load(self) -> list[FailureMemoryEntry]:
        if not self.path.exists():
            return []
        entries: list[FailureMemoryEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except Exception:
                continue
            if not isinstance(raw, dict):
                continue
            entry = FailureMemoryEntry.from_dict(raw)
            if not entry.signature:
                continue
            entries.append(entry)
        return entries

    def _save(self, entries: list[FailureMemoryEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(item.to_dict(), ensure_ascii=False) for item in entries]
        content = "\n".join(lines).strip()
        if content:
            content += "\n"
        self.path.write_text(content, encoding="utf-8")

    def upsert_case(
        self,
        *,
        page_scope: str,
        classification: str,
        reason_code: str,
        symptom: str,
        root_cause: str,
        successful_strategy: str,
        guardrails: str,
        evidence_snippet: str = "",
        question_text: str = "",
        action: str = "",
        selector: str = "",
        source_event: str = "",
        status: str = "active",
    ) -> FailureMemoryEntry:
        signature = build_failure_signature(
            page_scope=page_scope,
            classification=classification,
            reason_code=reason_code,
            question_text=question_text,
            action=action,
        )
        now = _now_iso()
        entries = self._load()
        by_signature = {item.signature: item for item in entries}
        existing = by_signature.get(signature)
        if existing is None:
            entry = FailureMemoryEntry(
                signature=signature,
                page_scope=page_scope,
                classification=classification or "unknown",
                reason_code=reason_code or "unspecified",
                symptom=symptom[:240],
                root_cause=root_cause[:240],
                successful_strategy=successful_strategy[:320],
                guardrails=guardrails[:240],
                evidence_snippet=evidence_snippet[:320],
                question_text=question_text[:220],
                action=action[:80],
                selector=selector[:120],
                source_event=source_event[:80],
                status=status or "active",
                first_seen_at=now,
                last_seen_at=now,
                hit_count=1,
            )
            entries.append(entry)
        else:
            existing.hit_count += 1
            existing.last_seen_at = now
            existing.page_scope = page_scope or existing.page_scope
            existing.classification = classification or existing.classification
            existing.reason_code = reason_code or existing.reason_code
            if symptom:
                existing.symptom = symptom[:240]
            if root_cause:
                existing.root_cause = root_cause[:240]
            if successful_strategy:
                existing.successful_strategy = successful_strategy[:320]
            if guardrails:
                existing.guardrails = guardrails[:240]
            if evidence_snippet:
                existing.evidence_snippet = evidence_snippet[:320]
            if question_text:
                existing.question_text = question_text[:220]
            if action:
                existing.action = action[:80]
            if selector:
                existing.selector = selector[:120]
            if source_event:
                existing.source_event = source_event[:80]
            if status:
                existing.status = status
            entry = existing

        entries = sorted(
            entries,
            key=lambda x: (x.last_seen_at, x.hit_count),
            reverse=True,
        )[: self.max_entries]
        self._save(entries)
        return entry

    def query_similar(
        self,
        *,
        page_scope: str,
        classification: str = "",
        reason_code: str = "",
        question_text: str = "",
        action: str = "",
        limit: int = 3,
    ) -> list[FailureMemoryEntry]:
        target_scope = _normalize_scope(page_scope)
        target_domain = target_scope.split("|", 1)[0]
        target_class = _normalize_text(classification)
        target_reason = _normalize_text(reason_code)
        target_question = _normalize_text(question_text)
        target_action = _normalize_text(action)

        ranked: list[tuple[float, FailureMemoryEntry]] = []
        for entry in self._load():
            if entry.status != "active":
                continue
            score = 0.0
            entry_scope = _normalize_scope(entry.page_scope)
            entry_domain = entry_scope.split("|", 1)[0]
            if target_domain and entry_domain == target_domain:
                score += 0.18
            if target_scope and entry_scope == target_scope:
                score += 0.18
            if target_class and _normalize_text(entry.classification) == target_class:
                score += 0.24
            if target_reason and _normalize_text(entry.reason_code) == target_reason:
                score += 0.24
            q_sim = _jaccard(target_question, entry.question_text)
            if q_sim > 0:
                score += min(0.2, q_sim * 0.2)
            if target_action and _normalize_text(entry.action) == target_action:
                score += 0.08
            score += min(0.18, max(0, entry.hit_count - 1) * 0.02)
            if score >= 0.26:
                ranked.append((score, entry))

        ranked.sort(key=lambda item: (item[0], item[1].hit_count), reverse=True)
        return [entry for _score, entry in ranked[:limit]]

    def format_hints_for_prompt(
        self, entries: list[FailureMemoryEntry], *, max_items: int = 3
    ) -> str:
        if not entries:
            return "无"
        lines: list[str] = []
        for idx, item in enumerate(entries[:max_items], start=1):
            lines.append(
                f"{idx}. 症状: {item.symptom or item.reason_code}; "
                f"策略: {item.successful_strategy or '先重采样再改策略'}; "
                f"护栏: {item.guardrails or '禁止无限重复同一动作'}"
            )
        return "\n".join(lines)
