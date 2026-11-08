"""Shared policy helpers for durable fact storage and prompt eligibility."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


MANDATORY_TAG = "lumen:mandatory"
FACT_EMBEDDING_SOURCE_TYPE = "memory_fact"
FACT_EMBEDDING_SOURCE_PREFIX = "memory:fact:"

_FACT_FIELDS = ("category", "subject", "predicate", "value")


def _fact_value(fact: object, field: str, default: object = "") -> object:
    if isinstance(fact, dict):
        return fact.get(field, default)
    return getattr(fact, field, default)


def fact_embedding_text(fact: object) -> str:
    """Return the stable, tag-independent text represented by a fact."""
    return "\n".join(str(_fact_value(fact, field, "")).strip() for field in _FACT_FIELDS)


def fact_content_hash(fact: object) -> str:
    """Hash only the canonical fact content used for embedding."""
    return hashlib.sha256(fact_embedding_text(fact).encode("utf-8")).hexdigest()


def fact_revision(fact: object) -> str:
    """Identify one loaded fact version for optimistic administrative writes.

    Retrieval adds scores and diagnostics to fact dictionaries. Only persisted
    fields participate, so a preview snapshot and the live row share a revision.
    """
    fields = (
        "id", "user_id", "category", "subject", "predicate", "value",
        "confidence", "importance", "source_ref", "last_seen", "expires_at",
    )
    payload = {field: _fact_value(fact, field, None) for field in fields}
    payload["tags"] = sorted(str(tag) for tag in (_fact_value(fact, "tags", []) or []))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_mandatory_fact(fact: object) -> bool:
    """Return whether the fact has the exact reserved mandatory tag."""
    tags = _fact_value(fact, "tags", [])
    return isinstance(tags, list) and any(tag == MANDATORY_TAG for tag in tags)


def _parse_expiry(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_prompt_fact(
    fact: object,
    user_id: str | None = None,
    *,
    current_user_id: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Check prompt safety and optional user/global scope eligibility.

    A caller that supplies the current user gets the same scope rule as the
    retrieval layer: only that user and ``global`` facts are eligible.
    """
    if current_user_id is not None:
        if user_id is not None and user_id != current_user_id:
            return False
        user_id = current_user_id

    category = str(_fact_value(fact, "category", "")).strip().casefold()
    if category in {"secret", "credential"}:
        return False

    raw_expires_at = _fact_value(fact, "expires_at")
    expires_at = _parse_expiry(raw_expires_at)
    if raw_expires_at not in (None, "") and expires_at is None:
        return False
    if expires_at is not None:
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        reference = reference.astimezone(timezone.utc)
        if expires_at <= reference:
            return False

    if user_id is not None:
        fact_user_id = str(_fact_value(fact, "user_id", "")).strip()
        if fact_user_id not in {str(user_id).strip(), "global"}:
            return False
    return True
