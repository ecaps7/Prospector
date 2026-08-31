"""Stable, model-facing references for frozen research material."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

_KEY_ALIASES = {
    "task_id": "task_ref",
    "task_ids": "task_refs",
    "related_task_ids": "related_task_refs",
    "assertion_id": "assertion_ref",
    "assertion_ids": "assertion_refs",
    "related_assertion_ids": "related_assertion_refs",
    "supporting_assertion_ids": "supporting_assertion_refs",
    "winning_assertion_ids": "winning_assertion_refs",
    "effective_unusable_assertion_ids": "effective_unusable_assertion_refs",
    "excerpt_id": "excerpt_ref",
    "excerpt_ids": "excerpt_refs",
    "winning_excerpt_ids": "winning_excerpt_refs",
    "conflict_key": "conflict_ref",
    "material_conflict_keys": "material_conflict_refs",
    "known_conflict_keys": "known_conflict_refs",
}


def _unique(values: Iterable[UUID]) -> list[UUID]:
    return list(dict.fromkeys(values))


@dataclass(frozen=True, slots=True)
class ResearchModelRefs:
    """Translate storage UUIDs to compact references inside one frozen snapshot.

    Models reason in the local ``tN`` / ``aN`` / ``eN`` namespace. Persistence and
    domain validation continue to use UUIDs; the namespace is resolved exactly once at
    the model boundary.
    """

    task_by_ref: dict[str, UUID]
    ref_by_task: dict[UUID, str]
    assertion_by_ref: dict[str, UUID]
    ref_by_assertion: dict[UUID, str]
    excerpt_by_ref: dict[str, UUID]
    ref_by_excerpt: dict[UUID, str]
    conflict_by_ref: dict[str, str]
    ref_by_conflict: dict[str, str]

    @classmethod
    def build(
        cls,
        *,
        task_ids: Iterable[UUID] = (),
        assertion_ids: Iterable[UUID] = (),
        excerpt_ids: Iterable[UUID] = (),
        conflict_keys: Iterable[str] = (),
    ) -> ResearchModelRefs:
        def maps(values: Iterable[UUID], prefix: str) -> tuple[dict[str, UUID], dict[UUID, str]]:
            by_ref = {f"{prefix}{index}": value for index, value in enumerate(_unique(values), 1)}
            return by_ref, {value: ref for ref, value in by_ref.items()}

        task_by_ref, ref_by_task = maps(task_ids, "t")
        assertion_by_ref, ref_by_assertion = maps(assertion_ids, "a")
        excerpt_by_ref, ref_by_excerpt = maps(excerpt_ids, "e")
        conflicts = list(dict.fromkeys(conflict_keys))
        conflict_by_ref = {f"x{index}": value for index, value in enumerate(conflicts, 1)}
        return cls(
            task_by_ref=task_by_ref,
            ref_by_task=ref_by_task,
            assertion_by_ref=assertion_by_ref,
            ref_by_assertion=ref_by_assertion,
            excerpt_by_ref=excerpt_by_ref,
            ref_by_excerpt=ref_by_excerpt,
            conflict_by_ref=conflict_by_ref,
            ref_by_conflict={value: ref for ref, value in conflict_by_ref.items()},
        )

    @classmethod
    def from_verifier_snapshot(cls, snapshot: dict[str, Any]) -> ResearchModelRefs:
        def values(section: str, key: str) -> list[UUID]:
            return [
                UUID(str(row[key]))
                for row in snapshot.get(section) or []
                if isinstance(row, dict) and row.get(key) is not None
            ]

        return cls.build(
            task_ids=values("tasks", "task_id"),
            assertion_ids=values("assertions", "assertion_id"),
            excerpt_ids=values("excerpts", "excerpt_id"),
            conflict_keys=(
                str(row["conflict_key"])
                for row in snapshot.get("prior_conflict_resolutions") or []
                if isinstance(row, dict) and row.get("conflict_key") is not None
            ),
        )

    @classmethod
    def from_writer_snapshot(cls, snapshot: Any) -> ResearchModelRefs:
        cards = list(snapshot.evidence_cards)
        plan_task_ids = [
            UUID(str(task["id"]))
            for plan in snapshot.final_plan_summary
            for task in plan.get("tasks", [])
            if isinstance(task, dict) and task.get("id") is not None
        ]
        conflict_excerpt_ids = [
            UUID(str(value))
            for item in snapshot.conflicts
            if isinstance(item, dict)
            for key in ("excerpt_ids", "winning_excerpt_ids")
            for value in item.get(key) or []
        ]
        return cls.build(
            task_ids=(*plan_task_ids, *(card.task_id for card in cards)),
            assertion_ids=(card.assertion_id for card in cards),
            excerpt_ids=(
                *(excerpt.excerpt_id for card in cards for excerpt in card.excerpts),
                *conflict_excerpt_ids,
            ),
            conflict_keys=(
                str(item["conflict_key"])
                for item in snapshot.conflicts
                if isinstance(item, dict) and item.get("conflict_key") is not None
            ),
        )

    def alias_payload(self, value: Any) -> Any:
        """Replace frozen material identifiers throughout a model payload with local refs."""
        aliases = {
            **{str(item): ref for item, ref in self.ref_by_task.items()},
            **{str(item): ref for item, ref in self.ref_by_assertion.items()},
            **{str(item): ref for item, ref in self.ref_by_excerpt.items()},
            **self.ref_by_conflict,
        }
        if isinstance(value, UUID):
            return aliases.get(str(value), str(value))
        if isinstance(value, str):
            return aliases.get(value, value)
        if isinstance(value, list):
            return [self.alias_payload(item) for item in value]
        if isinstance(value, dict):
            return {
                _KEY_ALIASES.get(key, key): self.alias_payload(item) for key, item in value.items()
            }
        return value

    @staticmethod
    def _resolve(values: Sequence[str], known: dict[str, UUID], kind: str) -> list[UUID]:
        unknown = list(dict.fromkeys(value for value in values if value not in known))
        if unknown:
            raise ValueError(f"unknown {kind} refs: {', '.join(unknown)}")
        return [known[value] for value in values]

    def tasks(self, refs: Sequence[str]) -> list[UUID]:
        return self._resolve(refs, self.task_by_ref, "Task")

    def assertions(self, refs: Sequence[str]) -> list[UUID]:
        return self._resolve(refs, self.assertion_by_ref, "Assertion")

    def excerpts(self, refs: Sequence[str]) -> list[UUID]:
        return self._resolve(refs, self.excerpt_by_ref, "Excerpt")

    def conflicts(self, refs: Sequence[str]) -> list[str]:
        unknown = list(dict.fromkeys(value for value in refs if value not in self.conflict_by_ref))
        if unknown:
            raise ValueError(f"unknown Conflict refs: {', '.join(unknown)}")
        return [self.conflict_by_ref[value] for value in refs]
