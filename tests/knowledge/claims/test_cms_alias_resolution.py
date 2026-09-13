"""CMS field values resolve by canonical name, then by exact alias.

The CMS writes whichever form the editor had to hand. A partner field says
"ONGC"; the seeded canonical name is "Oil and Natural Gas Corporation Limited".
Exact-canonical-only lookup dropped that relationship even though the alias
store already held the other form, so 32 CMS-stated partner relationships never
reached the graph.

The fallback is still *exact* on both passes. No fuzzy, prefix or partial
matching is introduced, and every protection the per-mention resolver applies to
an alias applies here too.
"""
from __future__ import annotations

import json

from app.knowledge.claims import extract_cms

DOC = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PROJECT = "project-1"
ONGC = "org-ongc"
CPCB = "org-cpcb"
MOEF = "org-moef"
PERSON = "person-alice"


class _Alias:
    """The shape `EntityIndex.alias_groups` yields: candidates with flags."""

    def __init__(self, entity_id, *, autolink=True, is_ambiguous=False):
        self.entity_id = entity_id
        self.autolink = autolink
        self.is_ambiguous = is_ambiguous


class _Index:
    """A stand-in for EntityIndex exposing only what extraction consumes."""

    def __init__(self, entities, aliases=None):
        self.entities = entities
        self._aliases = aliases or {}

    def alias_groups(self):
        return iter(self._aliases.items())


class _IndexWithoutAliases:
    """An index predating `alias_groups` -- the back-compatibility case."""

    def __init__(self, entities):
        self.entities = entities


ENTITIES = {
    PROJECT: {"entity_type": "PROJECT", "normalized_name": "a project",
              "canonical_name": "A Project", "cms_uuid": DOC},
    ONGC: {"entity_type": "ORGANIZATION",
           "normalized_name": "oil and natural gas corporation limited",
           "canonical_name": "Oil and Natural Gas Corporation Limited"},
    CPCB: {"entity_type": "ORGANIZATION",
           "normalized_name": "central pollution control board",
           "canonical_name": "Central Pollution Control Board"},
    MOEF: {"entity_type": "ORGANIZATION",
           "normalized_name": "ministry of environment forest and climate change",
           "canonical_name": "Ministry of Environment, Forest and Climate Change"},
    PERSON: {"entity_type": "PERSON", "normalized_name": "alice smith",
             "canonical_name": "Dr Alice Smith"},
}


def _context(aliases=None):
    return extract_cms.CmsClaimContext.from_index(_Index(ENTITIES, aliases))


def _claims(meta, aliases=None):
    return extract_cms.claims_from_meta(DOC, json.dumps(meta), context=_context(aliases))


def _of(claims, predicate):
    return [c for c in claims if c.predicate == predicate]


# --------------------------------------------------------------------------- #
# The fallback resolves what canonical matching missed
# --------------------------------------------------------------------------- #

def test_an_alias_resolves_when_the_canonical_name_misses():
    ctx = _context({("ORGANIZATION", "ongc"): [_Alias(ONGC)]})
    assert ctx.object_for("ORGANIZATION", "ONGC") == ONGC


def test_the_alias_reaches_a_real_claim():
    claims = _of(
        _claims({"field_completed_partners": ["ONGC"]},
                {("ORGANIZATION", "ongc"): [_Alias(ONGC)]}),
        "PARTNER_OF",
    )
    assert len(claims) == 1
    assert claims[0].object_entity_id == ONGC
    # The value recorded is what the CMS actually said, not the canonical name.
    assert claims[0].source_value == "ONGC"


def test_person_aliases_resolve_too():
    """The fallback is per entity type, so a PI named by an alias also lands."""
    ctx = _context({("PERSON", "a smith"): [_Alias(PERSON)]})
    assert ctx.object_for("PERSON", "A Smith") == PERSON


def test_without_the_alias_nothing_resolves():
    """The baseline the fallback exists to change."""
    assert _context().object_for("ORGANIZATION", "ONGC") is None


# --------------------------------------------------------------------------- #
# Canonical wins
# --------------------------------------------------------------------------- #

