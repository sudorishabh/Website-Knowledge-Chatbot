# Query Intelligence Layer — evaluation report

Branch `feature/query-intelligence-layer`, Phases A–E.
Harness: `tests/evaluation/`. Design notes: `docs/query-intelligence-layer.md`.

> **Read this first.** All tests passing does **not** mean the system answers
> better. It means the machinery behaves as specified and nothing regressed.
> §6 separates what is measured from what is not, and the honest summary is that
> retrieval *recall* is demonstrated, retrieval *precision* and answer quality
> are not.

---

## 1. What the harness is

`tests/evaluation/harness.py` runs one query end to end through a chosen feature
configuration and records what the layer decided, as an `EvalRecord`:

| Field | Phase |
| --- | --- |
| `original`, `normalized`, `clarified`, `clarification_question`, `options`, `clarified_from` | A |
| `temporal_mode`, `temporal_window` | B |
| `requirements`, `subqueries`, `routes` | C |
| `perspectives` | D |
| `candidate_ids`, `evidence_ids`, `scores` | retrieval |

A scenario asserts about the record — what survived — rather than about an
internal call.

**Deterministic by construction.** No LLM, no Qdrant, no MySQL, no network. The
three model calls (query understanding, requirement extraction, perspective
generation) are fixture data; the corpus is hand-built candidates with explicit
relevance scores and dates. Everything between them is the real code: the real
`process`, planner, guards, RRF, reranker and context builder.

---

## 2. The feature matrix

Repository flag names, all defaulting **false**:

| `clarification_enabled` | `temporal_intent_enabled` | `subquery_planning_enabled` | `multi_query_enabled` | Row |
| --- | --- | --- | --- | --- |
| OFF | OFF | OFF | OFF | Baseline |
| ON | OFF | OFF | OFF | Clarified |
| OFF | ON | OFF | OFF | Temporal |
| OFF | OFF | ON | OFF | Decomposed |
| OFF | OFF | OFF | ON | Perspective |
| ON | ON | ON | ON | Full stack |

`tests/evaluation/test_feature_matrix.py` runs one query through all six and
asserts **independence**: each flag is on in some row and off in another, one
flag never switches on another, and the full stack is the union of the
individual effects with nothing lost.

---

## 3. Results

| Suite | Tests | Result |
| --- | --- | --- |
| Phase A — clarification | 36 | pass |
| Phase B — temporal | 66 | pass |
| Phase C — decomposition | 29 | pass |
| Phase D — perspectives | 28 | pass |
| Phase E — evaluation harness | 55 | pass |
| **Full deterministic suite** (`-m "not llm"`) | **4419 passed, 27 skipped** | **1 failure, environmental** |

Per-phase counts are for the files that phase owns, measured directly. They are
not disjoint: Phase B's 66 includes the 27 pre-existing `test_temporal_gate.py`
tests that guard its refactor, and Phase D's 28 includes the pre-existing
multi-query tests it re-pointed at the new API.

Growth from the pre-work baseline: 4241 → 4419 (+178 tests). No existing test
was weakened, removed, or had an assertion relaxed. Two test files had
monkeypatch *targets* renamed in Phase D (`paraphrases` → `perspectives`) with
their assertions unchanged.

### Known environmental failure

`tests/knowledge/test_build_knowledge.py::test_dry_run_does_not_project`

```
Stage(name='project', errors=[{'id': 'neo4j', 'error': 'unreachable'}],
      notes=['Neo4j unreachable; MySQL is authoritative...'])
```

It expects a `dry-run` note and gets an `unreachable` note because no Neo4j is
running on this machine. **It fails identically on clean `main` before any of
this work.** Not a regression and not related to the Query Intelligence Layer.

### Regressions

None. Every phase was validated against the prior phase's commit with a
preservation check over `app/ingestion`, `app/knowledge`, `app/catalog`,
`app/core`, `app/schemas`, `app/api`, `ui` and the earlier phases' own modules —
zero files changed in all of them at every step.

---

## 4. Scenario coverage

All 32 brief scenarios, in `tests/evaluation/test_scenarios.py`, numbered to
match:

| Group | Scenarios | Notes |
| --- | --- | --- |
| Clarification | 1–6 | includes catalog-derived options and the loop guard |
| Temporal | 7–12 | includes the no-freshness-bias equivalence check |
| Decomposition / routing | 13–17 | includes per-part routing and de-duplication |
| Perspectives | 18–24 | 18–20 are one measurement: miss → reach → keep |
| Combined | 25–32 | clarification+temporal, decomposition+perspectives, and the two headline trade-offs |

---

## 5. The two failure modes

`tests/evaluation/test_retrieval_quality.py`. These matter more than any other
test here, because a system that only prefers newer evidence passes one and
fails the other — and testing "newer wins" alone would have locked in the bug.

**Failure mode 1 — new but irrelevant evidence wins.**
Old + highly relevant (0.92, 2011) vs new + weakly relevant (0.38, 2026).
Expected and observed: the old, relevant passage leads. Asserted under *all
five* temporal modes, so it is not an artefact of having no temporal intent.

**Failure mode 2 — old but stale evidence wins a current question.**
Two passages at the same relevance (0.80) and the same start year, one closed in
2016 and one still open. For a current-state question the still-open one leads.
A control asserts the same pair is untouched without a current-state question,
so the reordering is attributable to the temporal intent rather than to the
metadata.

