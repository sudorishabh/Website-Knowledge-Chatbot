"""The projects a named person has a stated role in: which mentions count, and
one block per project. Every passage below is a project chunk as ingested."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.retrieval.priority import projects

EARTH_COOLING = (
    "Funded by the Advanced Research and Invention Agency (ARIA), UK.  Principal "
    "Investigator: Dr. Byju Pookkandy.  Co-Principal Investigator: Mr K Venkatramana  "
    "Team: Ms Suruchi Bhadwal; Dr Manish Kumar Shrivastava"
)


@pytest.mark.parametrize("name, role", [
    ("Dr Byju Pookkandy", "principal investigator"),
    ("Mr K Venkatramana", "co-principal investigator"),
    ("Ms Suruchi Bhadwal", "team"),
    # The second member of a team, after the first.
    ("Dr Manish Kumar Shrivastava", "team"),
])
def test_a_role_heading_before_the_name_is_their_role(name, role):
    assert projects.role_of(EARTH_COOLING, name) == role


@pytest.mark.parametrize("text", [
    "For more information, contact Mr. K Umamaheswaran (k.umamaheswaran@teri.res.in) "
    "Ms. Suruchi Bhadwal (suruchib@teri.res.in)",
    "Partners  Contact  Ms. Suruchi Bhadwal  Associate Director, Earth Science",
])
def test_a_contact_is_a_role(text):
    assert projects.role_of(text, "Ms Suruchi Bhadwal") == "contact"


@pytest.mark.parametrize("text", [
    "Authors: Dhriti Pathak, Kavya Michael Reviewer: Suruchi Bhadwal Chapter 5",
    "Moderated Roundtable Discussion Moderator: Ms Suruchi Bhadwal, Senior Fellow",
    "Broadcast Schedule of TERI experts' talk 01 June, 2024 - Ms Suruchi Bhadwal",
    # A thank-you names the people it thanks, whatever it calls them.
    "The team would like to acknowledge with gratitude, the guidance and support "
    "received from Charter advisory team, Dr Vibha Dhawan, Director General",
    # A sentence ended between the role and the name.
    "Contact the project office. The event was opened by Suruchi Bhadwal.",
])
def test_a_mention_without_a_role_is_not_a_project_role(text):
    name = "Dr Vibha Dhawan" if "Dhawan" in text else "Ms Suruchi Bhadwal"
    assert projects.role_of(text, name) is None


def _point(text, *, url, day, source_type="website", title="A project"):
    return SimpleNamespace(payload={
        "chunk_text": text, "source_url": url, "source_type": source_type, "title": title,
        "effective_start_date": day, "bundle": "ongoing_projects", "document_id": url,
    })


def test_one_block_per_project_latest_first(monkeypatch):
    points = [
        _point("Contact Ms Suruchi Bhadwal", url="https://teriin.org/project/hi-aware",
               day="2014-01-01", title="HI-AWARE"),
        _point("Team: Ms Suruchi Bhadwal", url="https://teriin.org/project/cooling",
               day="2025-06-17", source_type="pdf_attachment", title="https://teriin.org/files/c.pdf"),
        _point("Team: Ms Suruchi Bhadwal; Dr Manish", url="https://teriin.org/project/cooling",
               day="2025-06-17", title="Simulating the Effects of Earth Cooling"),
        _point("Reviewer: Suruchi Bhadwal", url="https://teriin.org/project/cop26", day="2021-05-24"),
    ]
    monkeypatch.setattr(projects, "_scroll", lambda name: points)
    blocks = projects.person_projects("Ms Suruchi Bhadwal")
    # The project page's own text over its attachment's; the reviewer is no role.
    assert [b.payload["title"] for b in blocks] == ["Simulating the Effects of Earth Cooling",
                                                    "HI-AWARE"]
    assert blocks[0].text == "Team: Ms Suruchi Bhadwal; Dr Manish"
    assert blocks[0].payload[projects.PROJECT_OF] == "Ms Suruchi Bhadwal"
    assert blocks[0].payload["project_role"] == "team"
    assert "chunk_text" not in blocks[0].payload
    assert len(projects.person_projects("Ms Suruchi Bhadwal", limit=1)) == 1


def test_a_failed_search_costs_only_the_projects(monkeypatch):
    def boom(name):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(projects, "_scroll", boom)
    assert projects.person_projects("Ms Suruchi Bhadwal") == []
