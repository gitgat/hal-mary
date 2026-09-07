# CLAUDE.md — how to work in this repo

`hal-mary` is a Claude-powered fantasy football advisor. It watches Caroline's ESPN league,
researches the live internet through the local `claude` binary, and tells her — in plain English,
assuming zero football knowledge — who to draft, start, and claim. **She makes every click in ESPN
herself. This application never writes to ESPN.**

Read [`docs/superpowers/specs/2026-09-07-hal-mary-design.md`](docs/superpowers/specs/2026-09-07-hal-mary-design.md)
for the design of record and [`docs/DECISIONS.md`](docs/DECISIONS.md) for why things are the way they
are. Do not re-litigate a decision recorded there without reading the entry first.

## This repo is entirely AI-authored

Bryan owns judgment and outcomes. The agent owns mechanism and operates the repo autonomously.
There is no human reviewer standing between a branch and `main`.

That autonomy is paid for by discipline. The rules below are not suggestions.

## Hard rules

1. **TDD, always.** Write the failing test first, watch it fail, then implement. A commit that adds
   behavior without a test that would have caught its absence does not get merged.
2. **Never claim something works without the command output that proves it.** No "should work", no
   "tests pass" written from memory. Run it, paste it.
3. **`main` stays green.** Every PR runs the full suite before merge.
4. **Secrets never enter git.** `.env` is gitignored. `.env.example` carries key names and nothing
   else. ESPN cookies, the web password, and the database live on the box, not in the repo.
5. **No hardcoded model names, timeouts, cadences, or budgets.** They belong in `config.toml`, keyed
   per job. Default model is `opus`. Code reads them through `hal_mary.config`.
6. **Never trust training knowledge for football facts.** The season is live and the model's cutoff
   is not. Anything about a current player, injury, or matchup comes from a Claude call with web
   tools enabled, and gets stored with its source URL.
7. **Update the docs the change invalidates, in the same commit.** A stale `CLAUDE.md` is worse than
   none because it is believed.

## Git workflow

```bash
git switch -c task/NN-short-slug        # branch per plan task
# ... TDD commits ...
uv run pytest                            # must be green
gh pr create --fill                      # body: what changed, how verified, task closed
# self-review with the feature-dev:code-reviewer subagent, address findings
gh pr merge --squash --delete-branch
```

Commit messages: imperative subject, body explaining *why*. Every commit made by an agent carries
the `Co-Authored-By` and `Claude-Session` trailers.

## Commands

```bash
uv sync                        # install deps
uv run pytest                  # full test suite
uv run pytest tests/unit -x    # fast loop while developing
uv run hal-mary serve          # web app + scheduler (dev)
uv run hal-mary sync           # pull league state from ESPN
uv run hal-mary job <name>     # run one job on demand
uv run hal-mary draft-spike    # probe whether ESPN exposes live draft picks
```

## Architecture in one paragraph

FastAPI serves a phone-friendly web app (Jinja + HTMX + Server-Sent Events) backed by SQLite.
APScheduler runs research jobs inside the same process. `hal_mary.claude_runner` is the **only**
module that spawns the `claude` binary; everything else calls through it. Football reasoning lives in
Markdown prompt files under `prompts/`, not in Python — Python does bookkeeping (which players are
gone, which roster slots are open, how many picks until her turn). Memory is SQLite tables plus an
FTS5-indexed `notes` table that every job writes to and every prompt retrieves from.

## The timing constraint that shapes everything

A `claude -p` call with web search takes 30 to 120 seconds. The ESPN draft pick clock is 60 to 90
seconds. **Therefore research happens before the draft and on-the-clock advice runs with web tools
off, against a board that is already built.** Any change that puts a web-enabled Claude call on the
pick-clock path is wrong.

## Gotchas

- **The ESPN API is unofficial.** Cookies (`ESPN_S2`, `SWID`) expire. The status page checks auth
  hourly; when it breaks in-season, that is the first thing to look at.
- **Whether `league.draft` updates *during* a live draft is unverified.** See the spike results in
  the design doc before relying on it.
- **Database on local disk, never on NFS.** In this homelab `/var/data` is a TrueNAS NFS export
  mounted on every node, and SQLite on NFS corrupts. The production VM keeps `hal.db` on its own
  disk.
- **Tests must never spawn the real `claude` binary** except the one live integration test, which
  skips when the binary or its auth is missing. Unit tests point `config.claude.binary` at
  `tests/fake_claude/claude`.
