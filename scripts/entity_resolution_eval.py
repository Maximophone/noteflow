"""
Replay entity resolution over past meetings and measure how well it does.

The user's finished resolutions (``resolved_entities`` in each transcript) are the
ground truth. Meetings are replayed in date order: before each one, the registry
only knows the meetings that came before it, so nothing leaks from the answers.
The AI is given the same mentions the user resolved and the raw AssemblyAI text,
and its answers are compared with the user's.

Then fits the confidence model (entity_confidence.WEIGHTS) and reports how well
its probabilities are calibrated, using predictions made out of fold.

    python scripts/entity_resolution_eval.py --sample 100            # replay + fit
    python scripts/entity_resolution_eval.py --fit-only               # refit from cache

AI answers are cached in --cache, so a rerun only pays for meetings not yet replayed.
The cache holds meeting content, so it lives in the vault, never in this repository.
"""

import argparse
import asyncio
import copy
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from config.paths import PATHS
from config.user_config import USER_NAME
from processors.common.frontmatter import parse_frontmatter_from_content
from processors.notes import entity_confidence
from processors.notes.entity_registry import EntityRegistry, entity_key, load_descriptions, normalize_alias
from processors.notes.entity_resolver import EntityResolver


def load_meetings(transcripts: Path):
    meetings = []
    for path in sorted(transcripts.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "resolved_entities:" not in text:
            continue
        fm = parse_frontmatter_from_content(text) or {}
        if fm.get("category") != "meeting" or not fm.get("resolved_entities"):
            continue
        mapping = fm.get("final_speaker_mapping") or {}
        attendees = sorted({v["person_id"] for v in mapping.values() if isinstance(v, dict) and v.get("person_id")})
        meetings.append({
            "file": path.name,
            "date": str(fm.get("date", "")),
            "resolved": fm["resolved_entities"],
            "attendees": attendees,
            "mapping": mapping,
            "json": fm.get("json_data"),
        })
    return sorted(meetings, key=lambda m: (m["date"], m["file"]))


def raw_transcript(transcripts: Path, meeting) -> str:
    """The transcript as the resolver would have seen it, before any links."""
    data = json.loads((transcripts / meeting["json"]).read_text(encoding="utf-8"))
    names = {k.replace("Speaker ", ""): v.get("name", k) for k, v in meeting["mapping"].items() if isinstance(v, dict)}
    lines = [f"{names.get(u['speaker'], 'Speaker ' + u['speaker'])}: {u['text']}" for u in data.get("utterances") or []]
    return "\n\n".join(lines) or data.get("text", "")


def truth_of(meeting):
    """detected name -> the user's final link ('' when left unlinked)."""
    return {e["detected_name"]: (e.get("resolved_link") or "") for e in meeting["resolved"] if e.get("detected_name")}


async def replay(args, meetings, transcripts):
    cache = {}
    if args.cache.exists():
        for line in args.cache.read_text().splitlines():
            row = json.loads(line)
            cache[row["file"]] = row

    eligible = [m for m in meetings if m["date"] >= args.since and m["json"] and (transcripts / m["json"]).exists()]
    random.Random(args.seed).shuffle(eligible)
    sample = {m["file"] for m in eligible[:args.sample]}
    todo = [m["file"] for m in meetings if m["file"] in sample and m["file"] not in cache]
    print(f"{len(sample)} sampled meetings, {len(todo)} not cached yet")

    resolver = EntityResolver(PATHS.transcriptions)
    registry = EntityRegistry.rebuild(Path("/dev/null"), f"[[{USER_NAME}]]", [])
    if args.descriptions:
        registry.curated = load_descriptions(args.descriptions)
    sem = asyncio.Semaphore(args.concurrency)

    async def run(meeting, snapshot):
        async with sem:
            mentions = [{"detected_name": n} for n in truth_of(meeting)]
            try:
                results = await resolver._resolve_mentions(
                    snapshot, raw_transcript(transcripts, meeting), meeting["attendees"], mentions
                )
            except Exception as e:
                print(f"  failed {meeting['file']}: {e}")
                return
            row = {"file": meeting["file"], "date": meeting["date"], "results": results}
            with args.cache.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            cache[meeting["file"]] = row
            print(f"  done {meeting['file']} ({len(results)} mentions)")

    tasks = []
    for meeting in meetings:
        if meeting["file"] in todo:
            tasks.append(asyncio.create_task(run(meeting, copy.deepcopy(registry))))
        registry.record_meeting(meeting["file"], meeting["date"], meeting["resolved"], meeting["attendees"])
    if tasks:
        await asyncio.gather(*tasks)
    return [cache[f] for f in sample if f in cache]


def history_baseline(meetings):
    """Accuracy of simply reusing each spelling's most frequent past link."""
    registry = EntityRegistry.rebuild(Path("/dev/null"), f"[[{USER_NAME}]]", [])
    seen = right = 0
    for meeting in meetings:
        for name, link in truth_of(meeting).items():
            alias_ev = registry._index().get(normalize_alias(name))
            if alias_ev:
                top = max(alias_ev.items(), key=lambda kv: kv[1])[0]
                seen += 1
                right += top == entity_key(link)
        registry.record_meeting(meeting["file"], meeting["date"], meeting["resolved"], meeting["attendees"])
    return seen, right


def fit_logistic(X, y, l2=1.0, iters=3000, lr=0.1):
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        grad = X.T @ (p - y) / len(y) + l2 * np.r_[0, w[1:]] / len(y)
        w -= lr * grad
    return w


def report(rows, meetings_by_file):
    X, y, llm, groups = [], [], [], []
    for row in rows:
        truth = truth_of(meetings_by_file[row["file"]])
        for r in row["results"]:
            gt = truth.get(r["detected_name"])
            if gt is None:
                continue
            X.append(entity_confidence.vector(r["features"]))
            y.append(float(entity_key(r["resolved_link"]) == entity_key(gt)))
            llm.append(r["llm_probability"])
            groups.append(row["file"])
    X, y, llm = np.array(X), np.array(y), np.array(llm)
    print(f"\n{len(y)} mentions from {len(set(groups))} meetings; AI accuracy {y.mean():.3f}")

    # Out-of-fold probabilities, folds split by meeting
    files = sorted(set(groups))
    random.Random(0).shuffle(files)
    fold_of = {f: i % 5 for i, f in enumerate(files)}
    folds = np.array([fold_of[g] for g in groups])
    oof = np.zeros(len(y))
    for k in range(5):
        w = fit_logistic(X[folds != k], y[folds != k])
        oof[folds == k] = 1 / (1 + np.exp(-X[folds == k] @ w))

    for label, p in [("AI stated probability", llm), ("calibrated model (out of fold)", oof)]:
        brier = np.mean((p - y) ** 2)
        print(f"\n== {label}: Brier {brier:.4f}")
        print("  bin          n   mean p   observed")
        edges = [0, 0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99, 1.0001]
        for lo, hi in zip(edges, edges[1:]):
            m = (p >= lo) & (p < hi)
            if m.sum():
                print(f"  [{lo:.2f},{min(hi,1):.2f})  {m.sum():5d}   {p[m].mean():.3f}    {y[m].mean():.3f}")

    print("\n== Auto-accept threshold (calibrated model): share auto-accepted, errors let through")
    for t in [0.8, 0.9, 0.95, 0.97, 0.98, 0.99]:
        m = oof >= t
        errs = int((m & (y == 0)).sum())
        print(f"  >= {t:.2f}: {m.mean():.1%} auto-accepted, {errs} wrong of {m.sum()} ({errs / max(m.sum(), 1):.2%}); "
              f"{(~m).sum()} to review, which hold {int(((~m) & (y == 0)).sum())} of {int((y == 0).sum())} errors")

    w = fit_logistic(X, y)
    print("\nWEIGHTS = {")
    for name, value in zip(entity_confidence.FEATURES, w):
        print(f'    "{name}": {value:.3f},')
    print("}")


def write_audit(rows, meetings_by_file, path: Path, min_probability: float = 0.85):
    """List past answers the AI confidently disagrees with: likely historical mistakes.

    Written to the vault for the user to check, since it quotes meeting content.
    """
    lines = [
        "# Entity Resolution Audit",
        "",
        "Past resolutions that the replayed resolver confidently disagrees with.",
        "Some are the resolver's mistakes, some are mistakes in the history.",
        "Fix the wrong ones in the transcript's `resolved_entities`, then delete",
        "`entity_registry.json` so it is rebuilt from the corrected history.",
        "",
        "| Meeting | Mention | Past answer | Resolver says | AI prob. |",
        "|---|---|---|---|---|",
    ]
    found = 0
    for row in sorted(rows, key=lambda r: r["date"]):
        truth = truth_of(meetings_by_file[row["file"]])
        for r in row["results"]:
            gt = truth.get(r["detected_name"])
            if gt is None or entity_key(r["resolved_link"]) == entity_key(gt):
                continue
            if r["llm_probability"] < min_probability:
                continue
            found += 1
            meeting = f"[[{row['file'][:-3]}]]"
            lines.append(
                f"| {meeting} | {r['detected_name']} | {gt or '(no link)'} | "
                f"{r['resolved_link'] or '(no link)'} | {r['llm_probability']:.2f} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{found} confident disagreements written to {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", type=int, default=100)
    parser.add_argument("--since", default="2026-03-01")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--cache", type=Path, default=PATHS.vault_noteflow_path / "entity_eval_cache.jsonl")
    parser.add_argument("--fit-only", action="store_true")
    parser.add_argument("--descriptions", type=Path, default=None,
                        help="Entity Descriptions table to give the resolver; for a fair replay, one "
                             "prefilled with --cutoff no later than --since")
    parser.add_argument("--audit", type=Path, default=PATHS.vault_noteflow_path / "Entity Resolution Audit.md",
                        help="where to list past answers the AI confidently disagrees with")
    args = parser.parse_args()

    transcripts = PATHS.transcriptions
    meetings = load_meetings(transcripts)
    print(f"{len(meetings)} resolved meetings")
    seen, right = history_baseline(meetings)
    print(f"History-only baseline: spelling seen before for {seen} mentions, most frequent link right {right / seen:.3f}")

    if args.fit_only:
        rows = [json.loads(l) for l in args.cache.read_text().splitlines()]
    else:
        rows = asyncio.run(replay(args, meetings, transcripts))
    by_file = {m["file"]: m for m in meetings}
    report(rows, by_file)
    write_audit(rows, by_file, args.audit)


if __name__ == "__main__":
    main()
