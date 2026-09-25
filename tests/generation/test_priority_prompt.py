"""How a live priority page is presented to the model."""
from __future__ import annotations

from app.core.models.context import PRIORITY_PAGE_KIND, ContextBlock
from app.generation import prompts
from app.generation.prompts import CANONICAL_MARKER, LIVE_MARKER, format_context_blocks


def _live(**extra):
    payload = {
        "kind": PRIORITY_PAGE_KIND, "source_type": "website", "source_authority": 1.0,
        "title": "Climate Change", "source_url": "https://teriin.org/climate",
        "fetched_at": "2026-09-24T10:00:00+00:00", **extra,
    }
    return ContextBlock(n=1, text="Climate Change\nIn the post-Paris agreement era...", payload=payload)


def _header(block):
    return format_context_blocks([block]).splitlines()[0]


def test_a_live_block_is_an_official_page_read_on_a_date():
    header = _header(_live())
    assert header == (f"[1] (website · {CANONICAL_MARKER} · {LIVE_MARKER}, read 2026-09-24 · "
                      "Climate Change)")


def test_a_live_block_never_carries_a_page_date():
    assert "page date" not in _header(_live())


def test_a_section_heading_is_shown():
    assert _header(_live(section_heading="Team")).endswith("· Climate Change · Team)")


def test_a_stale_copy_says_so():
    assert "did not answer; this is the last copy read" in _header(_live(stale=True))


def test_an_ordinary_page_is_not_called_live():
    block = ContextBlock(n=1, text="x", payload={"source_type": "website", "bundle": "news",
                                                  "title": "A story"})
    assert LIVE_MARKER not in _header(block)


def test_the_rules_explain_the_live_marker():
    rules = prompts.grounded_system_prompt()
    assert f'says "{LIVE_MARKER}"' in rules
    assert "never give it as a publication date" in rules
    assert "give its title and the link" in rules
