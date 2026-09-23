"""Promote CMS author names to ``author_attested``. Dry run by default.

A name earns the right to hold ``AUTHORED`` — and only ``AUTHORED`` — by being
named in an authoritative CMS author field, resolving by exact normalized match
to an existing PERSON, and surviving the discriminating tests in
:mod:`app.knowledge.author_promotion`. Nothing here mints an entity, nothing is
demoted, and ``claim_eligible`` is never set.

    python -m scripts.promote_authors                 # decide and report
    python -m scripts.promote_authors --json          # machine-readable
    python -m scripts.promote_authors --apply         # write the trust level
    python -m scripts.promote_authors --documents     # the re-run scope
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter

logger = logging.getLogger("promote_authors")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="Write the trust level. Without this, nothing changes.")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    parser.add_argument("--documents", action="store_true",
                        help="Also list the documents a promotion would affect.")
    parser.add_argument("--limit", type=int, default=15,
                        help="How many example decisions to show (default 15).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    from app.knowledge.author_promotion import (
        PROMOTION_VERSION, affected_documents, apply_promotions,
        evaluate_promotions,
    )

    decisions = evaluate_promotions()
    promoted = [d for d in decisions if d.promote]
    refused = [d for d in decisions if not d.promote]
    reasons = Counter(d.reason for d in refused)
    # Group the refusals by rule rather than by their parameterised message, so
    # "surname shared with 26 other people" and "...with 8..." read as one rule.
    def rule_of(reason: str) -> str:
        for key in ("initials only", "name tokens", "marked ambiguous",
                    "surname shared", "publishing span", "division areas"):
            if key in reason:
                return key
        return reason
    by_rule = Counter(rule_of(d.reason) for d in refused)

    docs = affected_documents(decisions) if (args.documents or args.apply) else set()

    if args.json:
        print(json.dumps({
            "version": PROMOTION_VERSION,
            "considered": len(decisions),
            "promoted": len(promoted),
            "refused": len(refused),
            "refused_by_rule": dict(by_rule),
            "documents_affected": len(docs),
            "applied": bool(args.apply),
        }, indent=1))
    else:
        print(f"promotion version : {PROMOTION_VERSION}")
        print(f"names considered  : {len(decisions)}")
        print(f"  would promote   : {len(promoted)}")
        print(f"  refused         : {len(refused)}")
        print()
        print("refused by rule:")
        for rule, n in by_rule.most_common():
            print(f"   {n:>5}  {rule}")
        print()
        print("refused, detailed:")
        for reason, n in reasons.most_common(10):
            print(f"   {n:>5}  {reason}")
        print()
        print(f"example promotions (first {args.limit}):")
        for d in sorted(promoted, key=lambda x: -x.evidence["documents"])[:args.limit]:
            print(f"   {d.surface[:34]:34} {d.reason}")
        if docs:
            print()
            print(f"documents affected: {len(docs)}")

    if not args.apply:
        print("\nDry run: nothing was written. Re-run with --apply.")
        return 0

    changed = apply_promotions(decisions)
    print(f"\nPromoted {changed} author name(s) to author_attested.")
    print(f"Documents needing a knowledge re-run: {len(docs)}")
    out = "reports/post_ingest_audit/evidence/promoted_documents.txt"
    try:
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(sorted(docs)))
        print(f"Wrote the re-run scope to {out}")
    except OSError as exc:
        logger.warning("Could not write the document list: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
