"""Apply Writer revision patches onto an existing ReportDraft without full rewrite."""

from __future__ import annotations

from prospector.schemas.report import ReportDraft, ReportParagraph, ReportStatement


class StatementPatchError(ValueError):
    """Raised when a patch violates the sentence-level revision contract."""


def preceding_statement_ids(draft: ReportDraft) -> dict[str, set[str]]:
    """For each statement, the ids a patch replacing it may legally cite as premises.

    A premise has to stand earlier in the report than the sentence resting on it. While
    streaming a first draft that is self-evident -- nothing later exists yet -- but revision
    hands the model the finished draft and asks it to rewrite sentences in place, so every
    sentence looks equally available. Computing the legal set here lets the patch assembler
    reject a backwards reference on the turn it arrives.
    """
    seen: set[str] = set()
    legal: dict[str, set[str]] = {}
    for statement in draft.statements():
        legal[statement.statement_id] = set(seen)
        seen.add(statement.statement_id)
    return legal


def apply_statement_patches(
    draft: ReportDraft,
    patches: list[ReportStatement],
    *,
    allowed_statement_ids: set[str] | None = None,
) -> ReportDraft:
    """Replace named statements in ``draft``; all other sentences stay byte-identical."""
    return apply_report_patches(
        draft,
        statement_patches=patches,
        paragraph_patches=[],
        allowed_statement_ids=allowed_statement_ids,
    )


def apply_report_patches(
    draft: ReportDraft,
    *,
    statement_patches: list[ReportStatement],
    paragraph_patches: list[ReportParagraph],
    allowed_statement_ids: set[str] | None = None,
    allowed_paragraph_ids: set[str] | None = None,
) -> ReportDraft:
    """Atomically apply the smallest allowed sentence/paragraph replacements."""
    if not statement_patches and not paragraph_patches:
        raise StatementPatchError("revision produced no patches")
    patch_map = {patch.statement_id: patch for patch in statement_patches}
    if len(patch_map) != len(statement_patches):
        raise StatementPatchError("duplicate statement_id in patches")
    paragraph_map = {patch.paragraph_id: patch for patch in paragraph_patches}
    if len(paragraph_map) != len(paragraph_patches):
        raise StatementPatchError("duplicate paragraph_id in patches")

    existing = {statement.statement_id for statement in draft.statements()}
    unknown = set(patch_map) - existing
    if unknown:
        raise StatementPatchError(
            "patches reference unknown statement_id values: " + ", ".join(sorted(unknown))
        )
    if allowed_statement_ids is not None:
        disallowed = set(patch_map) - allowed_statement_ids
        if disallowed:
            raise StatementPatchError(
                "patches touch statements not listed in findings: " + ", ".join(sorted(disallowed))
            )
    existing_paragraphs = {paragraph.paragraph_id for paragraph in draft.paragraphs()}
    unknown_paragraphs = set(paragraph_map) - existing_paragraphs
    if unknown_paragraphs:
        raise StatementPatchError(
            "patches reference unknown paragraph_id values: "
            + ", ".join(sorted(unknown_paragraphs))
        )
    if allowed_paragraph_ids is not None:
        disallowed_paragraphs = set(paragraph_map) - allowed_paragraph_ids
        if disallowed_paragraphs:
            raise StatementPatchError(
                "patches touch paragraphs not listed in findings: "
                + ", ".join(sorted(disallowed_paragraphs))
            )

    paragraph_by_statement = {
        statement.statement_id: paragraph.paragraph_id
        for paragraph in draft.paragraphs()
        for statement in paragraph.statements
    }
    overlap = {
        statement_id
        for statement_id in patch_map
        if paragraph_by_statement[statement_id] in paragraph_map
    }
    if overlap:
        raise StatementPatchError(
            "sentence and paragraph patches overlap: " + ", ".join(sorted(overlap))
        )

    def map_paragraph(paragraph: ReportParagraph) -> ReportParagraph:
        replacement = paragraph_map.get(paragraph.paragraph_id)
        if replacement is not None:
            return replacement
        return ReportParagraph(
            paragraph_id=paragraph.paragraph_id,
            statements=[
                patch_map.get(statement.statement_id, statement)
                for statement in paragraph.statements
            ],
        )

    updated = ReportDraft(
        title=draft.title,
        introduction=[map_paragraph(paragraph) for paragraph in draft.introduction],
        sections=[
            section.model_copy(
                update={
                    "paragraphs": [map_paragraph(paragraph) for paragraph in section.paragraphs]
                }
            )
            for section in draft.sections
        ],
        conclusion=[map_paragraph(paragraph) for paragraph in draft.conclusion],
    )
    return updated
