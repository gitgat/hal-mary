# hal-mary implementation plan

Design of record: [2026-09-07-hal-mary-design.md](../specs/2026-09-07-hal-mary-design.md)

## Resequencing during execution (2026-09-07)

The plan below is the original task order. Execution changed it three times, for reasons worth
recording:

**Tasks 3, 4 and 5 ran in parallel, in separate git worktrees.** They share no source file, and the
draft deadline made serialising three independent modules a poor trade. Three implementers cannot
share one working tree, so each got its own worktree branched off the same commit.

**Task 6 was split.** `draft/board.py` (the pure snake-draft and name-matching functions) and
`events.py` depend on nothing else being built — no database, no network, no Claude — so they became
**Task 6a** and started immediately rather than waiting. **Task 6b** keeps the board-building job, the
advisor, and the draft loop, which do depend on 3, 4 and 5. This took the longest task off the
critical path by roughly its own duration.

**Task 11 was added and given priority.** Two implementers independently discovered that every
relative path in `[paths]`, plus `claude.scratch_dir` and `claude.system_prompt_file`, resolves
against the process working directory. Under the systemd unit that Task 10 installs, the memory
directory would not be found, `standing_memory()` would return an empty string, and every prompt would
silently go out without the standing context that tells Claude who Caroline is and what the league
rules are. No crash, no log line, just quietly worse advice.

That is the same class of defect Task 1 fixed for the config file and `.env`, where the fix did not
generalise. Task 11 anchors every configured path to the directory containing the resolved
`config.toml` and makes a missing directory loud rather than silent. It runs immediately after 3, 4
and 5 merge, and **before Task 10**, because Task 10 is what would ship the bug.

Task briefs for every task live in the execution workspace and carry the interfaces as actually built,
which supersede the sketches below wherever they differ.

## Implementation plan

Ordered by the draft deadline. Each task is executed subagent-driven with TDD: implementer subagent writes failing tests, then code; reviewer subagent checks against this plan. Task 0 and Task 1 come first, no exceptions.

### Task 0: Repo foundation (design, plan, CLAUDE.md, DECISIONS.md, first PR)
- Copy the Design section above into `docs/superpowers/specs/2026-09-07-hal-mary-design.md` and this plan into `docs/superpowers/plans/2026-09-07-hal-mary-plan.md`. Initial commit.

### Task 1: Scaffold
- `uv init`, `pyproject.toml` with deps and pytest config, `src/hal_mary/__init__.py`, `config.toml` with `[claude]`, `[jobs.*]` (all `model = "opus"`), `[draft]`, `[web]`; `.env.example`; `.gitignore` (`.env`, `*.db`, scratch). `config.py` with tests: loads toml, overlays env, validates required keys, model per job resolvable.
- `db.py` with migration runner over `migrations/*.sql` and tests for idempotent apply. Migration 001: `league_settings, teams, players, roster_slots, draft_picks, board, notes(+fts), advice, job_runs, sync_runs, claude_calls, chat_sessions, chat_messages`.

### Task 2: ESPN draft spike (riskiest assumption)
- `hal-mary draft-spike` CLI: with real cookies and a league ID, print `league.draft` length every 5s for 2 minutes. Bryan runs it against an ESPN mock draft. Records outcome in `docs/superpowers/specs/...-design.md` under a "Spike results" heading. Decides whether Fallback A (manual tap) is required in Task 6.

### Task 3: Claude runner
- `tests/fake_claude/claude` script + fixtures. Tests for argv per job config, stdin prompt, stream parsing, `--json-schema` result extraction, timeout kill, `claude_calls` row written, binary override. Then `claude_runner.py`.

### Task 4: Memory
- `memory.py`: `write_note`, `search_notes` (FTS5, filters, recency), `standing_memory()` reads `memory/*.md`, `build_context(...)` assembles the prompt preamble. Tests with in-memory SQLite.

