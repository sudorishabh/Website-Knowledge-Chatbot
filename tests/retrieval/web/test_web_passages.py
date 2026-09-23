"""Web documents as ranked passages on the corpus's own footing.

Three contracts are pinned here. Passages are paragraph-sized and never straddle
a section, and each carries its section as context — the corpus's parent/child
shape. They are embedded the way ingestion embeds a child ("title › heading"
breadcrumb, plain stored text) and scored by cosine against the question, so
their scores mean what a corpus chunk's scores mean. And every one carries its
provenance: where, when, how it was found, and — for a PDF — which page.

The embedder is a parameter, so a deterministic fake stands in for Azure.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.models.context import WEB_SOURCE_TYPE
from app.retrieval.web import passages
from app.retrieval.web.extract import PARAGRAPH, TABLE, WebBlock, WebDocument
from app.retrieval.web.providers import SearchHit

WORDS = "alpha beta gamma delta epsilon zeta eta theta iota kappa".split()


def _sentence(n: int, word: str = "fact") -> str:
    return " ".join([word] * (n - 1)) + " end."


def _doc(blocks: list[WebBlock], **fields) -> WebDocument:
    base = dict(
        url="https://www.teriin.org/user/15680",
        final_url="https://www.teriin.org/user/15680",
        canonical_url="https://www.teriin.org/user/15680",
        domain="teriin.org", kind="html", fetched_at="2026-09-23T06:00:00+00:00",
        title="Mr Sayanta Ghosh | TERI", published="2025-03-04", date_source="page_markup",
        authors=[],
    )
    base.update(fields)
    return WebDocument(blocks=blocks, **base)


def _hit(url: str = "https://www.teriin.org/user/15680", **fields) -> SearchHit:
    base = dict(url=url, title="Mr Sayanta Ghosh | TERI", snippet="", provider="brave",
                query="Sayanta Ghosh TERI", rank=1, site="teriin.org")
    base.update(fields)
    return SearchHit(**base)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "web_primary_domains", "teriin.org")
    monkeypatch.setattr(s, "web_blocked_domains", "")
    monkeypatch.setattr(s, "web_allow_third_party", True)
    monkeypatch.setattr(s, "web_passages_per_document", 3)
    monkeypatch.setattr(s, "web_max_candidates", 12)
    monkeypatch.setattr(s, "dedup_cosine_threshold", 0.92)
    return s


class _Embedder:
    """Vectors from keywords: dimension i counts WORDS[i] in the text, plus a
    constant so no vector is zero. Records every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vector(t) for t in texts]

    @staticmethod
    def vector(text: str) -> list[float]:
        lowered = text.lower().split()
        return [float(lowered.count(w)) for w in WORDS] + [0.1]


# --- splitting ---------------------------------------------------------------


def test_short_paragraphs_under_one_heading_are_joined():
    doc = _doc([WebBlock(_sentence(20), heading="Profile"),
                WebBlock(_sentence(20), heading="Profile")])
    (only,) = passages.split(doc)
    assert passages._words(only.text) == 40 and only.heading == "Profile"


def test_a_heading_change_always_closes_the_passage():
    doc = _doc([WebBlock(_sentence(20), heading="Profile"),
                WebBlock(_sentence(20), heading="Publications")])
    first, second = passages.split(doc)
    assert (first.heading, second.heading) == ("Profile", "Publications")
    assert first.section != second.section


def test_a_long_paragraph_is_cut_at_sentence_ends():
    long = " ".join(_sentence(50) for _ in range(10))       # 500 words, 10 sentences
    parts = passages.split(_doc([WebBlock(long, heading="Findings")]))
    assert len(parts) >= 2
    assert all(passages._words(p.text) <= passages._MAX_WORDS for p in parts)
    assert all(p.text.endswith("end.") for p in parts)


def test_a_table_is_its_own_passage_even_when_small():
    doc = _doc([WebBlock(_sentence(20), heading="Shares"),
                WebBlock("Season | PM10\nWinter | 24%", kind=TABLE, heading="Shares"),
                WebBlock(_sentence(20), heading="Shares")])
    kinds = [p.kind for p in passages.split(doc)]
    assert kinds == [PARAGRAPH, TABLE, PARAGRAPH]


def test_fragments_too_short_to_be_evidence_are_dropped_not_merged():
    doc = _doc([WebBlock("9 April 2025"), WebBlock("Share"), WebBlock(_sentence(12))])
    (only,) = passages.split(doc)
    assert only.text == _sentence(12)                 # no date line, no "Share"


def test_short_list_items_are_merged_rather_than_lost():
    doc = _doc([WebBlock("Mitigation options include:", heading="Options"),
                WebBlock("Fleet modernisation", heading="Options"),
                WebBlock("Public transport expansion", heading="Options"),
                WebBlock("Banning pre-BS-VI trucks", heading="Options")])
    (only,) = passages.split(doc)
    assert "Fleet modernisation" in only.text and "pre-BS-VI" in only.text


