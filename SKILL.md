---
name: review-week
description: Review my Claude Code usage over the last 7 days and produce a weekly report with improvement tips, wins to keep, a project summary, next-week ideas, and skill-improvement ideas. Use when the user wants a weekly AI-usage review or mentions "review-week".
---

# Weekly AI Usage Review

Produce an honest, data-grounded review of how the user worked with Claude Code
over a time window (default: the last 7 days).

## Steps

1. **Run the extractor.** From this skill's directory:
   ```
   python3 scripts/extract.py [--since <window>] [--repos-todo]
   ```
   - Pass through any window the user gave: `--since 14d`, or a range like
     `--since 2026-06-25..2026-07-02`. Default is `7d` (no arg needed).
   - Add `--repos-todo` when the user wants next-week ideas seeded from recent
     TODO/FIXME added in their repos. It is slower; skip it otherwise.
   - The script prints one JSON digest to stdout. Capture it.

2. **Read the digest.** Reason from `per_project` (tools, tokens, cost, PRs,
   branches, plan-mode use, rework hits, sampled prompts, titles),
   `open_threads`, and `repo_todos`. The sampled prompts show how the user
   actually steers Claude; use them for qualitative tips.

3. **Write the report** with the six sections below, then **write it to a file**
   at `~/.claude/reviews/YYYY-MM-DD.md` (today's date; create the dir if needed)
   and print a condensed version inline in chat.

4. **Point the user to the file.** After the inline summary, print the full
   report path and a ready-to-run open command on its own line, for example:
   ```
   Full report: ~/.claude/reviews/2026-07-02.md
   Open it:     open ~/.claude/reviews/2026-07-02.md   (or: review-open)
   ```

## Report structure (all six sections, in order)

1. **Week at a glance** - window dates, projects touched, session count, total
   tokens, and estimated cost. State plainly that cost is an estimate.
2. **Projects worked on** - one short paragraph per active project: what it was
   moving toward (use titles and sampled prompts), notable PRs/branches, and
   rough effort (sessions, tokens). Keep it tight.
3. **Tips to improve (at least 5)** - each tip MUST cite a concrete signal from
   the data, for example: high `rework_hits` on a project (rework/redo prompts),
   large token/cost with no merged PR, low `plan_mode_msgs` on complex work,
   very long single sessions, heavy Bash use where a tool would be cleaner, or
   vague opening prompts in `sampled_prompts`. No generic advice that could
   apply to anyone.
4. **Doing well, keep it up** - grounded in the same data: good plan-mode use,
   clean PR follow-through, tight scoping, effective subagent/Agent use, etc.
5. **Ideas for the next 7 days (up to 10)** - draw from `open_threads`,
   unmerged PRs/branches, project momentum, `repo_todos`, and your own synthesis
   of what each project needs next. Label each idea with its source project.
6. **Ideas to improve this skill** - your suggestions for evolving
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