def test_a_canonical_name_beats_an_alias_on_the_same_form():
    """An alias must never shadow an entity the CMS named outright."""
    aliases = {("ORGANIZATION", "central pollution control board"): [_Alias(MOEF)]}
    assert _context(aliases).object_for(
        "ORGANIZATION", "Central Pollution Control Board") == CPCB


# --------------------------------------------------------------------------- #
# Ambiguity protections are preserved
# --------------------------------------------------------------------------- #

def test_an_ambiguous_alias_is_refused():
    """The seeder saw this form denote more than one entity, so it identifies
    nothing on its own -- the same answer per-mention resolution gives."""
    ctx = _context({("ORGANIZATION", "ongc"): [_Alias(ONGC, is_ambiguous=True)]})
    assert ctx.object_for("ORGANIZATION", "ONGC") is None


def test_an_alias_marked_do_not_autolink_is_refused():
    ctx = _context({("ORGANIZATION", "ongc"): [_Alias(ONGC, autolink=False)]})
    assert ctx.object_for("ORGANIZATION", "ONGC") is None


def test_an_alias_that_maps_to_two_entities_is_refused():
    """A collision the ambiguity marker has not reached yet. Refused rather
    than arbitrarily resolved to whichever sorted first."""
    ctx = _context({("ORGANIZATION", "cpcb"): [_Alias(CPCB), _Alias(MOEF)]})
    assert ctx.object_for("ORGANIZATION", "CPCB") is None


def test_one_entity_named_by_two_alias_rows_still_resolves():
    """Two rows, one entity, is not a collision."""
    ctx = _context({("ORGANIZATION", "ongc"): [_Alias(ONGC), _Alias(ONGC)]})
    assert ctx.object_for("ORGANIZATION", "ONGC") == ONGC


def test_a_blocked_alias_does_not_fall_through_to_another_candidate():
    """If the only safe candidate is removed, the answer is None -- not the
    unsafe one."""
    ctx = _context({("ORGANIZATION", "ongc"): [
        _Alias(ONGC, is_ambiguous=True), _Alias(CPCB, autolink=False)]})
    assert ctx.object_for("ORGANIZATION", "ONGC") is None


# --------------------------------------------------------------------------- #
# No fuzzy matching
# --------------------------------------------------------------------------- #

def test_a_partial_value_does_not_match_an_alias():
    ctx = _context({("ORGANIZATION", "ongc"): [_Alias(ONGC)]})
    for value in ("ONG", "ONGCX", "ONGC Energy Centre", "the ongc"):
        assert ctx.object_for("ORGANIZATION", value) is None, value


def test_an_alias_of_another_type_does_not_resolve():
    """Alias lookup is keyed by entity type, as the rule table demands."""
    ctx = _context({("PERSON", "ongc"): [_Alias(PERSON)]})
    assert ctx.object_for("ORGANIZATION", "ONGC") is None


def test_an_unknown_value_still_resolves_to_nothing():
    ctx = _context({("ORGANIZATION", "ongc"): [_Alias(ONGC)]})
    assert ctx.object_for("ORGANIZATION", "Some Organisation We Never Saw") is None


# --------------------------------------------------------------------------- #
# Back-compatibility
# --------------------------------------------------------------------------- #

def test_an_index_without_alias_support_behaves_as_before():
    ctx = extract_cms.CmsClaimContext.from_index(_IndexWithoutAliases(ENTITIES))
    assert ctx.alias_lookup == {}
    assert ctx.object_for("ORGANIZATION", "ONGC") is None
    assert ctx.object_for(
        "ORGANIZATION", "Central Pollution Control Board") == CPCB


def test_the_context_can_still_be_built_without_an_alias_lookup():
    ctx = extract_cms.CmsClaimContext(lookup={"ORGANIZATION": {}}, projects_by_uuid={})
    assert ctx.alias_lookup == {}


def test_only_the_rule_table_types_are_indexed():
    """PROJECT aliases are never object candidates, so they are not carried."""
    ctx = _context({("PROJECT", "a project"): [_Alias(PROJECT)]})
    assert set(ctx.alias_lookup) == {"ORGANIZATION", "PERSON"}