def test_a_pdf_passage_records_the_pages_it_spans():
    doc = _doc([WebBlock(_sentence(60), heading="Summary", page=3),
                WebBlock(_sentence(60), heading="Summary", page=4)], kind="pdf")
    (only,) = passages.split(doc)
    assert (only.page_start, only.page_end) == (3, 4)


# --- context -----------------------------------------------------------------


def test_the_context_is_the_surrounding_section_headed_by_its_heading():
    doc = _doc([WebBlock(_sentence(200, "one"), heading="A"),
                WebBlock(_sentence(200, "two"), heading="A"),
                WebBlock(_sentence(200, "three"), heading="A"),
                WebBlock(_sentence(200, "other"), heading="B")])
    parts = passages.split(doc)
    text, _, _ = passages._context(parts, 1)
    assert text.startswith("A\n\n")
    assert "two" in text and "one" in text and "three" in text
    assert "other" not in text                       # never across a section


def test_the_context_is_bounded():
    doc = _doc([WebBlock(_sentence(250, w), heading="A") for w in ("a", "b", "c", "d", "e")])
    parts = passages.split(doc)
    text, _, _ = passages._context(parts, 2)
    assert passages._words(text) <= passages._CONTEXT_WORDS + 1   # + the heading


# --- embedding like the corpus -----------------------------------------------


def test_the_embedded_text_carries_ingestions_breadcrumb():
    doc = _doc([WebBlock(_sentence(12), heading="Publications")])
    (p,) = passages.split(doc)
    assert passages._embed_text(doc, p) == (
        f"Mr Sayanta Ghosh | TERI › Publications\n\n{p.text}"
    )


def test_without_a_title_or_heading_the_text_is_embedded_bare():
    doc = _doc([WebBlock(_sentence(12))], title=None)
    (p,) = passages.split(doc)
    assert passages._embed_text(doc, p) == p.text


# --- candidates ----------------------------------------------------------------


def _profile() -> WebDocument:
    return _doc([
        WebBlock("Mr Sayanta Ghosh is an Associate Fellow and Area Convener for the "
                 "Centre for Geospatial Technology Application alpha alpha.", heading="Profile"),
        WebBlock("He has more than 25 publications to his credit in reputed journals "
                 "beta beta beta.", heading="Publications"),
    ])


def test_passages_are_scored_by_cosine_against_the_question():
    embed = _Embedder()
    query_vector = _Embedder.vector("beta")                 # the publications question
    candidates = passages.to_candidates([(_profile(), _hit())], "publications",
                                        query_vector, embed=embed)
    assert "25 publications" in candidates[0].payload["chunk_text"]
    assert candidates[0].semantic_score == candidates[0].score
    assert candidates[0].semantic_score > candidates[1].semantic_score
    assert candidates[0].vector                             # carried for de-duplication


def test_every_passage_is_embedded_in_one_call():
    embed = _Embedder()
    other = _doc(_profile().blocks, canonical_url="https://www.teriin.org/other")
    docs = [(_profile(), _hit()), (other, _hit(rank=2))]
    passages.to_candidates(docs, "q", _Embedder.vector("alpha"), embed=embed)
    assert len(embed.calls) == 1 and len(embed.calls[0]) == 4


def test_each_document_contributes_at_most_its_share(settings, monkeypatch):
    monkeypatch.setattr(settings, "web_passages_per_document", 1)
    doc = _doc([WebBlock(_sentence(12, w), heading=w) for w in WORDS[:5]])
    candidates = passages.to_candidates([(doc, _hit())], "q", _Embedder.vector("alpha"),
                                        embed=_Embedder())
    assert len(candidates) == 1


def test_the_total_is_capped(settings, monkeypatch):
    monkeypatch.setattr(settings, "web_passages_per_document", 10)
    monkeypatch.setattr(settings, "web_max_candidates", 2)
    doc = _doc([WebBlock(_sentence(12, w), heading=w) for w in WORDS[:5]])
    assert len(passages.to_candidates([(doc, _hit())], "q", _Embedder.vector("alpha"),
                                      embed=_Embedder())) == 2


def test_the_same_passage_on_two_sites_is_kept_once():
    text = "Delhi's vehicular emissions contribute 24% of PM10 gamma gamma in winter."
    original = _doc([WebBlock(text)], canonical_url="https://www.teriin.org/article/x")
    syndicated = _doc([WebBlock(text)], canonical_url="https://news.example/teri-x",
                      domain="news.example")
    candidates = passages.to_candidates(
        [(original, _hit(rank=1)), (syndicated, _hit(url="https://news.example/teri-x", rank=2))],
        "vehicular emissions", _Embedder.vector("gamma"), embed=_Embedder(),
    )
    assert len(candidates) == 1
    assert candidates[0].payload["domain"] == "teriin.org"   # ties go to the better search rank


