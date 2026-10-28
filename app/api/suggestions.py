"""Suggested questions for the chat widget's start screen.

The list is a JSON array of question strings in ``data/queries_suggestions.json``
(or ``suggestions_path``), kept by hand. It is re-read whenever the file
changes, so an edit reaches the widget without a restart, and cleaned on the way
out: non-strings and blanks dropped, whitespace collapsed, and a question that
appears twice — in any case or spacing — served once. Which few to show, and in
what order, is the widget's choice.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, Response

from app.api.auth import Principal, require_principal
from app.config import get_settings
from app.schemas.query import SuggestionsResponse

router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)

#: app/api/suggestions.py -> the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = _REPO_ROOT / "data" / "queries_suggestions.json"

#: Seconds a browser may reuse the list before asking again.
_MAX_AGE = 300


def configured_path() -> Path:
    configured = (getattr(get_settings(), "suggestions_path", "") or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_PATH


def clean(raw: list) -> list[str]:
    """The questions in file order, each once."""
    seen: set[str] = set()
    questions: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            questions.append(text)
    return questions


@lru_cache(maxsize=4)
def _load(path: Path, mtime_ns: int | None) -> tuple[str, ...]:
    """Parsed once per version of the file: its modification time is part of the
    key, so an edit is a cache miss. A missing or malformed file is an empty
    list and one log line per version, not one per request."""
    if mtime_ns is None:
        logger.warning("No suggested questions at %s; the widget shows none.", path)
        return ()
    try:
        # utf-8-sig: a file saved by a Windows editor can open with a BOM.
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        logger.error("Could not read the suggested questions at %s; the widget "
                     "shows none until it is fixed.", path, exc_info=True)
        return ()
    if not isinstance(raw, list):
        logger.error("The suggested questions at %s are not a JSON array; the "
                     "widget shows none until it is fixed.", path)
        return ()
    return tuple(clean(raw))


def load_suggestions() -> list[str]:
    path = configured_path()
    try:
        mtime_ns: int | None = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = None
    return list(_load(path, mtime_ns))


@router.get("/suggestions", response_model=SuggestionsResponse)
def suggestions(
    response: Response,
    # The same gate as /chat (see app/api/chat.py), so the widget reaches both
    # or neither.
    _principal: Principal = Depends(require_principal),
) -> SuggestionsResponse:
    response.headers["Cache-Control"] = f"max-age={_MAX_AGE}"
    return SuggestionsResponse(suggestions=load_suggestions())
