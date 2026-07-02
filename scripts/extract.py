#!/usr/bin/env python3
"""
extract.py - Parse Claude Code session transcripts into a compact JSON digest.

Reads JSONL transcripts under ~/.claude/projects/<slug>/*.jsonl (and
.../subagents/*.jsonl), filters to a time window, and emits one JSON object on
stdout with per-project metrics, token/cost estimates, PR outcomes, sampled
user prompts, rework signals, and open-thread flags.

Pure stdlib. No third-party dependencies.

Usage:
    extract.py [--since 7d] [--repos-todo]
    extract.py --since 14d
    extract.py --since 2026-06-25..2026-07-02
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# --- Pricing (USD per 1M tokens). Opus 4.8 rates; adjust if pricing changes. ---
# Cost is a rough estimate only. Cache-write billed at 5m-cache rate (1.25x input),
# cache-read at 0.1x input. Base input/output from the claude-api reference.
PRICE = {
    "input": 5.0,
    "output": 25.0,
    "cache_read": 0.50,
    "cache_write": 6.25,
}

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

# User-prompt noise: injected reminders and tool results we should not treat as
# real user intent when sampling prompts.
NOISE_PATTERNS = [
    re.compile(r"<system-reminder>", re.I),
    re.compile(r"<command-name>", re.I),
    re.compile(r"^\s*Caveat:", re.I),
    re.compile(r"tool_use_error", re.I),
]

# Rework / correction signals in user prompts.
REWORK_RE = re.compile(
    r"\b(no,|nope|actually|instead|revert|undo|that'?s wrong|that is wrong|"
    r"re-?do|redo|not what i|don'?t do that|stop|wait,|rollback|roll back|"
    r"you broke|still (?:broken|failing|wrong)|try again)\b",
    re.I,
)


def parse_window(since):
    """Return (start_dt, end_dt) in UTC from a --since argument."""
    now = datetime.now(timezone.utc)
    if since and ".." in since:
        a, b = since.split("..", 1)
        start = datetime.fromisoformat(a).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(b).replace(tzinfo=timezone.utc) + timedelta(days=1)
        return start, end
    m = re.fullmatch(r"(\d+)d", since or "7d")
    days = int(m.group(1)) if m else 7
    return now - timedelta(days=days), now


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def project_name(slug):
    """Turn a dir slug like -Users-kulcsarrudolf-Projects-igemag-ai into a name."""
    marker = "-Projects-"
    if marker in slug:
        return slug.split(marker, 1)[1] or slug
    return slug.lstrip("-").split("-")[-1] or slug


def text_of(content):
    """Flatten a message content (str or list of blocks) into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(parts)
    return ""


def is_noise(text):
    return any(p.search(text) for p in NOISE_PATTERNS)


def is_tool_result_only(content):
    """True when a user message is purely tool_result blocks (no real prompt)."""
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if blocks and all(b.get("type") == "tool_result" for b in blocks):
            return True
    return False