def test_a_long_document_embeds_only_its_most_relevant_passages(monkeypatch):
    monkeypatch.setattr(passages, "_MAX_EMBEDDED_PER_DOCUMENT", 3)
    blocks = [WebBlock(_sentence(12, "filler"), heading=f"S{i}") for i in range(10)]
    blocks.append(WebBlock("Seaweed biomass can replace aquafeed and NPK fertilizer here.",
                           heading="S10"))
    embed = _Embedder()
    passages.to_candidates([(_doc(blocks), _hit())], "seaweed aquafeed NPK fertilizer",
                           _Embedder.vector("alpha"), embed=embed)
    embedded = embed.calls[0]
    assert len(embedded) == 3
    assert any("aquafeed" in text for text in embedded)     # kept although it is last


def test_a_document_with_no_usable_passage_costs_no_embedding():
    embed = _Embedder()
    assert passages.to_candidates([(_doc([WebBlock("Share")]), _hit())], "q",
                                  _Embedder.vector("alpha"), embed=embed) == []
    assert embed.calls == []


# --- provenance ----------------------------------------------------------------


def _only(doc: WebDocument, hit: SearchHit | None = None) -> dict:
    (candidate,) = passages.to_candidates([(doc, hit or _hit())], "q",
                                          _Embedder.vector("alpha"), embed=_Embedder(),
                                          per_document=1)
    return candidate.payload


def test_the_payload_states_where_when_and_how_the_passage_was_found():
    payload = _only(_doc([WebBlock(_sentence(12, "alpha"), heading="Profile")],
                         authors=["Mr Sayanta Ghosh"]))
    assert payload["source_type"] == WEB_SOURCE_TYPE
    assert payload["url"] == "https://www.teriin.org/user/15680"
    assert payload["domain"] == "teriin.org"
    assert payload["is_primary_source"] is True
    assert payload["title"] == "Mr Sayanta Ghosh | TERI"
    assert payload["section_heading"] == "Profile"
    assert payload["authors"] == ["Mr Sayanta Ghosh"]
    assert payload["content_type"] == "html"
    assert payload["retrieved_at"] == "2026-09-23T06:00:00+00:00"
    assert payload["retrieval_method"] == "web_search:brave"
    assert (payload["search_query"], payload["search_rank"]) == ("Sayanta Ghosh TERI", 1)
    assert payload["untrusted"] is True
    assert payload["document_id"].startswith("web:")
    assert payload["chunk_id"] == f"{payload['document_id']}:0"
    assert payload["context_text"].startswith("Profile\n\n")


def test_the_stated_date_is_kept_with_where_it_was_read():
    payload = _only(_doc([WebBlock(_sentence(12, "alpha"))]))
    assert (payload["published_date"], payload["date_source"]) == ("2025-03-04", "page_markup")
    assert payload["effective_start_date"] == "2025-03-04T00:00:00"


def test_without_a_stated_date_the_providers_estimate_is_labelled_as_such():
    payload = _only(_doc([WebBlock(_sentence(12, "alpha"))], published=None, date_source=None),
                    _hit(published="2025-03-01"))
    assert (payload["published_date"], payload["date_source"]) == ("2025-03-01", "search_provider")


def test_an_undated_passage_is_never_dated_by_when_it_was_fetched():
    payload = _only(_doc([WebBlock(_sentence(12, "alpha"))], published=None, date_source=None))
    assert "published_date" not in payload
    assert "effective_start_date" not in payload
    assert payload["retrieved_at"] == "2026-09-23T06:00:00+00:00"


def test_a_third_party_page_is_not_a_primary_source():
    payload = _only(_doc([WebBlock(_sentence(12, "alpha"))],
                         canonical_url="https://news.example/story", domain="news.example"))
    assert payload["is_primary_source"] is False


def test_a_pdf_passage_cites_its_page_and_states_its_contexts_span():
    doc = _doc([WebBlock(_sentence(150, "alpha"), heading="Summary", page=5),
                WebBlock(_sentence(150, "beta"), heading="Summary", page=6)],
               kind="pdf", url="https://www.teriin.org/files/a.pdf",
               final_url="https://www.teriin.org/files/a.pdf",
               canonical_url="https://www.teriin.org/files/a.pdf")
    payload = _only(doc)
    assert payload["content_type"] == "pdf"
    assert payload["file_url"] == "https://www.teriin.org/files/a.pdf"
    assert payload["page_number"] == 5
    assert payload["page_range"] == [5, 6]


def test_an_html_passage_carries_no_page_fields():
    payload = _only(_doc([WebBlock(_sentence(12, "alpha"))]))
    assert "page_number" not in payload and "page_range" not in payload
    assert "file_url" not in payload
