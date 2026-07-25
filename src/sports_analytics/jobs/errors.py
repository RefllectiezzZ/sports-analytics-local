"""Sanitize concise job and worker error text for durable storage."""

from __future__ import annotations

from sports_analytics.core.exceptions import RepositoryError
from sports_analytics.jobs.types import MAX_JOB_ERROR_LENGTH


def sanitize_error_text(
    exc: BaseException | str,
    *,
    maximum_length: int = MAX_JOB_ERROR_LENGTH,
) -> str:
    """Return a concise sanitized error string without traceback or payload data.

    Stores the exception class name and a truncated message. Does not include
    traceback text, local variables, payloads, or credentials.
    """
    if maximum_length < 32:
        msg = "maximum_length must be at least 32"
        raise RepositoryError(msg)
    if isinstance(exc, str):
        text = exc.strip()
        if not text:
            msg = "error text must be non-empty"
            raise RepositoryError(msg)
        if len(text) > maximum_length:
            return text[: maximum_length - 3] + "..."
        return text

    class_name = type(exc).__name__
    message = str(exc).strip().replace("\n", " ").replace("\r", " ")
    if message:
        combined = f"{class_name}: {message}"
    else:
        combined = class_name
    if len(combined) > maximum_length:
        return combined[: maximum_length - 3] + "..."
    return combined