**Both together** — `test_relevance_outranks_validity_but_validity_breaks_the_tie`
pins the full policy in one case:

```
current-state question, three passages
  strong-valid  (0.88, open)    -> 1st
  strong-stale  (0.88, closed)  -> 2nd   same relevance band, worse temporal fit
  weak-valid    (0.40, open)    -> 3rd   a relevance band lower; validity cannot lift it
```

---

## 6. What is and is not measured

### Measured — retrieval recall

`test_the_full_stack_recalls_at_least_as_much_as_the_baseline` asserts the
baseline's evidence is a **subset** of the full stack's, and strictly smaller.
Stated as a superset rather than a count deliberately: "more blocks" is not an
improvement if a passage the baseline had was displaced.

The perspective recall test is a matched pair — `test_18_19_20` shows a passage
reaching the final evidence only when the perspective leg runs, and a control
(`test_without_the_perspective_that_passage_is_not_retrieved`, Phase D) shows it
absent when generation returns nothing. The gain is therefore attributable to
the leg, not to wording.

### Measured — correctness and regression protection

The ranking invariant, the clarification loop guard, flag-off equivalence for
every phase, failure fallbacks (LLM down, malformed output, catalog outage,
embedding failure), and the absence of duplicate evidence.

### Behavioural improvements — demonstrated, not quantified

An unclear question now gets a question back instead of small talk; a
current-state question prefers valid evidence; a multi-part question retrieves
each part. These are shown to happen. Their *frequency and value on real
traffic* is not measured.

### Not measured

* **Answer quality.** The harness stops at the evidence. No answer is generated.
* **Precision.** Extra legs add candidates; nothing here shows the additions are
  relevant on a real corpus. Recall without precision can hurt.
* **The model's judgement.** Understanding, requirements and perspectives are
  fixture data. The harness can show that a *good* perspective is kept and a
  *bad* one rejected; it cannot show the LLM proposes good ones.
* **Latency.** Phase C puts requirement extraction on the critical path and
  Phase D adds embeddings per perspective. Not benchmarked.
* **The 86-question organisational benchmark**, which the codebase's own
  docstrings cite, has not been re-run against any configuration.

---

## 7. Parameters to tune

| Parameter | Default | Why it is a guess |
| --- | --- | --- |
| `multi_query_distinct_threshold` | 0.92 | Borrowed from `dedup_cosine_threshold`, never calibrated on query embeddings. First knob if perspectives are over- or under-rejected. |
| `subquery_max` | 3 | A latency/recall trade-off picked by judgement. |
| `multi_query_paraphrases` | 2 | Now counts perspectives; the name is kept for `.env` compatibility. |
| `_TEMPORAL_TOLERANCE` (reranker) | 0.10 | Separates the discrete fit tiers; untested against a wider scale. |
| `clarify.MAX_OPTIONS` | 4 | UX judgement, not measured. |
| Clarification trigger | `clarification_needed` only | `is_ambiguous` is deliberately excluded; whether that is too conservative is an open question. |

---

## 8. Architecture status

No ingestion change, no payload change, no schema change, no new top-level
package, and no new deferred-upward import. The layering test passes unchanged.

| Phase | Module | Integration |
| --- | --- | --- |
| A | `retrieval/understanding/clarify.py` | `process()` + a guard in `_prepare` |
| B | `retrieval/search/temporal_gate.py` (extended) | a band in `reranker`, a gate in `retriever` |
| C | `retrieval/subqueries.py` | planned in `pipeline`, consumed in `retriever` |
| D | `retrieval/search/strategies.py` (rewritten leg) | the existing multi-query call |
| E | `tests/evaluation/` | test-only |

Two things were **reported rather than built**, and both stand:

* **Document-level competing-fact detection** (the brief's original Phase 5).
  `context/builder._conflicting` documents why it stops where it does; going
  further needs a labelled measurement set or an ingest-time `supersedes`
  relation.
* **Per-sub-query MySQL routing.** It would need an operation classifier per
  part — a second decomposition system. `_db_section` remains authoritative for
  catalog retrieval.

---

## 9. Recommended rollout order

Each flag is independent, so enable them one at a time and watch the retrieval
trace between steps.

1. **`temporal_intent_enabled`** — first. Deterministic, no extra LLM call, no
   added latency, and the strongest regression protection in this suite. Lowest
   risk, immediate benefit on current/historical questions.
2. **`multi_query_enabled`** — second. One LLM call that was already budgeted.
   Watch `multi_query_distinct_threshold`: if the trace shows perspectives
   nearly always rejected, lower it; if it shows near-duplicate legs, raise it.
3. **`clarification_enabled`** — third. No latency cost, but it is the only
   change users *see*: a question instead of an answer. Roll out where the
   clarification turn can be observed, and check the `clarifying` /
   `clarified_from` trace fields for unnecessary interruptions.
4. **`subquery_planning_enabled`** — last. It is the only one with a structural
   latency cost: requirement extraction moves from running beside retrieval to
   running ahead of it. Enable once the other three are stable and the added
   round trip has been measured against the recall gain.

Before step 1, re-run the 86-question benchmark at baseline to get a real
quality reference. This report cannot supply one.
