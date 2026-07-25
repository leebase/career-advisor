"""Domain-agnostic profile schema loading and fact coverage.

Any schema file with shape ``domain / version / sections → facts`` can be
loaded. The Interview Engine does not hardcode "candidate" — it operates on
slots discovered from the schema (Domain Profiles pattern).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

_SCHEMAS_DIR = Path(__file__).parent / "schemas"

# Heuristic: weak facts lack numbers or concrete evidence language.
_METRIC_HINTS = (
    "%",
    "percent",
    "users",
    "tickets",
    "servers",
    "endpoints",
    "seats",
    "sites",
    "hours",
    "days",
    "weeks",
    "months",
    "years",
    "reduced",
    "increased",
    "saved",
    "from ",
    " to ",
    "approx",
    "about ",
    "roughly ",
)

# A fact the model is this sure of, and can point at evidence for, counts as
# filled even with no number in it. Without this, a 0.93-confidence evidenced
# answer stayed "weak" purely for lacking a digit, so the engine dug at the
# same slot forever (see feedback.md 2026-07-24).
CONFIDENCE_SATISFIES = 0.75

# Sections where an unquantified answer is still worth one more dig. Add a
# domain of your own? Prefer making this schema-driven over extending the list.
_SCALE_HEAVY_SECTIONS = (
    "achievements",
    "employment_history",
    "skills_and_stack",
)


@dataclass(frozen=True)
class FactSlot:
    key: str  # section_id.fact_id
    section_id: str
    fact_id: str
    description: str
    evidence: str
    priority: str  # required | preferred | optional
    section_title: str
    section_order: int


@dataclass
class DomainSchema:
    domain: str
    version: int
    title: str
    slots: list[FactSlot] = field(default_factory=list)

    def slot_map(self) -> dict[str, FactSlot]:
        return {s.key: s for s in self.slots}

    def sections_in_order(self) -> list[tuple[str, str, list[FactSlot]]]:
        """Return (section_id, section_title, slots) ordered by section order."""
        by_section: dict[str, list[FactSlot]] = {}
        titles: dict[str, str] = {}
        orders: dict[str, int] = {}
        for slot in self.slots:
            by_section.setdefault(slot.section_id, []).append(slot)
            titles[slot.section_id] = slot.section_title
            orders[slot.section_id] = slot.section_order
        ordered = sorted(by_section.keys(), key=lambda sid: orders[sid])
        return [(sid, titles[sid], by_section[sid]) for sid in ordered]


def load_schema(domain: str = "candidate", path: Path | None = None) -> DomainSchema:
    """Load a domain schema YAML. Default: package schemas/<domain>.yaml."""
    schema_path = path or (_SCHEMAS_DIR / f"{domain}.yaml")
    raw = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "sections" not in raw:
        raise ValueError(f"Invalid domain schema at {schema_path}: missing sections")
    slots: list[FactSlot] = []
    sections = raw.get("sections") or {}
    for section_id, section in sections.items():
        if not isinstance(section, dict):
            continue
        section_title = str(section.get("title") or section_id)
        section_order = int(section.get("order") or 99)
        facts = section.get("facts") or {}
        for fact_id, fact in facts.items():
            if not isinstance(fact, dict):
                continue
            slots.append(
                FactSlot(
                    key=f"{section_id}.{fact_id}",
                    section_id=section_id,
                    fact_id=fact_id,
                    description=str(fact.get("description") or ""),
                    evidence=str(fact.get("evidence") or ""),
                    priority=str(fact.get("priority") or "preferred"),
                    section_title=section_title,
                    section_order=section_order,
                )
            )
    slots.sort(key=lambda s: (s.section_order, s.fact_id))
    return DomainSchema(
        domain=str(raw.get("domain") or domain),
        version=int(raw.get("version") or 1),
        title=str(raw.get("title") or domain),
        slots=slots,
    )


def _has_metric_signal(text: str) -> bool:
    lower = text.lower()
    if any(ch.isdigit() for ch in text):
        return True
    return any(hint in lower for hint in _METRIC_HINTS)


def classify_fact(
    slot: FactSlot,
    value: str | None,
    evidence: str | None,
    confidence: float | None = None,
) -> str:
    """Return coverage status: empty | weak | filled | contradicted.

    ``contradicted`` is set by the engine when extraction flags it; this
    helper only scores empty/weak/filled from stored value+evidence.
    """
    if not value or not str(value).strip():
        return "empty"
    evidence_text = (evidence or "").strip()
    value_text = str(value).strip()
    combined = f"{value_text} {evidence_text}"
    if confidence is not None and confidence < 0.4:
        return "weak"
    if (
        confidence is not None
        and confidence >= CONFIDENCE_SATISFIES
        and evidence_text
    ):
        return "filled"
    # Required/preferred slots without evidence or metrics are weak.
    if slot.priority in ("required", "preferred"):
        if not evidence_text and not _has_metric_signal(value_text):
            return "weak"
        if evidence_text and not _has_metric_signal(combined):
            # Evidence present but no numbers — still weak for digging doctrine
            # on achievement/scale-heavy sections.
            if slot.section_id in _SCALE_HEAVY_SECTIONS:
                return "weak"
    return "filled"


@dataclass
class CoverageReport:
    empty: list[str]
    weak: list[str]
    filled: list[str]
    contradicted: list[str]
    # Dug at the cap already: ``accepted`` kept a qualitative answer,
    # ``skipped`` got nothing. Neither is re-asked.
    accepted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def open_gaps(self) -> list[str]:
        return self.empty + self.weak + self.contradicted

    def required_open(self, schema: DomainSchema) -> list[str]:
        gaps = set(self.open_gaps)
        return [
            s.key
            for s in schema.slots
            if s.priority == "required" and s.key in gaps
        ]

    def ready_for_documents(self, schema: DomainSchema) -> bool:
        """Every required slot is either filled or settled — enough to write."""
        return not self.required_open(schema)

    def section_progress(self, schema: DomainSchema) -> list[dict[str, Any]]:
        """Per-section captured/total for UI progress bars."""
        status_by_key = {
            **{k: "empty" for k in self.empty},
            **{k: "weak" for k in self.weak},
            **{k: "filled" for k in self.filled},
            **{k: "contradicted" for k in self.contradicted},
            **{k: "accepted" for k in self.accepted},
            **{k: "skipped" for k in self.skipped},
        }
        rows = []
        for section_id, title, slots in schema.sections_in_order():
            total = len(slots)
            # ``accepted`` counts: the candidate answered, we simply stopped
            # demanding a number for it. Showing that as 0 progress is what
            # made the bar sit still while they typed paragraphs.
            done = sum(
                1
                for s in slots
                if status_by_key.get(s.key) in ("filled", "accepted")
            )
            rows.append(
                {
                    "section_id": section_id,
                    "title": title,
                    "filled": done,
                    "total": total,
                    "pct": int(100 * done / total) if total else 0,
                }
            )
        return rows


def coverage(
    schema: DomainSchema,
    facts: Iterable[dict[str, Any]],
    contradicted_keys: Iterable[str] | None = None,
    exhausted_keys: Iterable[str] | None = None,
) -> CoverageReport:
    """Compute slot coverage from stored profile facts.

    ``facts`` items: dicts with keys fact_key, value, evidence, confidence
    (and optionally status == 'contradicted').

    ``exhausted_keys`` are slots already dug at the per-slot cap. They move
    to ``accepted``/``skipped`` and leave ``open_gaps`` so the engine stops
    asking about them.
    """
    contradicted_set = set(contradicted_keys or [])
    exhausted_set = set(exhausted_keys or [])
    by_key: dict[str, dict[str, Any]] = {}
    for row in facts:
        key = row["fact_key"] if "fact_key" in row else row.get("key")
        if key is None:
            continue
        by_key[str(key)] = row
        if row.get("status") == "contradicted":
            contradicted_set.add(str(key))

    empty: list[str] = []
    weak: list[str] = []
    filled: list[str] = []
    contradicted: list[str] = []
    accepted: list[str] = []
    skipped: list[str] = []

    for slot in schema.slots:
        exhausted = slot.key in exhausted_set
        if slot.key in contradicted_set:
            # A contradiction is a correction to resolve, not a gap to grind
            # on: honour the cap here too.
            (accepted if exhausted else contradicted).append(slot.key)
            continue
        row = by_key.get(slot.key)
        if row is None:
            (skipped if exhausted else empty).append(slot.key)
            continue
        value = row.get("value")
        evidence = row.get("evidence")
        conf = row.get("confidence")
        conf_f = float(conf) if conf is not None else None
        status = classify_fact(slot, value, evidence, conf_f)
        if status == "filled":
            filled.append(slot.key)
        elif exhausted:
            # Answered, just not quantifiably. Keep it and move on.
            accepted.append(slot.key)
        elif status == "empty":
            empty.append(slot.key)
        else:
            weak.append(slot.key)

    return CoverageReport(
        empty=empty,
        weak=weak,
        filled=filled,
        contradicted=contradicted,
        accepted=accepted,
        skipped=skipped,
    )


def is_degradation(
    existing: dict[str, Any] | Any,
    incoming: dict[str, Any],
) -> tuple[bool, str]:
    """Would writing ``incoming`` over ``existing`` lose information?

    Facts are stored one row per slot, so a late tired answer silently
    replaces a rich early one. This is deliberately conservative — it only
    blocks clear regressions, because a genuine correction must always win.
    Returns (blocked, reason).
    """
    if existing is None:
        return False, ""
    if incoming.get("status") == "contradicted":
        # An explicit correction always wins, even if it is shorter.
        return False, ""

    def _parts(row: Any) -> tuple[str, str, float]:
        # sqlite3.Row supports keys() but ``in`` tests values, not keys.
        data = (
            {k: row[k] for k in row.keys()} if hasattr(row, "keys") else dict(row)
        )
        try:
            conf = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        return (
            str(data.get("value") or ""),
            str(data.get("evidence") or ""),
            conf,
        )

    ex_value, ex_evidence, ex_conf = _parts(existing)
    in_value, in_evidence, in_conf = _parts(incoming)

    ex_metric = _has_metric_signal(f"{ex_value} {ex_evidence}")
    in_metric = _has_metric_signal(f"{in_value} {in_evidence}")
    if in_metric and not ex_metric:
        return False, ""
    if ex_metric and not in_metric:
        return True, "kept the earlier answer, which had the numbers"

    # Same metric footing: only block a clearly thinner, less certain answer.
    if in_conf < ex_conf - 0.05 and len(in_value + in_evidence) < len(
        ex_value + ex_evidence
    ):
        return True, "kept the earlier, more detailed answer"
    return False, ""


def facts_as_prompt_block(schema: DomainSchema, facts: list[dict[str, Any]]) -> str:
    """Render collected facts for LLM prompts."""
    by_key = {str(r.get("fact_key") or r.get("key")): r for r in facts}
    lines: list[str] = []
    for slot in schema.slots:
        row = by_key.get(slot.key)
        if not row or not row.get("value"):
            continue
        lines.append(
            f"- {slot.key}: {row.get('value')}"
            f" | evidence: {row.get('evidence') or '(none)'}"
            f" | confidence: {row.get('confidence')}"
        )
    return "\n".join(lines) if lines else "(none yet)"


def schema_as_prompt_block(schema: DomainSchema) -> str:
    lines = [f"Domain: {schema.domain} v{schema.version} — {schema.title}", ""]
    for section_id, title, slots in schema.sections_in_order():
        lines.append(f"## {title} ({section_id})")
        for slot in slots:
            lines.append(
                f"- {slot.key} [{slot.priority}]: {slot.description}"
                f" | evidence needed: {slot.evidence}"
            )
        lines.append("")
    return "\n".join(lines)
