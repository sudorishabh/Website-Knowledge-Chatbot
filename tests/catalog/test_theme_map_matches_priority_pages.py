"""The theme map names the themes the priority page list names.

`data/priority_crawl_pages.json` mirrors the live site and is the authority for
which themes exist. Since 2026-09-25 it lists them flat, one "<theme> Theme"
page each, so it no longer says what sits under what; the hierarchy that
ingestion classifies against lives in `app/theme_structure.json` alone. This
keeps the two lists of names from drifting apart again (they had: "Microbes"
was still a theme there, and four live themes were missing).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.catalog import theme_taxonomy

ROOT = Path(__file__).resolve().parents[2]


def _map_names(buckets):
    """Every theme in the map, at any depth, keyed the way the classifier keys
    them."""
    out = set()

    def walk(nodes):
        for node in nodes or ():
            out.add(theme_taxonomy._key(node["name"]))
            walk(node.get("children"))

    for bucket in buckets:
        if bucket.get("name") in ("Main Themes", "Other Themes"):
            walk(bucket.get("children"))
    return out


def _listed_names(entries):
    """Every "<theme> Theme" page on the priority list, without the suffix."""
    return {
        theme_taxonomy._key(re.sub(r"\s+theme$", "", entry["name"].strip(), flags=re.I))
        for entry in entries
        if re.search(r"\s+theme$", str(entry.get("name") or "").strip(), re.I)
    }


def test_the_theme_map_names_the_priority_lists_themes():
    priority = json.loads((ROOT / "data" / "priority_crawl_pages.json").read_text(encoding="utf-8"))
    theme_map = json.loads((ROOT / "app" / "theme_structure.json").read_text(encoding="utf-8"))
    assert _map_names(theme_map) == _listed_names(priority)


def test_ampersand_and_and_are_one_theme_key():
    assert theme_taxonomy._key("Environment & Public Health") == \
        theme_taxonomy._key("Environment and Public Health")
    assert theme_taxonomy._key("Forest&Biodiversity") == theme_taxonomy._key("forest and biodiversity")


def test_the_cms_spelling_classifies_against_the_map():
    theme_taxonomy.reload_taxonomy()
    try:
        (row,) = theme_taxonomy.classify(["Environment & Public Health"])
        assert row.name == "Environment & Public Health"  # the CMS name is stored as-is
        assert (row.theme_type, row.group) == (theme_taxonomy.PRIMARY, theme_taxonomy.MAIN)
        (sub,) = theme_taxonomy.classify(["Marine and Coastal"])
        assert sub.parent == "Environment" and sub.group == theme_taxonomy.MAIN
    finally:
        theme_taxonomy.reload_taxonomy()
