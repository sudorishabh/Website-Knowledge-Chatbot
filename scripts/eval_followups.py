"""Evaluate the "Related questions" shown under answers, against a live /chat.

A suggestion is a promise: click me and you will get an answer. This harness
checks the promise the only way that counts — it clicks. Every suggested
question is asked back through ``/chat`` exactly as the widget asks it, with the
conversation so far as history, and the reply is graded.

What it reports, in plain language:

* **Coverage** — how many answers came with suggestions, and that greetings,
  refusals and out-of-scope questions came with none.
* **Speed** — how long after the answer the suggestions appeared (median, p90),
  and how often they were too late and dropped.
* **Backfire** — a suggestion whose click gets "I don't have information", a
  clarifying question back, or a reply that misses the question. Any reply that
  is not a real answer is asked a second time: the pipeline is not
  deterministic (see ``benchmark_chat.py``), so a suggestion that fails once and
  answers once is *flaky*, and one that fails twice is a *backfire*.
* **Novelty** — whether the suggestion was already answered by the answer it
  sat under.
* **Scope** — broad (a theme or TERI's work in an area), focused (one project,
  report, programme or person) or narrow (a single figure, date or detail).

The grade comes from one model call per suggestion, on the same deployment the
bot uses. It is an instrument, not ground truth: read the backfire list, which
quotes each failing reply in full.

Read-only against every store, as long as the server runs with
``SEMANTIC_CACHE_ENABLED=false``; the server needs ``FOLLOWUPS_ENABLED=true``.

    python scripts/eval_followups.py --base http://127.0.0.1:8011 --label baseline
    python scripts/eval_followups.py --compare reports/followups/baseline.json \\
        reports/followups/tuned.json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

import requests

OUT_DIR = os.path.join("reports", "followups")

#: The questions whose suggestions are evaluated. The start screen's suggested
#: questions (what visitors are invited to ask first), the question-shaped part
#: of the retrieval query set (people, projects, acronyms, themes, a dated
#: report), "list everyone" questions, catalog counts (whose suggestions are
#: fixed requests), and two controls that must get none.
SEEDS: list[tuple[str, str]] = [
    ("start_screen", "What does TERI do?"),
    ("start_screen", "What research does TERI do?"),
    ("start_screen", "What are TERI's latest publications?"),
    ("start_screen", "What is clean energy?"),
    ("start_screen", "What are green buildings?"),
    ("start_screen", "Tell about the director general of TERI"),
    ("start_screen", "What is the mission and vision of TERI?"),
    ("start_screen", "Can you provide a brief history of The Energy and Resources "
                     "Institute (TERI)?"),
    ("start_screen", "Where are TERI's offices located, and how can I contact them?"),
    ("start_screen", "Who are TERI's leading experts and researchers?"),
    ("start_screen", "What are TERI's ongoing projects?"),
    ("start_screen", "What are TERI's flagship initiatives and centres of excellence?"),
    ("theme", "What are the environmental impacts of solar energy?"),
    ("theme", "How does climate change affect water availability in India?"),
    ("theme", "What approaches reduce air pollution in Indian cities?"),
    ("theme", "Why does sustainable agriculture matter for smallholder farmers?"),
    ("theme", "How can buildings be made more energy efficient?"),
    ("acronym", "What is CAAQMS?"),
    ("acronym", "GRIHA rating for green buildings"),
    ("person", "What has Dr Leena Srivastava written about?"),
    ("person", "Dr Debajit Palit energy access"),
    ("project", "Himalayan Adaptation, Water and Resilience research"),
    ("relational", "Which organizations fund TERI's water projects?"),
    ("dated", "What did the 2019 report say about PM2.5 in Delhi NCR?"),
    ("theme", "SDG 7 progress on energy access in rural India"),
    # "List everyone / everything" questions: answers built from directory and
    # index pages, where suggestions used to repeat a row of the list or guess
    # from a job title.
    ("list", "list down all the management people and in what sectors do they work?"),
    ("list", "Who are TERI's Distinguished Fellows?"),
    ("list", "List the members of TERI's Governing Council."),
    ("list", "List TERI's regional centres and what they work on."),
    ("list", "List TERI's centres of excellence."),
    ("list", "List TERI's main areas of work."),
    ("catalog", "How many reports did TERI publish in 2024?"),
    ("catalog", "How many news items mention climate change?"),
    ("catalog", "How many events did TERI hold in 2025?"),
    ("control", "hi"),
    ("control", "What is the capital of France?"),
]


# --------------------------------------------------------------------------
# Talking to /chat
# --------------------------------------------------------------------------

def ask(base: str, question: str, history: list[dict] | None = None,
        timeout: int = 240) -> dict[str, Any]:
    """One /chat call, read the way the widget reads it, with arrival times."""
    started = time.monotonic()
    marks: dict[str, float] = {}
    answer = ""
    sources: dict[str, Any] = {}
    followups: list[str] = []
    error = None
    try:
        with requests.post(f"{base}/chat", stream=True, timeout=timeout,
                           json={"question": question, "history": history or []}) as res:
            res.raise_for_status()
            for line in res.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                event = json.loads(line[5:].strip())
                kind = event.get("type")
                at = round(time.monotonic() - started, 2)
                if kind == "token":
                    marks.setdefault("first_token", at)
                    answer += event.get("text", "")
                elif kind == "correction":
                    answer = event.get("text", "")
                elif kind == "sources":
                    marks["sources"] = at
                    sources = event
                elif kind == "followups":
                    marks["followups"] = at
                    followups = list(event.get("questions") or [])
                elif kind == "done":
                    marks["done"] = at
                elif kind == "error":
                    marks["error"] = at
                    error = "stream error event"
    except Exception as exc:  # network, timeout, bad JSON
        error = f"{type(exc).__name__}: {exc}"
    return {
        "question": question,
        "answer": answer,
        "intent": sources.get("intent"),
        "citations": len(sources.get("citations") or []),
        "clarification": bool(sources.get("clarification")),
        "followups": followups,
        "marks": marks,
        "error": error,
    }


def _refused(answer: str) -> bool:
    from app.generation.sections import is_refusal

    return is_refusal(answer) or not answer.strip()


def outcome(reply: dict[str, Any]) -> str:
    """What a click got, before any grading: an answer, or one of the failures
    that need no judge to recognise."""
    if reply.get("error"):
        return "error"
    if reply.get("clarification"):
        return "clarification"
    if _refused(reply.get("answer") or ""):
        return "refusal"
    return "answered"


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------

_JUDGE_SYSTEM = (
    "You audit the 'related questions' a chatbot for TERI (The Energy and "
    "Resources Institute) suggests under its answers. You are given the "
    "visitor's original question, the answer they were shown, one suggested "
    "question, and the chatbot's reply when that suggestion was clicked.\n"
    "Judge three things.\n"
    "1. answered: does the reply actually answer the suggested question with "
    "substance? 'yes' = it answers what was asked; 'partly' = it gives related "
    "material but leaves the core of the question unanswered, or says the "
    "specific thing is not available; 'no' = it does not answer it (a refusal, "
    "a different topic, or only a generic description).\n"
    "2. already_answered: was the suggested question already answered by the "
    "ORIGINAL answer, so clicking it adds nothing new? true or false.\n"
    "3. scope of the suggested question: 'broad' = a theme, a field or TERI's "
    "work in an area as a whole; 'focused' = one named project, report, "
    "programme, event, centre or person, asked about openly; 'narrow' = a "
    "single figure, date, number, location or detail.\n"
    "Give a one-sentence reason for the 'answered' verdict."
)


def _judge_model():
    from pydantic import BaseModel

    from app.core.clients.llm import get_llm

    class Verdict(BaseModel):
        answered: Literal["yes", "partly", "no"]
        already_answered: bool
        scope: Literal["broad", "focused", "narrow"]
        reason: str

    return get_llm(temperature=0).with_structured_output(Verdict)


def judge(model, seed: dict[str, Any], suggestion: str,
          reply: dict[str, Any]) -> dict[str, Any]:
    prompt = (
        f"Original question: {seed['question']}\n\n"
        f"Answer shown:\n{seed['answer'][:3000]}\n\n"
        f"Suggested question: {suggestion}\n\n"
        f"Reply when the suggestion was clicked:\n{(reply.get('answer') or '')[:3000]}"
    )
    try:
        verdict = model.invoke([("system", _JUDGE_SYSTEM), ("human", prompt)])
        return verdict.model_dump()
    except Exception as exc:
        return {"answered": None, "already_answered": None, "scope": None,
                "reason": f"judge failed: {type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def _gap(marks: dict[str, float]) -> float | None:
    if "followups" in marks and "sources" in marks:
        return round(marks["followups"] - marks["sources"], 2)
    return None


def run(base: str, label: str, workers: int,
        questions: list[tuple[str, str]] = SEEDS) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{label}.json")
    started = time.time()

    print(f"[1/4] Asking {len(questions)} seed questions ...", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        seeds = list(pool.map(lambda s: {"kind": s[0], **ask(base, s[1])}, questions))
    for seed in seeds:
        seed["outcome"] = outcome(seed)
        seed["chips_after_answer_s"] = _gap(seed["marks"])

    clicks = [(i, q) for i, seed in enumerate(seeds) for q in seed["followups"]]
    print(f"[2/4] Clicking {len(clicks)} suggestions ...", flush=True)

    def click(item: tuple[int, str]) -> dict[str, Any]:
        i, suggestion = item
        seed = seeds[i]
        history = [{"role": "user", "content": seed["question"]},
                   {"role": "assistant", "content": seed["answer"]}]
        reply = ask(base, suggestion, history)
        reply["outcome"] = outcome(reply)
        return {"seed": i, "suggestion": suggestion, "history": history, "reply": reply}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(click, clicks))

    print(f"[3/4] Grading {len(results)} replies ...", flush=True)
    model = _judge_model()
    # Grading calls are small, so twice the chat concurrency — but tied to it,
    # so --workers also slows grading when the deployment is rate-limiting.
    with ThreadPoolExecutor(max_workers=workers * 2) as pool:
        verdicts = list(pool.map(
            lambda r: judge(model, seeds[r["seed"]], r["suggestion"], r["reply"]),
            results))
    for result, verdict in zip(results, verdicts):
        result["verdict"] = verdict

    retry = [r for r in results if not _good(r)]
    print(f"[4/4] Asking the {len(retry)} that did not get a real answer again ...",
          flush=True)

    def again(result: dict[str, Any]) -> None:
        reply = ask(base, result["suggestion"], result["history"])
        reply["outcome"] = outcome(reply)
        result["retry"] = {"reply": reply,
                           "verdict": judge(model, seeds[result["seed"]],
                                            result["suggestion"], reply)}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(again, retry))

    for result in results:
        result["status"] = _status(result)
        result.pop("history", None)

    data = {"label": label, "base": base, "ran_at": time.strftime("%Y-%m-%d %H:%M"),
            "minutes": round((time.time() - started) / 60, 1),
            "seeds": seeds, "clicks": results, "summary": summarize(seeds, results)}
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    report_path = path[:-5] + ".md"
    with io.open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report(data))
    print(f"Wrote {path} and {report_path}")
    return path


def _good(result: dict[str, Any]) -> bool:
    return (result["reply"]["outcome"] == "answered"
            and result["verdict"].get("answered") in ("yes", None))


def _status(result: dict[str, Any]) -> str:
    """answered / partly / flaky / backfire / error, for one suggestion.

    "error" is a click the server failed on both times (a rate limit, a
    timeout) — it says nothing about the suggestion, so it is kept out of the
    backfire count rather than inflating it."""
    if _good(result):
        return "answered"
    second = result.get("retry")
    if (result["reply"]["outcome"] == "error"
            and (not second or second["reply"]["outcome"] == "error")):
        return "error"
    first_partly = (result["reply"]["outcome"] == "answered"
                    and result["verdict"].get("answered") == "partly")
    second = result.get("retry")
    if second:
        reply, verdict = second["reply"], second["verdict"]
        if reply["outcome"] == "answered" and verdict.get("answered") in ("yes", None):
            return "flaky"
        if first_partly or (reply["outcome"] == "answered"
                            and verdict.get("answered") == "partly"):
            return "partly"
    return "partly" if first_partly else "backfire"


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "0/0"


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def _fixed(suggestion: str) -> bool:
    """A catalog answer's fixed request ("List them", "Break them down by
    year"), not a generated suggestion. Told apart by its words, not by the
    seed's kind: a count question is sometimes answered in prose, and then its
    suggestions are generated ones like any other."""
    return suggestion == "List them" or suggestion.startswith("Break them down by ")


def _statuses(clicks: list[dict]) -> dict[str, int]:
    return {k: sum(1 for c in clicks if c["status"] == k)
            for k in ("answered", "flaky", "partly", "backfire", "error")}


def summarize(seeds: list[dict], clicks: list[dict]) -> dict[str, Any]:
    generated = [s for s in seeds if s["kind"] not in ("catalog", "control")
                 and s["outcome"] == "answered"]
    catalog = [s for s in seeds if s["kind"] == "catalog"]
    controls = [s for s in seeds if s["kind"] == "control"]
    gaps = [s["chips_after_answer_s"] for s in generated
            if s["chips_after_answer_s"] is not None]
    late = [s for s in generated if not s["followups"]
            and s["marks"].get("done", 0) - s["marks"].get("sources", 0) >= 5.5]
    gen_clicks = [c for c in clicks if not _fixed(c["suggestion"])]
    status = _statuses(gen_clicks)
    scope = {k: sum(1 for c in gen_clicks if c["verdict"].get("scope") == k)
             for k in ("broad", "focused", "narrow")}
    lists = [s for s in generated if s["kind"] == "list"]
    list_clicks = [c for c in gen_clicks if seeds[c["seed"]]["kind"] == "list"]
    return {
        "list_answers": len(lists),
        "list_with_followups": sum(1 for s in lists if s["followups"]),
        "list_suggestions": len(list_clicks),
        "list_status": _statuses(list_clicks),
        "list_already_answered": sum(1 for c in list_clicks
                                     if c["verdict"].get("already_answered")),
        "generated_answers": len(generated),
        "generated_with_followups": sum(1 for s in generated if s["followups"]),
        "late_dropped": len(late),
        "chips_after_answer_median_s": st.median(gaps) if gaps else None,
        "chips_after_answer_p90_s": _quantile(gaps, 0.9),
        "catalog_with_followups": sum(1 for s in catalog if s["followups"]),
        "catalog": len(catalog),
        "controls_with_followups": sum(1 for s in controls if s["followups"]),
        "suggestions": len(gen_clicks),
        "status": status,
        "already_answered": sum(1 for c in gen_clicks
                                if c["verdict"].get("already_answered")),
        "scope": scope,
        "catalog_clicks": [{"suggestion": c["suggestion"], "status": c["status"]}
                           for c in clicks if _fixed(c["suggestion"])],
        "seed_answer_outcomes": {k: sum(1 for s in seeds if s["outcome"] == k)
                                 for k in ("answered", "refusal", "clarification",
                                           "error")},
    }


def report(data: dict[str, Any]) -> str:
    s, seeds = data["summary"], data["seeds"]
    n = s["suggestions"]
    st_ = s["status"]
    lines = [
        f"# Follow-up questions evaluation — {data['label']}",
        "",
        f"Run {data['ran_at']} against {data['base']}, {data['minutes']} minutes. "
        f"{len(seeds)} questions asked; every suggestion clicked with the "
        "conversation as history, and any that did not get a real answer clicked "
        "a second time.",
        "",
        "## In plain language",
        "",
        f"- **Suggestions offered:** {_pct(s['generated_with_followups'], s['generated_answers'])}"
        f" of normal answers came with suggestions; {s['late_dropped']} were dropped for "
        "arriving too late.",
        f"- **Catalog counts:** {s['catalog_with_followups']}/{s['catalog']} came with "
        "'List them / Break them down' suggestions.",
        f"- **Controls** (a greeting, an out-of-scope question): "
        f"{s['controls_with_followups']} came with suggestions (should be 0).",
        f"- **Speed:** suggestions appeared a median {s['chips_after_answer_median_s']} s "
        f"after the answer (90% within {s['chips_after_answer_p90_s']} s).",
        f"- **Clicked suggestions that got a real answer:** {_pct(st_['answered'], n)}; "
        f"another {st_['flaky']} answered only on the second try.",
        f"- **Half-answered** (related material, but not what was asked): {st_['partly']}.",
        f"- **Backfired** (refused, asked to clarify, or missed the question twice): "
        f"{_pct(st_['backfire'], n)}.",
        f"- **Already answered by the answer above them:** {_pct(s['already_answered'], n)}.",
        f"- **How specific:** broad {s['scope']['broad']}, focused {s['scope']['focused']}, "
        f"narrow {s['scope']['narrow']}.",
    ]
    if s.get("list_answers"):
        ls = s["list_status"]
        lines.append(
            f"- **'List everyone' questions:** {s['list_with_followups']}/{s['list_answers']} "
            f"came with suggestions; of their {s['list_suggestions']} suggestions, "
            f"{ls['answered']} got a real answer, {ls['flaky']} only on a second try, "
            f"{ls['partly']} half-answered, {ls['backfire']} backfired, and "
            f"{s['list_already_answered']} repeated the list above them.")
    lines.append("")
    if s["catalog_clicks"]:
        lines += ["Catalog suggestions clicked: " + "; ".join(
            f"'{c['suggestion']}' → {c['status']}" for c in s["catalog_clicks"]), ""]

    bad = [c for c in data["clicks"] if c["status"] in ("backfire", "partly", "flaky")]
    if bad:
        lines += ["## Suggestions that did not cleanly work", ""]
        for c in bad:
            seed = seeds[c["seed"]]
            reply = c["reply"]
            lines += [
                f"### {c['status'].upper()}: {c['suggestion']}",
                f"- Under: *{seed['question']}*",
                f"- First click: {reply['outcome']}, judged '{c['verdict'].get('answered')}' "
                f"— {c['verdict'].get('reason')}",
            ]
            if c.get("retry"):
                r2 = c["retry"]
                lines.append(f"- Second click: {r2['reply']['outcome']}, judged "
                             f"'{r2['verdict'].get('answered')}' — {r2['verdict'].get('reason')}")
            head = " ".join((reply.get("answer") or "").split())[:500]
            lines += [f"- Reply: {head}{'…' if len(reply.get('answer') or '') > 500 else ''}", ""]

    lines += ["## Every question and its suggestions", "",
              "| Question | Answer | Suggestions (status, scope) | Appeared after |",
              "| --- | --- | --- | --- |"]
    by_seed: dict[int, list[dict]] = {}
    for c in data["clicks"]:
        by_seed.setdefault(c["seed"], []).append(c)
    for i, seed in enumerate(seeds):
        chips = "<br>".join(
            f"{c['suggestion']} (*{c['status']}, {c['verdict'].get('scope')}*"
            f"{', repeat' if c['verdict'].get('already_answered') else ''})"
            for c in by_seed.get(i, [])) or "—"
        gap = seed["chips_after_answer_s"]
        lines.append(f"| {seed['question']} | {seed['outcome']} | {chips} | "
                     f"{'' if gap is None else f'{gap} s'} |")
    return "\n".join(lines) + "\n"


def compare(paths: list[str]) -> None:
    runs = [json.load(io.open(p, encoding="utf-8")) for p in paths]
    for r in runs:
        # Recounted with today's rules, so runs saved by an older version of
        # this script are measured the same way as new ones.
        for c in r["clicks"]:
            c["status"] = _status(c)
        r["summary"] = summarize(r["seeds"], r["clicks"])
    rows = [
        ("Normal answers with suggestions",
         lambda s: _pct(s["generated_with_followups"], s["generated_answers"])),
        ("Dropped as too late", lambda s: s["late_dropped"]),
        ("Median wait after answer (s)", lambda s: s["chips_after_answer_median_s"]),
        ("90th percentile wait (s)", lambda s: s["chips_after_answer_p90_s"]),
        ("Suggestions clicked", lambda s: s["suggestions"]),
        ("Real answer on first click",
         lambda s: _pct(s["status"]["answered"], s["suggestions"])),
        ("Answered only on retry", lambda s: s["status"]["flaky"]),
        ("Half-answered", lambda s: s["status"]["partly"]),
        ("Backfired", lambda s: _pct(s["status"]["backfire"], s["suggestions"])),
        ("Server errors (not judged)", lambda s: s["status"].get("error", 0)),
        ("Questions that errored", lambda s: s["seed_answer_outcomes"]["error"]),
        ("Already answered above", lambda s: _pct(s["already_answered"], s["suggestions"])),
        ("Broad / focused / narrow",
         lambda s: f"{s['scope']['broad']} / {s['scope']['focused']} / {s['scope']['narrow']}"),
        ("Controls with suggestions", lambda s: s["controls_with_followups"]),
        ("List answers with suggestions",
         lambda s: _pct(s["list_with_followups"], s["list_answers"])),
        ("List suggestions: real answer / repeat",
         lambda s: (f"{s['list_status']['answered']}/{s['list_suggestions']}"
                    f" / {s['list_already_answered']}") if s["list_suggestions"] else "—"),
    ]
    print("| Measure | " + " | ".join(r["label"] for r in runs) + " |")
    print("| --- |" + " --- |" * len(runs))
    for name, get in rows:
        print(f"| {name} | " + " | ".join(str(get(r["summary"])) for r in runs) + " |")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--label", default="run")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--compare", nargs="+", metavar="RUN_JSON")
    parser.add_argument("--kinds", nargs="+", metavar="KIND",
                        help="only the seeds of these kinds (e.g. list), for a quick "
                             "look at one kind of question")
    args = parser.parse_args()
    if args.compare:
        compare(args.compare)
        return 0
    sys.path.insert(0, os.getcwd())
    seeds = [s for s in SEEDS if not args.kinds or s[0] in args.kinds]
    run(args.base.rstrip("/"), args.label, args.workers, seeds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
