---
name: review-week
description: Review my Claude Code usage over the last 7 days and produce a weekly report with improvement tips, wins to keep, a project summary, next-week ideas, product/business ideas to build based on my work, and skill-improvement ideas. Use when the user wants a weekly AI-usage review or mentions "review-week".
---

# Weekly AI Usage Review

Produce an honest, data-grounded review of how the user worked with Claude Code
over a time window (default: the last 7 days).

## Steps

1. **Handle focus intent first (if any).** The user can tag "focus" projects
   that get weighted higher in the report. If their message asks to set, change,
   or clear focus, act on it before generating:
   - "focus on X" / "set focus to X, Y" / `/review-week focus X,Y` ->
     `python3 scripts/extract.py --set-focus "X,Y"` (persists across runs).
   - "clear focus" / "remove focus" -> `python3 scripts/extract.py --clear-focus`.
   - "just this week focus on X" (temporary) -> add `--focus "X"` to the run in
     step 2 instead of persisting.
   Otherwise, do nothing here; the saved focus set is used automatically.

2. **Run the extractor.** From this skill's directory:
   ```
   python3 scripts/extract.py [--since <window>] [--focus <names>] [--repos-todo]
   ```
   - Pass through any window the user gave: `--since 14d`, or a range like
     `--since 2026-06-25..2026-07-02`. Default is `7d` (no arg needed).
   - Add `--repos-todo` when the user wants next-week ideas seeded from recent
     TODO/FIXME added in their repos. It is slower; skip it otherwise.
   - The script prints one JSON digest to stdout. Capture it.

3. **Read the digest.** Reason from `per_project` (tools, tokens, cost, PRs,
   branches, plan-mode use, rework hits, sampled prompts, titles, and `focus`),
   `open_threads`, `repo_todos`, and `focus_matched`. The sampled prompts show
   how the user actually steers Claude; use them for qualitative tips.
   **Focus weighting:** when `focus_matched` is non-empty, give those projects
   clear priority in the Tips, Next-7-days, and Product/business sections: lead
   with them, and aim at least half of each list at focus projects (while still
   briefly covering the rest). Mark focus projects in the report (e.g. a
   "(focus)" tag). When no focus is set, weight purely by activity as before.

4. **Write the report** with the eight sections below, then **write it to a file**
   at `~/.claude/reviews/YYYY-MM-DD.md` (today's date; create the dir if needed)
   and print a condensed version inline in chat.

5. **Measure and append the generation cost.** After the report file exists, run:
   ```
   python3 scripts/run_cost.py
   ```
   It reads the live session transcript and reports the measured tokens and cost
   of the turns that produced this report (from the extractor call to now). Then
   append a short footer to the report file and echo it inline, for example:
   ```
   ## Report generation cost

   Generating this report used ~3.5M tokens (mostly cache reads) and cost about
   $0.32, measured from this session. This excludes the final footer step, so it
   is a close lower bound. Cheaper on a fresh session than deep in a long one.
   ```
   Use the real numbers from `run_cost.py` (`total_tokens`, `estimated_cost_usd`).
   State plainly it is a measured lower bound, not an estimate of the whole turn.

6. **Point the user to the file.** After the inline summary, print the full
   report path and a ready-to-run open command on its own line, for example:
   ```
   Full report: ~/.claude/reviews/2026-07-02.md
   Open it:     open ~/.claude/reviews/2026-07-02.md   (or: review-open)
   ```

## Report structure (all eight sections, in order)

1. **Week at a glance** - window dates, projects touched, session count, total
   tokens, and estimated cost. State plainly that cost is an estimate. If
   `focus_matched` is non-empty, name the focus project(s) here so it is clear
   the report is weighted toward them.
2. **Week over week** - render `comparison.deltas` as a markdown table with
   columns: Metric, This period, Previous, Change. Include the sign and, when
   `pct_change` is present, the percentage. Metrics cover active hours (est),
   sessions, commits, lines added/removed, PRs opened, tokens, tool calls,
   rework signals, and estimated cost. Note that "previous" is the immediately
   preceding window of equal length (`previous_window`), computed in the same
   run - not a parse of an old report. Add one or two sentences calling out the
   most meaningful movements (e.g. cost up while output tokens flat means more
   cache reads; rework down is an improvement). Active hours and commits/LOC are
   estimates: hours from transcript gaps, git stats from the repos you worked in.
3. **Projects worked on** - one short paragraph per active project: what it was
   moving toward (use titles and sampled prompts), notable PRs/branches, and
   rough effort (sessions, tokens). Keep it tight.
4. **Tips to improve (at least 5)** - each tip MUST cite a concrete signal from
   the data, for example: high `rework_hits` on a project (rework/redo prompts),
   large token/cost with no merged PR, low `plan_mode_msgs` on complex work,
   very long single sessions, heavy Bash use where a tool would be cleaner, or
   vague opening prompts in `sampled_prompts`. No generic advice that could
   apply to anyone.
5. **Doing well, keep it up** - grounded in the same data: good plan-mode use,
   clean PR follow-through, tight scoping, effective subagent/Agent use, etc.
6. **Ideas for the next 7 days (up to 10)** - tactical execution: draw from
   `open_threads`, unmerged PRs/branches, project momentum, and `repo_todos`.
   Label each idea with its source project. Keep this list about finishing and
   advancing existing work.
7. **Product and business ideas** - this is the section to emphasize. Mine the
   week's work for opportunities to build features or products, grounded in what
   the user actually did and the domains/tech they touched (from titles, sampled
   prompts, and project focus). Cover two kinds:
   - **Feature/product extensions** of an existing project (e.g. turn a spike
     into a sellable capability, productize a repeated internal workflow).
   - **Standalone product or tool ideas** the week's experience uniquely
     positions the user to build (a pain they hit repeatedly, a reusable piece
     they rebuilt across projects, a niche they now understand).
   For each idea give 2-4 lines: **what it is**, **who it is for**, **the problem
   it solves**, and a **business angle** (why it could have value, a rough
   monetization or go-to-market path, or the smallest validating first step).
   Be concrete and tied to the user's real work, not generic startup ideas.
   Prioritize: lead with the 1-2 ideas with the strongest signal behind them.
8. **Ideas to improve this skill** - your suggestions for evolving
   `/review-week` (new metrics, better heuristics, comparisons, etc.).

## Style

- English.
- Never use em dashes or en dashes. Use periods, commas, colons, parentheses,
  or plain hyphens for ranges.
- In markdown, start each prose sentence on its own line.
- Be direct and specific. Every claim should trace back to a number or a quoted
  prompt from the digest. Avoid filler and flattery.

## Notes

- The extractor is pure stdlib Python 3, reads `~/.claude/projects/**`, and
  needs no network. If it reports `malformed_lines_skipped`, that is normal for
  partially-written transcripts; only flag it if the count is large.
- Pricing constants live at the top of `scripts/extract.py`. Update them there
  if Opus pricing changes.
