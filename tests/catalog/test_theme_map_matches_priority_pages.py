"""The theme map follows the priority page list.

`data/priority_crawl_pages.json` mirrors the live site's themes menu and is the
authority for which themes are main, which are other, and what sits under what.
`app/theme_structure.json` is what ingestion classifies against; this keeps the
two from drifting apart again (they had: "Microbes" was still a sub-theme, and
four live sub-themes were missing).
"""
from __future__ import annotations

import json
from pathlib import Path

from app.catalog import theme_taxonomy

ROOT = Path(__file__).resolve().parents[2]


def _edges(buckets, wanted=("Main Themes", "Other Themes")):
    """(bucket, parent, name) for every theme, names keyed the way the
    classifier keys them."""
    out = set()

    def walk(nodes, bucket, parent):
        for node in nodes or ():
            name = theme_taxonomy._key(node["name"])
            out.add((bucket, parent, name))
            walk(node.get("children"), bucket, name)

    for bucket in buckets:
        if bucket.get("name") in wanted:
            walk(bucket.get("children"), bucket["name"], None)
    return out


def test_the_theme_map_is_the_priority_lists_theme_tree():
    priority = json.loads((ROOT / "data" / "priority_crawl_pages.json").read_text(encoding="utf-8"))
    theme_map = json.loads((ROOT / "app" / "theme_structure.json").read_text(encoding="utf-8"))
    assert _edges(theme_map) == _edges(priority)


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