def blank_project():
    return {
        "sessions": set(),
        "active_days": set(),
        "first": None,
        "last": None,
        "tools": defaultdict(int),
        "tokens": defaultdict(int),
        "models": defaultdict(int),
        "prs": {},                # prUrl -> repo
        "branches": set(),
        "plan_mode_msgs": 0,
        "total_asst_msgs": 0,
        "agent_calls": 0,
        "titles": {},             # sessionId -> title
        "prompts": [],            # (ts, text)
        "rework_hits": 0,
        "cwd": None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="7d",
                    help="Window: 'Nd' (e.g. 7d) or 'YYYY-MM-DD..YYYY-MM-DD'.")
    ap.add_argument("--repos-todo", action="store_true",
                    help="Also scan active git repos for recent TODO/FIXME (heavier).")
    args = ap.parse_args()

    start, end = parse_window(args.since)

    projects = defaultdict(blank_project)
    # Per-session tracking for open-thread heuristic.
    session_last = {}   # sessionId -> (ts, role, is_question, project, title, cwd)
    malformed = 0
    files_scanned = 0

    pattern_main = os.path.join(PROJECTS_DIR, "*", "*.jsonl")
    pattern_sub = os.path.join(PROJECTS_DIR, "*", "*", "subagents", "*.jsonl")
    files = glob.glob(pattern_main) + glob.glob(pattern_sub)

    for path in files:
        # Cheap pre-filter: skip files untouched since the window started.
        try:
            if datetime.fromtimestamp(os.path.getmtime(path), timezone.utc) < start:
                continue
        except OSError:
            continue
        files_scanned += 1

        # Derive project slug from the path (dir directly under PROJECTS_DIR).
        rel = os.path.relpath(path, PROJECTS_DIR)
        slug = rel.split(os.sep, 1)[0]
        pname = project_name(slug)

        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue

        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue

                rtype = d.get("type")
                sid = d.get("sessionId")
                ts = parse_ts(d.get("timestamp"))

                # Session-level metadata records (no timestamp filtering needed).
                if rtype == "ai-title" and sid:
                    projects[pname]["titles"].setdefault(sid, d.get("aiTitle"))
                    continue
                if rtype == "custom-title" and sid:
                    # custom title wins over ai title
                    if d.get("customTitle"):
                        projects[pname]["titles"][sid] = d.get("customTitle")
                    continue
                if rtype == "pr-link":
                    pts = parse_ts(d.get("timestamp"))
                    if pts is None or start <= pts < end:
                        url = d.get("prUrl")
                        if url:
                            projects[pname]["prs"][url] = d.get("prRepository")
                    continue

                # Message records: filter by timestamp window.
                if ts is not None and not (start <= ts < end):
                    continue

                m = d.get("message")
                if not isinstance(m, dict):
                    continue

                p = projects[pname]
                if d.get("cwd") and not p["cwd"]:
                    p["cwd"] = d.get("cwd")
                if sid:
                    p["sessions"].add(sid)
                if ts:
                    p["active_days"].add(ts.date().isoformat())
                    if p["first"] is None or ts < p["first"]:
                        p["first"] = ts
                    if p["last"] is None or ts > p["last"]:
                        p["last"] = ts
                if d.get("gitBranch"):
                    p["branches"].add(d["gitBranch"])
                if d.get("permissionMode") == "plan":
                    p["plan_mode_msgs"] += 1

                role = m.get("role")
                content = m.get("content")

                if role == "assistant":
                    p["total_asst_msgs"] += 1
                    if m.get("model"):
                        p["models"][m["model"]] += 1
                    u = m.get("usage") or {}
                    p["tokens"]["input"] += u.get("input_tokens", 0) or 0
                    p["tokens"]["output"] += u.get("output_tokens", 0) or 0
                    p["tokens"]["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
                    p["tokens"]["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                name = b.get("name", "?")
                                p["tools"][name] += 1
                                if name == "Agent":
                                    p["agent_calls"] += 1
                    # open-thread: assistant ending in a question
                    txt = text_of(content).strip()
                    is_q = txt.endswith("?")
                    if sid:
                        session_last[sid] = (ts, "assistant", is_q, pname,
                                             p["titles"].get(sid), p["cwd"])

                elif role == "user":
                    if is_tool_result_only(content):
                        # tool result, not a real prompt; still marks session activity
                        if sid:
                            session_last[sid] = (ts, "tool_result", False, pname,
                                                 p["titles"].get(sid), p["cwd"])
                        continue
                    txt = text_of(content).strip()
                    if txt and not is_noise(txt):
                        p["prompts"].append((ts.isoformat() if ts else "", txt))
                        if REWORK_RE.search(txt):
                            p["rework_hits"] += 1
                    if sid:
                        session_last[sid] = (ts, "user", False, pname,
                                             p["titles"].get(sid), p["cwd"])

    # --- Build open-threads list ---
    open_threads = []
    for sid, (ts, role, is_q, pname, title, cwd) in session_last.items():
        reason = None
        if role == "user":
            reason = "ended on a user prompt with no assistant completion"
        elif role == "assistant" and is_q:
            reason = "ended on an assistant question"
        if reason:
            open_threads.append({
                "project": pname,
                "session": sid,
                "title": title,
                "last_activity": ts.isoformat() if ts else None,
                "reason": reason,
            })
    open_threads.sort(key=lambda x: x["last_activity"] or "", reverse=True)

    # --- Assemble per-project output ---
    totals = defaultdict(int)
    total_cost = 0.0
    per_project = []
    for pname, p in projects.items():
        if not p["sessions"] and not p["prompts"]:
            continue
        tok = p["tokens"]
        cost = (
            tok["input"] / 1e6 * PRICE["input"]
            + tok["output"] / 1e6 * PRICE["output"]
            + tok["cache_read"] / 1e6 * PRICE["cache_read"]
            + tok["cache_write"] / 1e6 * PRICE["cache_write"]
        )
        total_cost += cost
        for k, v in tok.items():
            totals[k] += v

        # Sample prompts: first, longest, last 3 (deduped, capped).
        prompts = sorted(p["prompts"], key=lambda x: x[0])
        sample = []
        seen = set()

        def add(item):
            key = item[1][:200]
            if key not in seen:
                seen.add(key)
                sample.append({"ts": item[0], "text": item[1][:800]})

        if prompts:
            add(prompts[0])
            add(max(prompts, key=lambda x: len(x[1])))
            for it in prompts[-3:]:
                add(it)

        per_project.append({
            "project": pname,
            "cwd": p["cwd"],
            "sessions": len(p["sessions"]),
            "active_days": sorted(p["active_days"]),
            "first_activity": p["first"].isoformat() if p["first"] else None,
            "last_activity": p["last"].isoformat() if p["last"] else None,
            "tools": dict(sorted(p["tools"].items(), key=lambda x: -x[1])),
            "tokens": dict(tok),
            "estimated_cost_usd": round(cost, 2),
            "models": dict(p["models"]),
            "prs": [{"url": u, "repo": r} for u, r in p["prs"].items()],
            "branches": sorted(b for b in p["branches"] if b),
            "plan_mode_msgs": p["plan_mode_msgs"],
            "assistant_msgs": p["total_asst_msgs"],
            "agent_calls": p["agent_calls"],
            "rework_hits": p["rework_hits"],
            "prompt_count": len(p["prompts"]),
            "titles": list({t for t in p["titles"].values() if t}),
            "sampled_prompts": sample,
        })

    per_project.sort(key=lambda x: x["sessions"], reverse=True)

    # --- Optional: recent TODO/FIXME scan in active repos ---
    repo_todos = []
    if args.repos_todo:
        since_git = start.strftime("%Y-%m-%d")
        seen_cwd = set()
        for pp in per_project:
            cwd = pp.get("cwd")
            if not cwd or cwd in seen_cwd or not os.path.isdir(cwd):
                continue
            seen_cwd.add(cwd)
            if not os.path.isdir(os.path.join(cwd, ".git")):
                continue
            try:
                out = subprocess.run(
                    ["git", "-C", cwd, "log", "--since", since_git,
                     "-p", "--diff-filter=AM", "-G", "TODO|FIXME"],
                    capture_output=True, text=True, timeout=20,
                )
                hits = [l[1:].strip() for l in out.stdout.splitlines()
                        if l.startswith("+") and re.search(r"TODO|FIXME", l)]
                if hits:
                    repo_todos.append({"project": pp["project"],
                                       "todos": hits[:15]})
            except (subprocess.SubprocessError, OSError):
                continue

    digest = {
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "since_arg": args.since},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "projects": len(per_project),
            "sessions": sum(pp["sessions"] for pp in per_project),
            "tokens": dict(totals),
            "estimated_cost_usd": round(total_cost, 2),
            "files_scanned": files_scanned,
            "malformed_lines_skipped": malformed,
        },
        "pricing_used": PRICE,
        "per_project": per_project,
        "open_threads": open_threads[:25],
        "repo_todos": repo_todos,
    }
    json.dump(digest, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
