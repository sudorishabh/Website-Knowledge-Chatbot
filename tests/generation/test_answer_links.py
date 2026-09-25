"""The "Read more" link: shown to the model for the organisation's own pages,
and unlinked in the answer whenever the context never showed its address.

A list or an overview now closes with a link to the page it came from. The
model can only copy that address if the block header carries it, and a model
asked for a link will sometimes supply a plausible one instead — so the header
gives it, and a deterministic pass removes any anchor it did not give. No
network; every model call is stubbed.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.core.models.context import PRIORITY_PAGE_KIND, ContextBlock
from app.generation import faithfulness as fa
from app.generation.prompts import GROUNDED_SYSTEM_PROMPT, _source_hint
from app.pipeline import query_pipeline as pipe
from app.retrieval.understanding.query_processor import ProcessedQuery

URL = "https://teriin.org/centres-excellence"


def _live(**extra):
    return {
        "kind": PRIORITY_PAGE_KIND, "source_type": "website", "source_authority": 1.0,
        "title": "Centres of Excellence", "source_url": URL,
        "fetched_at": "2026-09-25T10:00:00+00:00", **extra,
    }


def _block(n, text, **payload):
    payload.setdefault("source_type", "website")
    return ContextBlock(n=n, text=text, payload=payload)


# --------------------------------------------------------------------------- #
# The header.
# --------------------------------------------------------------------------- #

def test_a_live_page_header_carries_its_address():
    assert _source_hint(_live()).endswith(f"· link {URL}")


def test_an_official_page_header_carries_its_address():
    payload = {"source_type": "website", "source_authority": 0.95,
               "title": "Mission and Goals", "source_url": "https://teriin.org/mission"}
    assert "link https://teriin.org/mission" in _source_hint(payload)


def test_an_ordinary_article_header_carries_no_address():
    # Its address is in the sources footer already; offering it here would only
    # invite a link per bullet.
    payload = {"source_type": "website", "source_authority": 0.3, "bundle": "news",
               "title": "A story", "source_url": "https://teriin.org/news/a-story"}
    assert "link " not in _source_hint(payload)


def test_a_pdf_header_carries_no_address():
    payload = {"source_type": "pdf_attachment", "source_authority": 1.0,
               "title": "Annual Report", "file_url": "https://teriin.org/files/ar.pdf"}
    assert "link " not in _source_hint(payload)


def test_the_style_asks_for_the_link_only_as_the_header_gives_it():
    assert "Read more: [the page's title](that link)" in GROUNDED_SYSTEM_PROMPT
    assert "Never write an address the context does not show" in GROUNDED_SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# strip_unknown_links
# --------------------------------------------------------------------------- #

def test_a_link_from_the_header_survives():
    blocks = [ContextBlock(n=1, text="Centres...", payload=_live())]
    answer = f"Read more: [Centres of Excellence]({URL})"
    assert fa.strip_unknown_links(answer, blocks) == answer


def test_an_invented_link_keeps_its_label_and_loses_its_anchor():
    blocks = [ContextBlock(n=1, text="Centres...", payload=_live())]
    answer = "Read more: [Centres](https://teriin.org/our-centres)"
    assert fa.strip_unknown_links(answer, blocks) == "Read more: Centres"


def test_a_link_copied_from_a_block_text_survives():
    # A live page names the documents it links to as "Title (https://...)".
    doc = "https://teriin.org/files/A_Transformative_Global_Goal_New_File.pdf"
    blocks = [_block(1, f"A Transformative Global Goal on Adaptation ({doc})")]
    answer = f"See [A Transformative Global Goal on Adaptation]({doc}) [1]."
    assert fa.strip_unknown_links(answer, blocks) == answer


def test_trailing_slash_and_case_do_not_make_a_known_link_unknown():
    blocks = [ContextBlock(n=1, text="x", payload=_live())]
    answer = f"[Centres]({URL.upper()}/)"
    assert fa.strip_unknown_links(answer, blocks) == answer


def test_citation_markers_are_not_links():
    blocks = [_block(1, "x")]
    assert fa.strip_unknown_links("A claim [1].", blocks) == "A claim [1]."


# --------------------------------------------------------------------------- #
# The stream.
# --------------------------------------------------------------------------- #

def _wire(monkeypatch, draft, persisted):
    blocks = [ContextBlock(n=1, text="Centres...", payload=_live())]
    pq = ProcessedQuery(original="q", search_query="q")
    gen = pipe._Generation(pq=pq, blocks=blocks, query_vector=[0.1], top_k=6)
    monkeypatch.setattr(pipe, "_prepare", lambda q, **kw: (None, gen))
    monkeypatch.setattr(
        pipe, "generate_stream",
        lambda q, b, history=None, answer_format=None, plan_directive="": iter([draft]),
    )
    monkeypatch.setattr(pipe, "get_settings", lambda: SimpleNamespace(faithfulness_check=False))
    monkeypatch.setattr(pipe, "_persist", lambda gen, result: persisted.update(result))


def test_the_stream_replaces_an_answer_carrying_an_invented_link(monkeypatch):
    persisted: dict = {}
    _wire(monkeypatch, "The centres are listed [1].\n\nRead more: "
                       "[Centres](https://teriin.org/made-up)", persisted)

    events = list(pipe.stream_answer("q"))
    assert [e["type"] for e in events] == ["token", "correction", "sources", "done"]
    assert events[1]["reason"] == "unknown_link"
    assert events[1]["text"].endswith("Read more: Centres")
    assert persisted["answer"] == events[1]["text"]


def test_the_stream_leaves_a_known_link_alone(monkeypatch):
    persisted: dict = {}
    draft = f"The centres are listed [1].\n\nRead more: [Centres]({URL})"
    _wire(monkeypatch, draft, persisted)

    events = list(pipe.stream_answer("q"))
    assert [e["type"] for e in events] == ["token", "sources", "done"]
    assert persisted["answer"] == draft
