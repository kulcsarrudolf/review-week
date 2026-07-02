#!/usr/bin/env python3
"""
extract.py - Parse Claude Code session transcripts into a compact JSON digest.

Reads JSONL transcripts under ~/.claude/projects/<slug>/*.jsonl (and
.../subagents/*.jsonl), filters to a time window, and emits one JSON object on
stdout with per-project metrics, token/cost estimates, PR outcomes, sampled
user prompts, rework signals, open-thread flags, and a week-over-week
comparison against the immediately preceding equal-length window.

Also computes git metrics (commits, lines added/removed) for the repos you
worked in, and an estimated active-hours figure from transcript timestamps.

Pure stdlib. No third-party dependencies.

Usage:
    extract.py [--since 7d] [--repos-todo]
    extract.py --since 14d
    extract.py --since 2026-06-25..2026-07-02
    extract.py --set-focus call-center-poc,igemag-ai   # persist focus tags
    extract.py --focus igemag-ai                        # focus for this run only
    extract.py --clear-focus                            # remove all focus tags
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

# --- Pricing (USD per 1M tokens), keyed by a substring of the model id, so cost
# works across model types automatically (Opus, Sonnet, Haiku, Fable, ...). Each
# entry is (input, output). Cache read is ~0.1x input, cache write ~1.25x input
# (5-minute cache), derived per model. Extend or adjust if pricing changes.
MODEL_PRICING = {
    "claude-fable-5":    (10.0, 50.0),
    "claude-mythos-5":   (10.0, 50.0),
    "claude-opus-4-1":   (15.0, 75.0),
    "claude-opus-4-0":   (15.0, 75.0),
    "claude-opus-4":     (5.0, 25.0),    # opus 4-5/4-6/4-7/4-8
    "claude-sonnet-4":   (3.0, 15.0),    # sonnet 4-x
    "claude-haiku-4":    (1.0, 5.0),     # haiku 4-5
    "claude-3-opus":     (15.0, 75.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-5-haiku":  (0.80, 4.0),
    "claude-3-haiku":    (0.25, 1.25),
}
DEFAULT_PRICING = (5.0, 25.0)   # unknown model: assume Opus-tier
CACHE_READ_MULT = 0.10
CACHE_WRITE_MULT = 1.25

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

# Persistent "focus" tags. Projects listed here get their tips, next-week
# ideas, and product ideas weighted higher. Survives across runs (including the
# scheduled weekly run, which starts cold).
FOCUS_CONFIG = os.path.expanduser("~/.claude/reviews/focus.json")


def _norm(s):
    """Normalize a name for forgiving matching (case/separator-insensitive)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load_focus():
    try:
        with open(FOCUS_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
        foc = data.get("focus", [])
        return [str(x) for x in foc] if isinstance(foc, list) else []
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def save_focus(names):
    os.makedirs(os.path.dirname(FOCUS_CONFIG), exist_ok=True)
    with open(FOCUS_CONFIG, "w", encoding="utf-8") as f:
        json.dump({"focus": names}, f, indent=2)
        f.write("\n")

# Gap (seconds) above which two consecutive messages in a session are treated as
# separate working stretches rather than continuous active time.
ACTIVE_GAP_SECONDS = 30 * 60

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
    """Return (start, end) in UTC from a --since argument."""
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


def price_for(model):
    """Input/output USD per 1M tokens for a model id (longest substring match)."""
    best, best_len = DEFAULT_PRICING, -1
    for key, rate in MODEL_PRICING.items():
        if key in (model or "") and len(key) > best_len:
            best, best_len = rate, len(key)
    return best


def cost_of_model(model, tok):
    """Cost for one model's token dict (input/output/cache_read/cache_write)."""
    inp, out = price_for(model)
    return (
        tok.get("input", 0) / 1e6 * inp
        + tok.get("output", 0) / 1e6 * out
        + tok.get("cache_read", 0) / 1e6 * inp * CACHE_READ_MULT
        + tok.get("cache_write", 0) / 1e6 * inp * CACHE_WRITE_MULT
    )


def cost_of(tokens_by_model):
    """Total cost for a {model: {input, output, cache_read, cache_write}} map."""
    return sum(cost_of_model(model, tok) for model, tok in tokens_by_model.items())


def add_usage(bucket, model, u):
    """Accumulate an assistant message's usage into a {model: tokendict} bucket."""
    t = bucket[model]
    t["input"] += u.get("input_tokens", 0) or 0
    t["output"] += u.get("output_tokens", 0) or 0
    t["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
    t["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0


def flat_tokens(tokens_by_model):
    """Collapse a {model: tokendict} map into one summed token dict."""
    flat = defaultdict(int)
    for tok in tokens_by_model.values():
        for k, v in tok.items():
            flat[k] += v
    return dict(flat)


def active_hours(sessions_ts):
    """Estimate active working hours: sum in-session gaps below ACTIVE_GAP_SECONDS."""
    total = 0.0
    for stamps in sessions_ts.values():
        stamps = sorted(stamps)
        for a, b in zip(stamps, stamps[1:]):
            gap = b - a
            if 0 < gap <= ACTIVE_GAP_SECONDS:
                total += gap
    return round(total / 3600.0, 1)


class WindowAgg:
    """Lightweight per-window metric accumulator used for comparison."""

    def __init__(self):
        self.sessions = set()
        self.active_days = set()
        self.tokens_by_model = defaultdict(lambda: defaultdict(int))
        self.tool_calls = 0
        self.rework = 0
        self.prs = set()
        self.sessions_ts = defaultdict(list)  # sessionId -> [epoch seconds]

    def summary(self, git):
        tok = flat_tokens(self.tokens_by_model)
        return {
            "sessions": len(self.sessions),
            "active_days": len(self.active_days),
            "active_hours_est": active_hours(self.sessions_ts),
            "prs_opened": len(self.prs),
            "commits": git["commits"],
            "loc_added": git["added"],
            "loc_removed": git["removed"],
            "tokens": tok,
            "total_tokens": sum(tok.values()),
            "output_tokens": tok.get("output", 0),
            "tool_calls": self.tool_calls,
            "rework_hits": self.rework,
            "estimated_cost_usd": round(cost_of(self.tokens_by_model), 2),
        }


def blank_project():
    return {
        "sessions": set(),
        "active_days": set(),
        "first": None,
        "last": None,
        "tools": defaultdict(int),
        "tokens_by_model": defaultdict(lambda: defaultdict(int)),
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


def git_window(cwd, start, end):
    """Return {commits, added, removed} for one repo over [start, end)."""
    result = {"commits": 0, "added": 0, "removed": 0}
    if not cwd or not os.path.isdir(os.path.join(cwd, ".git")):
        return result
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "log",
             "--since", start.isoformat(), "--until", end.isoformat(),
             "--no-merges", "--numstat", "--pretty=format:__COMMIT__"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return result
    # Split on newlines only: str.splitlines() would also break on control
    # separators, and a marker line must survive intact to be counted.
    for line in out.split("\n"):
        if line == "__COMMIT__":
            result["commits"] += 1
        elif "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 2:
                a, d = parts[0], parts[1]
                result["added"] += int(a) if a.isdigit() else 0
                result["removed"] += int(d) if d.isdigit() else 0
    return result


def git_metrics(cwds, start, end):
    """Aggregate git metrics across a set of repo working dirs."""
    total = {"commits": 0, "added": 0, "removed": 0}
    per_repo = []
    for cwd in sorted(c for c in cwds if c):
        g = git_window(cwd, start, end)
        if g["commits"] or g["added"] or g["removed"]:
            per_repo.append({"cwd": cwd, **g})
        for k in total:
            total[k] += g[k]
    return total, per_repo


def compare(cur, prev):
    """Build a metric-by-metric comparison block with deltas."""
    keys = [
        ("active_hours_est", "Active hours (est)"),
        ("sessions", "Sessions"),
        ("commits", "Commits"),
        ("loc_added", "Lines added"),
        ("loc_removed", "Lines removed"),
        ("prs_opened", "PRs opened"),
        ("total_tokens", "Total tokens"),
        ("output_tokens", "Output tokens"),
        ("tool_calls", "Tool calls"),
        ("rework_hits", "Rework signals"),
        ("estimated_cost_usd", "Estimated cost (USD)"),
    ]
    rows = []
    for key, label in keys:
        c = cur.get(key, 0)
        p = prev.get(key, 0)
        delta = round(c - p, 2)
        pct = round((delta / p) * 100, 1) if p else None
        rows.append({
            "metric": label, "key": key,
            "current": c, "previous": p, "delta": delta, "pct_change": pct,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="7d",
                    help="Window: 'Nd' (e.g. 7d) or 'YYYY-MM-DD..YYYY-MM-DD'.")
    ap.add_argument("--repos-todo", action="store_true",
                    help="Also scan active git repos for recent TODO/FIXME (heavier).")
    ap.add_argument("--focus", default=None,
                    help="Comma-separated project names to focus on for THIS run only.")
    ap.add_argument("--set-focus", default=None,
                    help="Comma-separated project names to save as the persistent focus set.")
    ap.add_argument("--clear-focus", action="store_true",
                    help="Clear the persistent focus set.")
    args = ap.parse_args()

    # Resolve focus: --set-focus/--clear-focus persist; --focus is a one-off
    # override; otherwise fall back to the saved config.
    if args.clear_focus:
        save_focus([])
    if args.set_focus is not None:
        save_focus([s.strip() for s in args.set_focus.split(",") if s.strip()])
    if args.focus is not None:
        focus_names = [s.strip() for s in args.focus.split(",") if s.strip()]
    else:
        focus_names = load_focus()
    focus_norms = {_norm(x) for x in focus_names}

    start, end = parse_window(args.since)
    length = end - start
    prev_end = start
    prev_start = start - length
    scan_from = prev_start  # broaden mtime pre-filter to cover the previous window

    projects = defaultdict(blank_project)   # detailed, current window only
    cur_agg = WindowAgg()
    prev_agg = WindowAgg()
    cwds = set()                            # repo dirs seen in either window
    session_last = {}                       # open-thread heuristic, current window
    malformed = 0
    files_scanned = 0

    pattern_main = os.path.join(PROJECTS_DIR, "*", "*.jsonl")
    pattern_sub = os.path.join(PROJECTS_DIR, "*", "*", "subagents", "*.jsonl")
    files = glob.glob(pattern_main) + glob.glob(pattern_sub)

    for path in files:
        try:
            if datetime.fromtimestamp(os.path.getmtime(path), timezone.utc) < scan_from:
                continue
        except OSError:
            continue
        files_scanned += 1

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

                # Session-level title records (no timestamp filtering).
                if rtype == "ai-title" and sid:
                    projects[pname]["titles"].setdefault(sid, d.get("aiTitle"))
                    continue
                if rtype == "custom-title" and sid:
                    if d.get("customTitle"):
                        projects[pname]["titles"][sid] = d.get("customTitle")
                    continue
                if rtype == "pr-link":
                    pts = parse_ts(d.get("timestamp"))
                    url = d.get("prUrl")
                    if url and pts is not None:
                        if start <= pts < end:
                            cur_agg.prs.add(url)
                            projects[pname]["prs"][url] = d.get("prRepository")
                        elif prev_start <= pts < prev_end:
                            prev_agg.prs.add(url)
                    elif url and pts is None:
                        # No timestamp: attribute to current window.
                        cur_agg.prs.add(url)
                        projects[pname]["prs"][url] = d.get("prRepository")
                    continue

                if ts is None:
                    continue
                if start <= ts < end:
                    window = "cur"
                    agg = cur_agg
                elif prev_start <= ts < prev_end:
                    window = "prev"
                    agg = prev_agg
                else:
                    continue

                m = d.get("message")
                if not isinstance(m, dict):
                    continue

                if d.get("cwd"):
                    cwds.add(d["cwd"])

                # Common per-window aggregation.
                if sid:
                    agg.sessions.add(sid)
                    agg.sessions_ts[sid].append(ts.timestamp())
                agg.active_days.add(ts.date().isoformat())

                role = m.get("role")
                content = m.get("content")

                if role == "assistant":
                    add_usage(agg.tokens_by_model, m.get("model") or "unknown",
                              m.get("usage") or {})
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                agg.tool_calls += 1
                elif role == "user":
                    if not is_tool_result_only(content):
                        txt = text_of(content).strip()
                        if txt and not is_noise(txt) and REWORK_RE.search(txt):
                            agg.rework += 1

                # Detailed current-window aggregation (drives the narrative).
                if window != "cur":
                    continue

                p = projects[pname]
                if d.get("cwd") and not p["cwd"]:
                    p["cwd"] = d["cwd"]
                if sid:
                    p["sessions"].add(sid)
                p["active_days"].add(ts.date().isoformat())
                if p["first"] is None or ts < p["first"]:
                    p["first"] = ts
                if p["last"] is None or ts > p["last"]:
                    p["last"] = ts
                if d.get("gitBranch"):
                    p["branches"].add(d["gitBranch"])
                if d.get("permissionMode") == "plan":
                    p["plan_mode_msgs"] += 1

                if role == "assistant":
                    p["total_asst_msgs"] += 1
                    if m.get("model"):
                        p["models"][m["model"]] += 1
                    add_usage(p["tokens_by_model"], m.get("model") or "unknown",
                              m.get("usage") or {})
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                name = b.get("name", "?")
                                p["tools"][name] += 1
                                if name == "Agent":
                                    p["agent_calls"] += 1
                    txt = text_of(content).strip()
                    is_q = txt.endswith("?")
                    if sid:
                        session_last[sid] = (ts, "assistant", is_q, pname,
                                             p["titles"].get(sid), p["cwd"])
                elif role == "user":
                    if is_tool_result_only(content):
                        if sid:
                            session_last[sid] = (ts, "tool_result", False, pname,
                                                 p["titles"].get(sid), p["cwd"])
                        continue
                    txt = text_of(content).strip()
                    if txt and not is_noise(txt):
                        p["prompts"].append((ts.isoformat(), txt))
                        if REWORK_RE.search(txt):
                            p["rework_hits"] += 1
                    if sid:
                        session_last[sid] = (ts, "user", False, pname,
                                             p["titles"].get(sid), p["cwd"])

    # --- Git metrics for both windows over the repos worked in ---
    cur_git, cur_git_repos = git_metrics(cwds, start, end)
    prev_git, _ = git_metrics(cwds, prev_start, prev_end)

    cur_summary = cur_agg.summary(cur_git)
    prev_summary = prev_agg.summary(prev_git)

    # --- Open threads (current window) ---
    open_threads = []
    for sid, (ts, role, is_q, pname, title, cwd) in session_last.items():
        reason = None
        if role == "user":
            reason = "ended on a user prompt with no assistant completion"
        elif role == "assistant" and is_q:
            reason = "ended on an assistant question"
        if reason:
            open_threads.append({
                "project": pname, "session": sid, "title": title,
                "last_activity": ts.isoformat() if ts else None, "reason": reason,
            })
    open_threads.sort(key=lambda x: x["last_activity"] or "", reverse=True)

    # --- Per-project detail (current window) ---
    totals = defaultdict(int)
    total_cost = 0.0
    per_project = []
    for pname, p in projects.items():
        if not p["sessions"] and not p["prompts"]:
            continue
        tok = flat_tokens(p["tokens_by_model"])
        cost = cost_of(p["tokens_by_model"])
        total_cost += cost
        for k, v in tok.items():
            totals[k] += v

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

        is_focus = bool(focus_norms) and (
            _norm(pname) in focus_norms
            or (p["cwd"] and _norm(os.path.basename(p["cwd"])) in focus_norms)
        )
        per_project.append({
            "project": pname,
            "focus": is_focus,
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

    # Focus projects first, then by session count.
    per_project.sort(key=lambda x: (not x["focus"], -x["sessions"]))

    # --- Optional recent TODO/FIXME scan ---
    repo_todos = []
    if args.repos_todo:
        since_git = start.strftime("%Y-%m-%d")
        for cwd in sorted(c for c in cwds if c and os.path.isdir(c)):
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
                    repo_todos.append({"project": project_name(os.path.basename(cwd)),
                                       "todos": hits[:15]})
            except (subprocess.SubprocessError, OSError):
                continue

    digest = {
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "since_arg": args.since},
        "previous_window": {"start": prev_start.isoformat(),
                            "end": prev_end.isoformat()},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "projects": len(per_project),
            "sessions": cur_summary["sessions"],
            "active_hours_est": cur_summary["active_hours_est"],
            "commits": cur_git["commits"],
            "loc_added": cur_git["added"],
            "loc_removed": cur_git["removed"],
            "prs_opened": cur_summary["prs_opened"],
            "tokens": dict(totals),
            "estimated_cost_usd": round(total_cost, 2),
            "files_scanned": files_scanned,
            "malformed_lines_skipped": malformed,
        },
        "comparison": {
            "current": cur_summary,
            "previous": prev_summary,
            "deltas": compare(cur_summary, prev_summary),
        },
        "git_by_repo": cur_git_repos,
        "focus_projects": focus_names,
        "focus_matched": [pp["project"] for pp in per_project if pp["focus"]],
        "pricing_used": {
            "model_pricing_input_output": {k: list(v) for k, v in MODEL_PRICING.items()},
            "default": list(DEFAULT_PRICING),
            "cache_read_mult": CACHE_READ_MULT,
            "cache_write_mult": CACHE_WRITE_MULT,
            "note": "cost is per-model from each message's model id; unknown models use default",
        },
        "per_project": per_project,
        "open_threads": open_threads[:25],
        "repo_todos": repo_todos,
    }
    json.dump(digest, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
