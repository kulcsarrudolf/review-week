# claude-review-week

A [Claude Code](https://claude.com/claude-code) skill that reviews how you used Claude Code over the last 7 days and turns it into an actionable weekly report.

It reads your local session transcripts (all projects), extracts real usage signals, and produces:

1. A week-over-week comparison: active hours, sessions, commits, lines added/removed, PRs, tokens, tool calls, rework signals, and cost, each with the delta vs the previous equal-length window
2. A short summary of the projects you worked on
3. At least 5 improvement tips, each tied to a concrete signal in your data
4. What you did well and should keep doing
5. Up to 10 tactical ideas for the next 7 days
6. Product and business ideas: features or products worth building, mined from the week's work (what it is, who it is for, the problem, and a business angle)
7. Ideas to improve the skill itself
8. A footer showing the measured token/cost of generating the report itself

You can tag one or more projects as your **focus** so tips and ideas weight toward them (see [Focus projects](#focus-projects) below).

Every tip is grounded in your actual data (rework patterns, token/cost, plan-mode usage, PR follow-through), not generic advice.

## How it works

Claude Code stores each session as a JSONL transcript under `~/.claude/projects/<project>/`.
This skill ships a small, dependency-free Python script (`scripts/extract.py`) that parses those transcripts into a compact JSON digest: per-project tool counts, token usage with a cost estimate, PR links, git branches, plan-mode usage, sampled prompts, rework signals, and open threads.
It also computes a week-over-week comparison by aggregating both the current window and the immediately preceding equal-length window in the same run (no dependency on old reports), plus git metrics (commits, lines added/removed) for the repos you worked in and an estimated active-hours figure from transcript timestamps.
The skill then has Claude synthesize the report from that digest and write it to `~/.claude/reviews/YYYY-MM-DD.md`.
A second helper (`scripts/run_cost.py`) reads the live session transcript afterward and reports the measured tokens and cost of generating the report itself, which the skill appends as a footer.

Nothing leaves your machine. The extractor is pure standard-library Python 3 and makes no network calls.

## Prerequisites

- **Claude Code**, used at least a little, so there are session transcripts under `~/.claude/projects/` to analyze. With no history there is nothing to report on.
- **Python 3.8+** available as `python3`. The scripts are pure standard library, so there is nothing to `pip install`.
- **git** on your `PATH` (optional): used for the commit and lines added/removed metrics. Without it those numbers show zero; everything else still works.
- macOS or Linux shell. The optional `review-open` helper uses the macOS `open` command; on Linux swap it for `xdg-open`.

Everything runs locally. No API key is required to run the skill (Claude Code itself already handles auth).

## Install

Clone into your Claude Code skills directory:

```sh
git clone https://github.com/kulcsarrudolf/claude-review-week.git ~/.claude/skills/claude-review-week
```

That is all. Claude Code discovers skills in `~/.claude/skills/` automatically.

## Usage

In Claude Code, run:

```
/claude-review-week
```

Optional time window (defaults to the last 7 days):

```
/claude-review-week 14d
/claude-review-week 2026-06-25..2026-07-02
```

You can also run the extractor directly to inspect the raw digest:

```sh
python3 ~/.claude/skills/claude-review-week/scripts/extract.py --since 7d | python3 -m json.tool
```

### Extractor options

| Flag | Description |
|---|---|
| `--since 7d` | Rolling window of N days (default `7d`). |
| `--since 2026-06-25..2026-07-02` | Explicit date range (inclusive). |
| `--repos-todo` | Also scan active git repos for recently added `TODO`/`FIXME` to seed next-week ideas. Slower; off by default. |
| `--set-focus a,b` | Persist a "focus" set (see below). Survives future runs. |
| `--focus a,b` | Focus for this run only, without persisting. |
| `--clear-focus` | Remove all focus tags. |

## Focus projects

Tag one or more projects as your focus and the report weights its tips, next-week ideas, and product ideas toward them (they also sort first and get a `(focus)` tag).

In Claude Code, just say it:

```
/claude-review-week focus call-center-poc, igemag-ai
/claude-review-week clear focus
```

Or set it directly:

```sh
python3 ~/.claude/skills/claude-review-week/scripts/extract.py --set-focus "call-center-poc,igemag-ai"
```

The focus set persists in `~/.claude/reviews/focus.json`, so the scheduled weekly run picks it up automatically.
Matching is forgiving about case and separators (`Call Center POC` matches `call-center-poc`).

## Output

- Full report written to `~/.claude/reviews/YYYY-MM-DD.md`.
- A condensed summary printed inline in the chat.

Keeping dated reports lets you compare week over week.
Each run also prints the report path and an `open` command so you can jump straight to it.

### Optional: an `open-latest` helper

Add this to your shell config (`~/.zshrc` or `~/.bashrc`) to open the most recent report with one command:

```sh
review-open() {
  local dir="$HOME/.claude/reviews" file
  if [ -n "$1" ]; then file="$dir/$1.md"; else file=$(ls -t "$dir"/*.md 2>/dev/null | head -1); fi
  [ -f "$file" ] && open "$file" || echo "No report found in $dir" >&2
}
```

Then run `review-open` (latest) or `review-open 2026-07-02` (a specific date).

## Configuring cost estimates

Token cost is a rough estimate. It is computed **per model**: each assistant message records the model it used, so a session that mixes Opus, Sonnet, and Haiku is priced correctly without any configuration.

The pricing table lives at the top of `scripts/extract.py`, keyed by a substring of the model id (input, output USD per 1M tokens):

```python
MODEL_PRICING = {
    "claude-fable-5":  (10.0, 50.0),
    "claude-opus-4":   (5.0, 25.0),    # opus 4-5/4-6/4-7/4-8
    "claude-sonnet-4": (3.0, 15.0),    # sonnet 4-x
    "claude-haiku-4":  (1.0, 5.0),     # haiku 4-5
    ...
}
DEFAULT_PRICING = (5.0, 25.0)   # unknown model: assume Opus-tier
CACHE_READ_MULT = 0.10          # cache read  ~0.1x input
CACHE_WRITE_MULT = 1.25         # cache write ~1.25x input (5-minute cache)
```

Matching is longest-substring, so `claude-opus-4-8` and dated ids like `claude-haiku-4-5-20251001` resolve automatically, and an unknown model falls back to `DEFAULT_PRICING`. Cache read/write rates are derived per model from its input rate.

**Self-updating:** if a run encounters a model that has no price entry, the skill flags it (the extractor reports it in `unknown_models`), looks up the correct rate, and asks your permission before adding a row to `MODEL_PRICING`. You never edit the table by hand unless you want to. Decline and the model is priced at the Opus-tier default, noted in the report.

## Development

The skill is:

- `SKILL.md` orchestrates the workflow and defines the report structure.
- `scripts/extract.py` does the parsing, aggregation, week-over-week comparison, and per-model cost.
- `scripts/run_cost.py` measures the token/cost of generating the report itself.

Since the repo lives at `~/.claude/skills/claude-review-week`, edits take effect immediately: just run `/claude-review-week` again.

Ideas for contributions:

- Week-over-week deltas by reading the previous report in `~/.claude/reviews/`.
- Real PR merge status via `gh` (not just PRs opened).
- Per-day activity breakdown and a busiest-day callout.
- Cache-vs-non-cache cost split.

Issues and pull requests welcome.

## License

MIT. See [LICENSE](LICENSE).
