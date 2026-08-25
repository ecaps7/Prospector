"""Stable HTTP error vocabulary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_LOCATION_PREFIXES = {"body", "query", "path", "header", "cookie"}
_VALUE_ERROR_PREFIX = "Value error, "


class ApiError(RuntimeError):
    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message


def validation_error_details(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Map request validation failures to field paths and displayable reasons."""
    return [
        {"path": _field_path(error.get("loc")), "reason": _field_reason(error)} for error in errors
    ]


def _field_path(loc: object) -> str:
    parts = [str(item) for item in loc] if isinstance(loc, (list, tuple)) else []
    if parts and parts[0] in _LOCATION_PREFIXES:
        parts = parts[1:]
    return ".".join(parts)


def _field_reason(error: Mapping[str, Any]) -> str:
    ctx = error.get("ctx")
    if isinstance(ctx, Mapping):
        raw = ctx.get("error")
        if isinstance(raw, BaseException):
            text = str(raw).strip()
            if text:
                return text
    msg = str(error.get("msg") or "").strip()
    if msg.startswith(_VALUE_ERROR_PREFIX):
        msg = msg[len(_VALUE_ERROR_PREFIX) :]
    return msg or "invalid"
