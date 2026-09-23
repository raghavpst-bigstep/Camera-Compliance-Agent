"""Employee directory, backed by a Google Sheet.

This is the agent's only tool. It is read-only: it resolves a free-form hint
from a manager's email ("Priya from design", "p.sharma@") into candidate
directory records and lets the model decide whether the match is unambiguous.

The sheet is expected to have a header row. Recognised column names (case and
punctuation insensitive) are listed in _COLUMN_ALIASES below.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import threading
import time
from typing import Any

import google.auth
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "employee_name": ("employee name", "employee", "name", "full name"),
    "employee_email": ("employee email", "email", "work email", "email address"),
    "manager_name": ("manager name", "manager", "reporting manager"),
    "manager_email": ("manager email", "manager e-mail"),
    "hr_name": ("hr name", "hr", "hr contact", "hr representative"),
    "hr_email": ("hr email", "hr contact email"),
    "department": ("department", "team", "dept", "function"),
}

_MAX_CANDIDATES = 5


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9@. ]+", " ", value.strip().lower()).strip()


def _canonical_column(header: str) -> str | None:
    key = re.sub(r"[^a-z ]+", " ", header.strip().lower()).strip()
    key = re.sub(r"\s+", " ", key)
    for canonical, aliases in _COLUMN_ALIASES.items():
        if key == canonical.replace("_", " ") or key in aliases:
            return canonical
    return None


class DirectoryCache:
    """Reads the employee sheet once and re-reads it after a TTL."""

    def __init__(
        self,
        spreadsheet_id: str,
        sheet_range: str,
        ttl_seconds: int = 300,
    ) -> None:
        self._spreadsheet_id = spreadsheet_id
        self._range = sheet_range
        self._ttl = ttl_seconds
        self._rows: list[dict[str, str]] = []
        self._loaded_at = 0.0
        self._lock = threading.Lock()
        self._service = None

    def _sheets(self):
        if self._service is None:
            credentials, _ = google.auth.default(scopes=[SHEETS_SCOPE])
            self._service = build(
                "sheets", "v4", credentials=credentials, cache_discovery=False
            )
        return self._service

    def _fetch(self) -> list[dict[str, str]]:
        result = (
            self._sheets()
            .spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=self._range)
            .execute()
        )
        values = result.get("values", [])
        if not values:
            log.warning("employee sheet %s returned no rows", self._spreadsheet_id)
            return []

        header, *body = values
        columns = [_canonical_column(cell) for cell in header]
        if "employee_name" not in columns and "employee_email" not in columns:
            raise RuntimeError(
                "Employee sheet header has neither an employee name nor an "
                f"employee email column. Parsed headers: {header}"
            )

        rows: list[dict[str, str]] = []
        for raw in body:
            row: dict[str, str] = {}
            for index, column in enumerate(columns):
                if column is None or index >= len(raw):
                    continue
                value = str(raw[index]).strip()
                if value:
                    row[column] = value
            if row.get("employee_name") or row.get("employee_email"):
                rows.append(row)
        return rows

    def rows(self) -> list[dict[str, str]]:
        with self._lock:
            if not self._rows or (time.time() - self._loaded_at) > self._ttl:
                self._rows = self._fetch()
                self._loaded_at = time.time()
                log.info("loaded %d directory rows", len(self._rows))
            return self._rows


_cache: DirectoryCache | None = None


def _get_cache() -> DirectoryCache:
    global _cache
    if _cache is None:
        spreadsheet_id = os.environ["EMPLOYEE_SHEET_ID"]
        sheet_range = os.environ.get("EMPLOYEE_SHEET_RANGE", "Employees!A:Z")
        ttl = int(os.environ.get("DIRECTORY_CACHE_TTL_SECONDS", "300"))
        _cache = DirectoryCache(spreadsheet_id, sheet_range, ttl)
    return _cache


def _score(hint: str, row: dict[str, str]) -> tuple[float, str]:
    """Return (confidence 0-1, how it matched) for one directory row."""
    hint_norm = _normalise(hint)
    if not hint_norm:
        return 0.0, "none"

    name = _normalise(row.get("employee_name", ""))
    email = _normalise(row.get("employee_email", ""))
    local_part = email.split("@", 1)[0] if email else ""

    if email and hint_norm == email:
        return 1.0, "exact_email"
    if name and hint_norm == name:
        return 1.0, "exact_name"
    if email and email in hint_norm:
        return 0.95, "email_in_hint"
    if local_part and hint_norm == local_part:
        return 0.9, "email_local_part"
    if name and name in hint_norm:
        return 0.85, "name_in_hint"

    hint_tokens = {t for t in hint_norm.split() if len(t) > 1}
    name_tokens = {t for t in name.split() if len(t) > 1}
    if name_tokens and name_tokens <= hint_tokens:
        return 0.8, "all_name_tokens_present"

    overlap = hint_tokens & name_tokens
    if overlap:
        ratio = len(overlap) / len(name_tokens)
        return min(0.75, 0.4 + 0.35 * ratio), "partial_name_tokens"

    if name:
        fuzzy = difflib.SequenceMatcher(None, hint_norm, name).ratio()
        if fuzzy >= 0.7:
            return fuzzy * 0.7, "fuzzy_name"

    return 0.0, "none"


def lookup_directory(hint: str) -> dict[str, Any]:
    """Look up employee, manager, and HR records matching a free-form hint.

    Use this to verify every person named in a manager's report. The directory
    is the source of truth: prefer its values over anything written in the
    email. Call it again with a different hint (a surname, an email address, a
    team name) if the first call is ambiguous or empty.

    Args:
        hint: A name, partial name, email address, or team from the report.

    Returns:
        A dict with:
          match_count: how many candidate rows scored above the threshold
          candidates: up to five records, best first, each carrying
            employee_name, employee_email, manager_name, manager_email,
            hr_name, hr_email, department, confidence, matched_on
          note: guidance when the result is empty or ambiguous
    """
    try:
        rows = _get_cache().rows()
    except Exception as exc:  # surfaced to the model, not raised
        log.exception("directory lookup failed")
        return {
            "match_count": 0,
            "candidates": [],
            "note": f"The directory could not be read: {exc}",
        }

    scored: list[tuple[float, str, dict[str, str]]] = []
    for row in rows:
        confidence, matched_on = _score(hint, row)
        if confidence >= 0.4:
            scored.append((confidence, matched_on, row))

    scored.sort(key=lambda item: item[0], reverse=True)
    top = scored[:_MAX_CANDIDATES]

    candidates = [
        {
            "employee_name": row.get("employee_name", ""),
            "employee_email": row.get("employee_email", ""),
            "manager_name": row.get("manager_name", ""),
            "manager_email": row.get("manager_email", ""),
            "hr_name": row.get("hr_name", ""),
            "hr_email": row.get("hr_email", ""),
            "department": row.get("department", ""),
            "confidence": round(confidence, 2),
            "matched_on": matched_on,
        }
        for confidence, matched_on, row in top
    ]

    if not candidates:
        note = (
            "No directory record matched this hint. Try a surname, an email "
            "address, or a different spelling. If nothing matches, the "
            "employee cannot be verified."
        )
    elif len(candidates) > 1 and candidates[0]["confidence"] - candidates[1]["confidence"] < 0.15:
        note = (
            "Several records matched with similar confidence. This is "
            "ambiguous: do not pick one. Ask for clarification unless another "
            "lookup resolves it."
        )
    else:
        note = "Best match first. Use its values verbatim."

    return {
        "match_count": len(scored),
        "candidates": candidates,
        "note": note,
    }
