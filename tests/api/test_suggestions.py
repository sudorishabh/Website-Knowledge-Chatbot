"""The widget's suggested questions come from a hand-kept JSON file.

An editor adds questions to the file and expects the widget to show them, so
the endpoint must pick an edit up without a restart, serve a question typed in
twice only once, and degrade to an empty list — never an error — when the file
is missing or broken.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import suggestions
from app.config import get_settings


@pytest.fixture
def path(tmp_path, monkeypatch):
    target = tmp_path / "queries_suggestions.json"
    monkeypatch.setattr(get_settings(), "suggestions_path", str(target))
    suggestions._load.cache_clear()
    yield target
    suggestions._load.cache_clear()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(suggestions.router)
    return TestClient(app)


def _write(path, payload, *, mtime_ns=None, encoding="utf-8"):
    path.write_text(json.dumps(payload), encoding=encoding)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))


def test_serves_the_file_in_order(path, client):
    _write(path, ["What is TERI?", "Where are TERI's offices?"])
    response = client.get("/suggestions")
    assert response.status_code == 200
    assert response.json() == {"suggestions": ["What is TERI?", "Where are TERI's offices?"]}
    assert response.headers["cache-control"] == "max-age=300"


def test_a_repeated_question_is_served_once():
    raw = [
        "What are TERI's latest research priorities?",
        "What are TERI's ongoing projects?",
        "  what are TERI's   latest research priorities? ",
    ]
    assert suggestions.clean(raw) == [
        "What are TERI's latest research priorities?",
        "What are TERI's ongoing projects?",
    ]


def test_blanks_and_non_strings_are_dropped():
    assert suggestions.clean(["", "   ", 3, None, {"q": "x"}, "Real question"]) == [
        "Real question"
    ]


def test_an_edit_shows_without_a_restart(path, client):
    _write(path, ["First version"], mtime_ns=1_000_000_000)
    assert client.get("/suggestions").json()["suggestions"] == ["First version"]
    _write(path, ["Second version"], mtime_ns=2_000_000_000)
    assert client.get("/suggestions").json()["suggestions"] == ["Second version"]


def test_a_byte_order_mark_is_tolerated(path, client):
    _write(path, ["Saved by Notepad"], encoding="utf-8-sig")
    assert client.get("/suggestions").json()["suggestions"] == ["Saved by Notepad"]


@pytest.mark.parametrize(
    "contents",
    [None, "not json at all", json.dumps({"questions": ["Not a list"]})],
    ids=["missing", "malformed", "not-an-array"],
)
def test_a_missing_or_broken_file_is_an_empty_list(path, client, contents):
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    response = client.get("/suggestions")
    assert response.status_code == 200
    assert response.json() == {"suggestions": []}


def test_the_shipped_file_is_a_list_of_questions():
    if not suggestions.DEFAULT_PATH.exists():
        pytest.skip("no shipped suggestions file in this checkout")
    raw = json.loads(suggestions.DEFAULT_PATH.read_text(encoding="utf-8-sig"))
    assert isinstance(raw, list)
    assert suggestions.clean(raw), "the shipped file holds no usable question"
