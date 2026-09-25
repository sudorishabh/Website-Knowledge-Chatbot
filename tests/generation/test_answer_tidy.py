"""The deterministic list tidy, on the shapes the live answers actually took.

Every input below is an answer the small model produced on 2026-09-25 with the
answer-shape rules already in its prompt (trimmed to a few items). No network.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.core.models.context import ContextBlock
from app.generation.tidy import tidy_lists
from app.pipeline import query_pipeline as pipe
from app.retrieval.understanding.query_processor import ProcessedQuery


def test_a_single_source_list_is_cited_once_on_its_opening_sentence():
    answer = (
        "TERI's thematic areas are [1]:\n"
        "\n"
        "- **Sustainable Agriculture** — global food security [1].  \n"
        "- **Climate Change** — accelerating climate action [1].  \n"
        "- **Energy** — a transition to clean energy [1]."
    )
    assert tidy_lists(answer) == (
        "TERI's thematic areas are [1]:\n"
        "\n"
        "- **Sustainable Agriculture** — global food security.\n"
        "- **Climate Change** — accelerating climate action.\n"
        "- **Energy** — a transition to clean energy."
    )


def test_the_opening_sentence_is_given_the_citation_when_it_lacks_one():
    answer = "Its centres are:\n- **A** — x [2]\n- **B** — y [2]"
    assert tidy_lists(answer) == "Its centres are [2]:\n- **A** — x\n- **B** — y"


def test_a_list_citing_different_blocks_is_left_alone():
    # The IPCC-articles list: one block per item, each citation meaningful.
    answer = "Articles:\n- **One** — mentions it [1]\n- **Two** — mentions it [2]"
    assert tidy_lists(answer) == answer


def test_a_list_under_a_heading_keeps_its_citations():
    # No sentence to move the citation to.
    answer = "### Focus areas\n- Modelling [1]\n- Risk studies [1]"
    assert tidy_lists(answer) == answer


# A selection of people, as gpt-6-luna answered "TERI top researchers" with the
# selection shape in its prompt (trimmed to two people a group).
_GROUPED = (
    "If by top researchers you mean its senior research leaders, these are some "
    "of the most senior people listed [1][2].\n"
    "\n"
    "### Senior leadership\n"
    "- **Dr Vibha Dhawan** — Director General [2]\n"
    "- **Mr Girish Sethi** — Senior Director, Energy [2].\n"
    "\n"
    "### Distinguished Fellows\n"
    "- **Mr R R Rashmi** — Distinguished Fellow, Green Shipping [1][2]\n"
    "- **Mr S Vijay Kumar** — Distinguished Fellow, Food and Land Use [1]\n"
    "\n"
    "### By area\n"
    "- **Energy:** Girish Sethi, Jiwesh Nandan [1][2]\n"
    "- **Climate:** Suruchi Bhadwal, Ajai Malhotra [1][2]"
)


def test_named_items_under_a_heading_shed_a_marker_the_opening_carries():
    tidied = tidy_lists(_GROUPED)
    assert "- **Dr Vibha Dhawan** — Director General\n" in tidied
    assert "- **Mr Girish Sethi** — Senior Director, Energy.\n" in tidied
    # The opening still cites both blocks, so the sources footer is unchanged.
    assert "most senior people listed [1][2]." in tidied


def test_a_group_whose_items_cite_different_blocks_keeps_them():
    tidied = tidy_lists(_GROUPED)
    assert "Green Shipping [1][2]" in tidied
    assert "Food and Land Use [1]" in tidied
    # Two markers on every item is not one shared marker.
    assert "Girish Sethi, Jiwesh Nandan [1][2]" in tidied


def test_a_heading_group_keeps_a_marker_the_opening_does_not_carry():
    answer = _GROUPED.replace("listed [1][2].", "listed [1].")
    assert "- **Dr Vibha Dhawan** — Director General [2]" in tidy_lists(answer)


def test_claims_under_a_heading_keep_their_citations_whatever_the_opening():
    # An overview's bullets are claims, not named items.
    answer = (
        "Green shipping is a programme [1].\n"
        "\n"
        "### Research\n"
        "- It develops cleaner fuels [1].\n"
        "- It assesses ports [1]."
    )
    assert tidy_lists(answer) == answer


def test_an_item_with_several_citations_is_left_alone():
    answer = "Lead:\n- **A** — x [1][2]\n- **B** — y [1][2]"
    assert tidy_lists(answer) == answer


def test_an_empty_description_loses_its_dash():
    answer = (
        "The centres are [1]:\n"
        "- **TERI-CFCL Centre of Excellence (CoE)** — [1]\n"
        "- **Mahindra-TERI Centre of Excellence** — [1]"
    )
    assert tidy_lists(answer) == (
        "The centres are [1]:\n"
        "- **TERI-CFCL Centre of Excellence (CoE)**\n"
        "- **Mahindra-TERI Centre of Excellence**"
    )


def test_a_hyphen_in_a_name_is_not_a_dash():
    answer = "- **NMCG-TERI Centre** [1]"
    assert tidy_lists(answer) == answer


def test_the_read_more_line_loses_its_citation():
    answer = "Read more: [Centres of Excellence](https://teriin.org/centres) [1]"
    assert tidy_lists(answer) == "Read more: [Centres of Excellence](https://teriin.org/centres)"


def test_a_clean_answer_is_unchanged():
    answer = (
        "According to the page, its centres are [1]:\n"
        "- **A** — x\n"
        "- **B** — y\n"
        "\n"
        "Read more: [Centres](https://teriin.org/centres)"
    )
    assert tidy_lists(answer) == answer


def test_prose_is_never_touched():
    answer = "Dr A is the Director General [1]. She was earlier at Org One [2]."
    assert tidy_lists(answer) == answer


def test_the_stream_emits_the_tidied_answer_as_a_correction(monkeypatch):
    blocks = [ContextBlock(n=1, text="Centres...", payload={"source_type": "website"})]
    pq = ProcessedQuery(original="q", search_query="q")
    gen = pipe._Generation(pq=pq, blocks=blocks, query_vector=[0.1], top_k=6)
    persisted: dict = {}
    monkeypatch.setattr(pipe, "_prepare", lambda q, **kw: (None, gen))
    monkeypatch.setattr(
        pipe, "generate_stream",
        lambda q, b, history=None, answer_format=None, plan_directive="":
            iter(["The centres are [1]:\n- **A** — [1]\n- **B** — [1]"]),
    )
    monkeypatch.setattr(pipe, "get_settings", lambda: SimpleNamespace(faithfulness_check=False))
    monkeypatch.setattr(pipe, "_persist", lambda gen, result: persisted.update(result))

    events = list(pipe.stream_answer("q"))
    assert [e["type"] for e in events] == ["token", "correction", "sources", "done"]
    assert events[1]["reason"] == "list_citations"
    assert events[1]["text"] == "The centres are [1]:\n- **A**\n- **B**"
    assert persisted["answer"] == events[1]["text"]
    # The footer still sees the block the opening sentence cites.
    assert [c["n"] for c in events[2]["citations"]] == [1]
