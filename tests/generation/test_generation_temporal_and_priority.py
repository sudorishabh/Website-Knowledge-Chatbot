"""Unit tests for the generation-prompt additions: the per-request "today"
anchor, source-priority guidance, and the extended refusal rules.

No LLM, no network — these check the composed prompt strings and the plumbing
that gets them into a call, the same style as ``tests/test_shared_prompt.py``.
"""
from __future__ import annotations

import re

from app.generation import answerer, prompts


# --------------------------------------------------------------------------- #
# 1. Today's date anchor (Phase 7 — Q002's stale present tense).
# --------------------------------------------------------------------------- #

def test_today_anchor_states_a_real_iso_date():
    from app.core.dates import today_utc

    anchor = prompts.today_anchor()
    assert today_utc().isoformat() in anchor


def test_today_anchor_points_back_at_rule_9():
    assert "rule 9" in prompts.today_anchor()


def test_the_anchor_is_computed_fresh_each_call_not_baked_into_the_constant():
    """`GROUNDED_SYSTEM_PROMPT` is built once at import for prompt-caching — the
    date must never be part of it, or a long-running process would answer
    against the date it started at. (The prompt does contain illustrative dates
    inside its worked examples, which is fine — the anchor's own heading is what
    must be absent.)"""
    assert "## Today's date" not in prompts.GROUNDED_SYSTEM_PROMPT


def test_the_built_system_prompt_carries_the_anchor():
    system = answerer._build_system(None, None)
    assert "## Today's date" in system


def test_the_anchor_is_appended_after_everything_else(monkeypatch):
    """Order doesn't change correctness, but it must land after the directive
    and correction text — appending first and being overwritten would silently
    drop it."""
    system = answerer._build_system(
        "list", "A prior draft made unsupported claims.",
    )
    assert system.rindex("## Today's date") > system.rindex("unsupported claims")


# --------------------------------------------------------------------------- #
# 2. Source-priority guidance (Phase 6).
# --------------------------------------------------------------------------- #

def test_rule_9_tells_the_model_to_prefer_the_canonical_block():
    prompt = prompts.grounded_system_prompt()
    assert prompts.CANONICAL_MARKER in prompt
    assert "not signals of authority" in prompt


def test_length_is_explicitly_named_as_not_authority():
    """The exact failure this guards against: a long attachment outranking a
    short, direct, canonical statement purely by being longer."""
    prompt = prompts.grounded_system_prompt()
    assert "60-word" in prompt and "400-word" in prompt


# --------------------------------------------------------------------------- #
# 3. Extended refusal rules (Phase 5) — "where can I find/download X" and
#    "adjacent evidence" must not be blanket-refused.
# --------------------------------------------------------------------------- #

def test_rule_3_covers_where_can_i_download_questions():
    prompt = prompts.grounded_system_prompt()
    assert "where can i find/get/download" in prompt.lower()


def test_rule_3_covers_adjacent_evidence_as_a_supported_negative():
    prompt = prompts.grounded_system_prompt()
    assert "adjacent to what was asked" in prompt


def test_the_refusal_constant_and_grounded_prompt_still_agree():
    """The extensions must not fork the actual refusal text rule 3 quotes."""
    assert prompts.REFUSAL in prompts.grounded_system_prompt()


def test_the_five_block_context_can_still_refuse():
    """Phase 5's explicit boundary: none of the new language makes "context
    exists" sufficient on its own. The refusal path itself is untouched —
    `generate_answer` with no blocks still returns the bare refusal."""
    assert answerer.generate_answer("q", []) == prompts.REFUSAL


# --------------------------------------------------------------------------- #
# 4. The rule numbering the history and graph-facts rules append onto
#    (pre-existing invariant; must still hold after inserting new sub-bullets
#    into rules 3 and 9, since those are sub-bullets and not new top-level
#    numbered rules).
#
#    This used to assert over two prompt variants. There is one now: a context
#    mixing website and PDF blocks no longer selects a different prompt, because
#    it no longer gets a different answer shape.
# --------------------------------------------------------------------------- #

def test_the_grounded_prompt_ends_its_rule_list_where_the_extras_start():
    prompt = prompts.GROUNDED_SYSTEM_PROMPT
    assert "\n10. " not in prompt
    assert re.search(r"\b9\. ", prompt)
