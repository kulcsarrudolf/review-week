#!/usr/bin/env python3
"""
run_cost.py - Measure the token/cost of generating the weekly report.

Run this AFTER the report has been written. It reads the current Claude Code
session transcript and sums the token usage of the assistant turns that make up
this claude-review-week run: from the turn that invoked `extract.py` to the end of the
file. Those turns (the digest read and the report write) are already flushed to
the transcript by the time this runs, so the figure is measured, not estimated.

The only turn it cannot include is the final one that runs this script and
appends the footer, since that turn is still in flight and unwritten. That turn
is a small edit, so the number is a close lower bound. The output says as much.

Reuses PRICE and cost_of from extract.py so pricing lives in one place.

Usage:
    run_cost.py [--session <path>]
"""

import argparse
import collections
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract  # noqa: E402  (reuse per-model pricing, cost_of, PROJECTS_DIR)


def newest_session():
    """The live session transcript: newest top-level *.jsonl (not a subagent)."""
    files = glob.glob(os.path.join(extract.PROJECTS_DIR, "*", "*.jsonl"))
    files = [f for f in files if f"{os.sep}subagents{os.sep}" not in f]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def is_extractor_call(rec):
    """True if this assistant record contains a Bash tool_use running extract.py."""
    m = rec.get("message")
    if not isinstance(m, dict):
        return False
    content = m.get("content")
    if not isinstance(content, list):
        return False
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash":
            cmd = (b.get("input") or {}).get("command", "")
            if "extract.py" in cmd:
                return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None,
                    help="Transcript path (defaults to the live session).")
    args = ap.parse_args()

    path = args.session or newest_session()
    if not path or not os.path.isfile(path):
        json.dump({"error": "no session transcript found"}, sys.stdout)
        sys.stdout.write("\n")
        return

    records = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Start at the last extractor invocation; that marks this claude-review-week run.
    start_idx = 0
    for i, rec in enumerate(records):
        if rec.get("type") == "assistant" and is_extractor_call(rec):
            start_idx = i

    tokens_by_model = collections.defaultdict(lambda: collections.defaultdict(int))
    turns = 0
    for rec in records[start_idx:]:
        if rec.get("type") != "assistant":
            continue
        m = rec.get("message")
        if not isinstance(m, dict):
            continue
        u = m.get("usage")
        if not isinstance(u, dict):
            continue
        turns += 1
        extract.add_usage(tokens_by_model, m.get("model") or "unknown", u)

    tokens = extract.flat_tokens(tokens_by_model)
    total = sum(tokens.values())
    out = {
        "session": os.path.basename(path),
        "assistant_turns_counted": turns,
        "models": sorted(tokens_by_model.keys()),
        "tokens": tokens,
        "total_tokens": total,
        "estimated_cost_usd": round(extract.cost_of(tokens_by_model), 4),
        "note": "measured from the session transcript; per-model pricing; "
                "excludes the final footer-writing turn, so it is a close lower bound",
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