### Task 5: ESPN client and sync
- Record fixtures (Bryan supplies cookies; record settings, teams, draft, free agents). `client.py` returning dicts, `sync.py` upserts, `hal-mary sync` CLI. `memory/league.md` generated from settings (scoring type, roster slots, team count, draft date, my pick slot). Tests against fixtures.

### Task 6: Draft board and loop
- `board.py` pure functions with tests (snake math, needs, scarcity, apply picks).
- `board_build` job: prompt `prompts/board_build.md` with league settings; tools on; `--json-schema` for tiers; writes `board` + `notes`. Tested with fake runner.
- `loop.py` + `advisor.py` with tests using fake ESPN + fake runner: new picks detected, advisor triggered at the right distance, structured advice persisted, fallback on parse failure.
- If spike failed: manual "taken" endpoint feeding the same loop.

### Task 7: Web app, draft-first
- `app.py` with password auth, SSE bus, `/draft` page (board by tier, my roster, advice card, manual taken control if needed), `/status`. Route tests. Mobile layout checked in a browser.
- `hal-mary serve` CLI runs web + scheduler in one process.
- **Checkpoint: draft-ready.** Run `board_build` for real, run the loop against a mock draft, use it on a phone.

### Task 8: Chat
- `/chat` page streaming over SSE, `chat_sessions` with `claude --resume`, tools on, context from `build_context` + team summary. "Remember this" writes a note. Tests with fake runner streaming fixture.

### Task 9: Scheduler and in-season jobs
- `registry.py`, `scheduler.py` phase detection and registration, `job_runs` persistence, manual `hal-mary job <name>`. Jobs `news_sweep`, `waiver_scan`, `lineup_check`, `weekly_recap` each with prompt file, schema, tests. `/advice` feed and `/team` pages.

### Task 10: Deployment
- `deploy/hal-mary.service`, `deploy/deploy.sh`, `README.md` runbook: cookies, first sync, `claude` login on the VM, enabling linger. Provision the Proxmox VM with Bryan.

## Verification

- `uv run pytest` green after every task.
- Task 2: spike output shows `len(league.draft)` increasing during a mock draft (or documents that it does not).
- Task 7 checkpoint: on a phone, `/draft` shows tiers, updates within 10s of a mock-draft pick, and advice appears before Caroline's pick with a recommendation in under 20s.
- Task 8: a chat question with web tools returns a current-events answer with a source URL.
- Task 10: `systemctl --user status hal-mary` active on the new VM; `/status` shows ESPN auth OK and claude OK.

## Working agreement (added 2026-09-07 after plan approval)

This repository is **entirely AI-authored**. Bryan owns judgment and outcomes; the agent owns
mechanism and is expected to operate the repo autonomously, as an owner would.

**Git workflow — every task:**
1. Branch from `main`: `task/NN-<slug>`.
2. TDD commits on the branch (failing test, then implementation).
3. `gh pr create` with a body stating what changed, how it was verified, and the plan task it closes.
4. Self-review with the `feature-dev:code-reviewer` subagent, address findings.
5. `gh pr merge --squash --delete-branch` once tests pass. No waiting on a human reviewer.
6. `main` stays green. Never push a red `main`.

**Durable notes live in the repo, not in chat.** Three files carry the memory:
- `CLAUDE.md` — how to work in this repo: conventions, commands, hard rules, gotchas. Updated in the
  same commit as any change that invalidates it (UPDATE-DOCS-AS-WE-GO, per the homelab process).
- `docs/superpowers/specs/2026-09-07-hal-mary-design.md` — the design of record. Amend it when a
  decision changes; record spike outcomes under "Spike results".
- `docs/DECISIONS.md` — dated one-paragraph entries for every non-obvious choice and every reversal,
  with the reasoning. This is what a future session reads to avoid re-litigating settled ground.

**Secrets never enter git.** `.env` is gitignored; `.env.example` carries the key names only. ESPN
cookies, the web password, and anything else credential-shaped stay on the box.

**Verification before claims.** No PR says "works" without the command output that proves it.
