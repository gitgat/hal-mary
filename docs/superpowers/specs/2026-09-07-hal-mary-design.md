# hal-mary: Claude-powered fantasy football advisor

## Context

Caroline got the last spot in Sam's ESPN fantasy football league. Neither she nor Bryan follows football. The draft is within 48 hours. We are building a resident application that watches the league on ESPN, researches the live internet through Claude, and advises Caroline in plain English: who to draft on the clock, who to start each week, who to claim off waivers. Caroline makes every click in ESPN herself. The bot never acts on her account.

Decisions made in brainstorming (2026-09-07):

| Decision | Choice |
|---|---|
| Draft role | Advisor. Caroline clicks in ESPN. Bot never writes to ESPN. |
| Interface | Local web app, phone-friendly, used side by side with ESPN's draft room. Chat box talks to Claude directly. |
| ESPN access | `espn_s2` + `SWID` cookies, unofficial JSON API via the `espn-api` Python library (0.46.0). Read-only. |
| Stack | Python 3.12, `uv`, FastAPI, Jinja + HTMX + SSE, SQLite, APScheduler, pytest. |
| Claude | Shell out to the local `claude` binary with `-p`, subscription auth. No API key. |
| Models | All model choices live in `config.toml`. Default `opus` for every job. Never hardcode. |
| Live data | Claude's built-in WebSearch/WebFetch, enabled per job. Never rely on training knowledge for current events. |
| Memory | SQLite tables + FTS5 full-text notes. Embeddings deferred. |
| Deployment | Dev here on `dev-scratch` via `uv run`, stopped when not actively developing. Production on a new Proxmox VM (provisioned with `../swarm-config/provision-dev.sh` + `uv`), systemd user service, data on VM-local disk. Never on NFS. |
| Approach | Prepared board + fast advisor. Research runs on a schedule with web tools. On-the-clock advice uses local context only, web tools off, so it answers in seconds. |
| Process | TDD (superpowers:test-driven-development) and subagent-driven development (superpowers:subagent-driven-development) for every task. |

Key constraint that shapes the design: a `claude -p` call with web search takes 30 to 120 seconds; the ESPN pick clock is 60 to 90 seconds. Research must happen before the draft, not during it.

## Design

### Repository layout

```
hal-mary/
  pyproject.toml            # uv project, deps: fastapi, uvicorn, jinja2, espn_api, apscheduler, pydantic, tomli/tomllib
  config.toml               # models per job, cadences, tool allowlists, poll intervals
  .env.example              # ESPN_S2, SWID, LEAGUE_ID, TEAM_ID, SEASON, WEB_PASSWORD, DB_PATH
  prompts/                  # Markdown prompt files, one per job
    system.md               # standing persona + "explain like we know nothing about football"
    board_build.md
    draft_advice.md
    chat.md
    waiver_scan.md
    news_sweep.md
    lineup_check.md
    weekly_recap.md
  memory/                   # standing facts always included (committed, human-editable)
    caroline.md             # preferences, risk tolerance, "explain terms"
    league.md               # filled in from ESPN settings after first sync
  src/hal_mary/
    config.py               # load config.toml + .env into a Settings model
    db.py                   # sqlite connection, migrations (plain SQL files in src/hal_mary/migrations/)
    claude_runner.py        # the only module that spawns `claude`
    memory.py               # notes write/search (FTS5), standing memory files
    espn/
      client.py             # thin wrapper over espn_api.football.League, returns plain dicts
      sync.py               # persist league settings, teams, rosters, draft picks, free agents
    draft/
      board.py              # pure functions: apply picks, positional needs, scarcity, next-pick distance
      loop.py               # poll ESPN, detect new picks, trigger advisor when on/near the clock
      advisor.py            # build prompt from board + memory, call claude (tools off, json-schema), persist advice
    jobs/
      registry.py           # job name -> callable, cadence from config
      scheduler.py          # APScheduler wiring, phase-aware (pre_draft / draft_live / in_season)
      board_build.py        # deep research job -> tiers + notes
      news_sweep.py
      waiver_scan.py
      lineup_check.py
      weekly_recap.py
    web/
      app.py                # FastAPI app factory, auth middleware, SSE bus
      routes/{draft,team,advice,chat,status}.py
      templates/*.html
      static/
    cli.py                  # `hal-mary serve|sync|job <name>|draft-spike`
  tests/
    fixtures/espn/*.json    # recorded ESPN responses
    fixtures/claude/*.jsonl # recorded stream-json
    fake_claude/claude      # fake binary put on PATH in tests
    unit/ ... integration/ ...
  deploy/
    hal-mary.service        # systemd user unit
    deploy.sh               # git pull, uv sync, migrate, restart
  docs/superpowers/specs/2026-09-07-hal-mary-design.md   # this design, committed as first task
  docs/superpowers/plans/2026-09-07-hal-mary-plan.md     # this plan, committed alongside
```

### Claude runner (`claude_runner.py`)

One function `run(job: JobSpec, prompt: str, *, session_id=None, resume=None, stream=False) -> ClaudeResult`.

Builds the subprocess argv from config:

```
claude -p --output-format stream-json --include-partial-messages
       --model <config.jobs[job].model>
       --system-prompt-file prompts/system.md   (or --system-prompt <text>)
       --tools <"" | WebSearch WebFetch>         from config.jobs[job].tools
       --allowedTools WebSearch WebFetch         when tools enabled
       --permission-mode dontAsk
       --json-schema <schema>                    when job.schema is set
       --no-session-persistence                  for jobs; omitted for chat
       --resume <id>                             for chat continuation
       --max-budget-usd <config.jobs[job].max_budget_usd>  optional
```

- Prompt goes over stdin. Working directory is a scratch dir, not the repo, so Claude can't wander into project files.
- Timeout per job from config. On timeout: kill, record failure.
- Parses stream-json events; yields text deltas when `stream=True` (chat SSE), else collects final `result` and structured output.
- Every call writes a row to `claude_calls` (job, model, argv, prompt_hash, duration_ms, cost_usd if reported, exit_code, output_path).
- The binary path is `config.claude.binary` (default `claude`), so tests point it at `tests/fake_claude/`.

### Memory (`memory.py`)

Tables: `notes(id, created_at, source_job, topic, player_name, team_abbr, text, source_url, expires_at)` with an FTS5 virtual table over `text`, `player_name`, `topic`. As built: `search_notes(conn, query=None, *, players=None, topics=None, limit=20, max_age_days=None, include_expired=False)` returns ranked notes, `write_note(conn, note)` / `write_notes(conn, notes)` from jobs and chat, and `prune_notes(conn, older_than_days)` drops stale ones. Standing memory: `standing_memory(settings)` reads `memory/*.md` fresh at prompt-build time, never cached. `build_context(conn, settings, ...)` assembles the block: standing memory + caller-supplied live sections + retrieved notes. Every prompt = system.md + that block + job-specific task.

### ESPN (`espn/`)

`client.py` wraps `espn_api.football.League(league_id, year, espn_s2, swid)` and returns plain dicts for: settings (scoring, roster slots, draft type/date/order, team count), teams + rosters, `league.draft` picks, `free_agents(size=N, position=...)`, `box_scores(week)`. `sync.py` upserts into `league_settings`, `teams`, `players`, `roster_slots`, `draft_picks`, `sync_runs`. Live draft: `refresh()` then re-read `league.draft`.

**Riskiest assumption:** `league.draft` (view `mDraftDetail`) updates while a draft is in progress. Spike first with an ESPN mock draft. Fallback A: manual "they took X" tap on the board page. Fallback B: Playwright reader of the draft room.

### Draft (`draft/`)

- `board.py` pure functions over dicts: `apply_picks(board, picks)`, `roster_needs(roster, slots)`, `scarcity(board, by_position)`, `picks_until_mine(pick_order, current_pick, my_team_id, snake=True)`.
- `loop.py`: every `config.draft.poll_seconds` (5), sync draft picks; on new picks, update board, broadcast SSE `board_updated`; when `picks_until_mine <= config.draft.advise_within_picks` (default 2) run advisor.
- `advisor.py`: prompt from `prompts/draft_advice.md` with board top-N by tier, my roster and needs, scarcity, last N picks, retrieved notes for candidate players. Tools off. `--json-schema` for `{pick, reason, backups:[{name,reason}], watch_out}`. Retry once on parse failure with shorter prompt; on second failure emit a deterministic fallback (top of board by tier filtered by need). Persist to `advice` table and broadcast SSE `advice`.

### Jobs and scheduler (`jobs/`)

`registry.py` maps names to callables; cadences from `config.toml` `[jobs.<name>]` with `cron`/`interval`, `model`, `tools`, `timeout_s`, `enabled`. `scheduler.py` reads `phase` (`pre_draft`, `draft_live`, `in_season`) from `league_settings.draft_date` and the current date, and registers only the jobs for that phase. Each run writes `job_runs(job, started_at, finished_at, status, error, summary)`.

Jobs:
- `board_build` (pre_draft, daily + on demand): research rankings, ADP, injuries, rookies, tiers for this league's scoring; writes `board` table (player, position, team, tier, rank, note) and `notes`.
- `news_sweep` (in_season Wed/Sat): injuries and role changes for rostered + top free agents; writes notes.
- `waiver_scan` (in_season Tue): free agents worth claiming; writes advice.
- `lineup_check` (in_season Sun morning + Thu/Mon evening for prime-time games): start/sit; writes advice.
- `weekly_recap` (in_season Tue): what happened, what to learn; writes notes + advice.

### Web (`web/`)

FastAPI app. Auth: single shared password from `.env`, signed session cookie. Pages: `/draft` (board, my roster, live advice card, manual "taken" fallback), `/team`, `/advice` (feed with done toggle), `/chat` (streamed replies over SSE, session persisted, "remember this" writes a note), `/status` (last syncs, job runs, ESPN auth check, claude binary check). One in-process SSE bus; pages subscribe to `/events`.

### Error handling

ESPN failure keeps last snapshot, shows stale banner with age. Claude timeout/parse failure recorded on job row, raw output saved; draft advisor falls back to deterministic board. Hourly ESPN auth check on status page. Scheduler never crashes the web app: jobs run in a thread pool with exceptions captured.

### Testing strategy

- `claude_runner` against `tests/fake_claude/claude` (a Python script on PATH that replays a fixture keyed by an env var) covering argv construction, streaming, json-schema output, timeout, nonzero exit.
- ESPN client against recorded JSON fixtures (monkeypatch `espn_api` requests). Record real fixtures once cookies are available.
- `draft/board.py` pure-function unit tests (snake order, needs, scarcity).
- Jobs tested with fake runner + in-memory SQLite.
- Web routes via `TestClient`; SSE endpoint tested with a short-lived subscription.
- One live integration test `tests/integration/test_claude_live.py` runs `claude -p "reply with OK"` and skips if the binary or auth is missing.

### Deployment

- Dev: `uv run hal-mary serve --reload`. Stop it when not developing.
- Prod: new Proxmox VM (`hal-mary`), `provision-dev.sh` + `uv`, `claude` login once, clone to `~/src/hal-mary`, `deploy/hal-mary.service` as a systemd user unit with `loginctl enable-linger`, DB at `~/hal-mary-data/hal.db` on local disk. VM creation and auth provided by Bryan when ready.


---

## Spike results (2026-09-07)

Two spikes ran before implementation, both answerable without ESPN credentials.

### Spike 1 — `claude -p` invocation cost

Measured on `dev-scratch` with `claude` 2.1.260, model `opus`, on the prompt "Reply with exactly: OK".

| Invocation | Cost | Cache-creation tokens |
|---|---|---|
| Bare `-p --model opus --tools ""` | $0.823 | 82,289 |
| Plus `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` | $0.048 | 4,780 |
| Plus `--setting-sources ""` | $0.005 | 230 |

A bare `claude -p` inherits the operator's entire environment: every configured MCP server, plugin,
skill, and settings file. On this box that was 82,000 tokens of system prompt attached to a two-token
question, at 165 times the isolated cost.

**Every hal-mary invocation therefore carries all three isolation flags**, hardcoded in
`claude_runner` rather than exposed per job, so no job can omit them. Beyond cost, isolation removes a
correctness hazard: without it, a hal-mary job inherits whatever tooling happens to be installed on
the host, and its behavior changes when Bryan installs something unrelated.

Also confirmed under isolation:

- Web tools work. A search for current ESPN rankings returned 2026 data with a source URL in 22.8
  seconds for $0.17. The project's central premise holds.
- Structured output works. `--json-schema` puts a parsed object on the result event's
  `structured_output` key.
- A draft-advice-shaped call with tools off and a schema returned in 14.5 seconds, inside the pick
  clock with margin.
- `--verbose` is required alongside `--output-format stream-json` under `-p`.

### Spike 2 — reading the `espn-api` source

Two defects in `espn-api` 0.46.0 make it unsafe for the live draft poll.

`refresh_draft()` duplicates picks: `self.draft = []` runs only in `BaseLeague.__init__`, while
`_fetch_draft` appends without clearing. A five-second poll would grow the list without bound.

`_fetch_draft` returns early unless `draftDetail.drafted` is true. Whether ESPN sets that flag during
a live draft or only at completion is the assumption we cannot afford to be wrong about.

**Draft picks therefore come from the raw endpoint** and everything else from the library:

```
GET https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{id}
    ?view=mDraftDetail
```

`draftDetail.picks` is read directly and the `drafted` flag is ignored. That is roughly fifteen lines
of `httpx` and it removes both defects. The library still handles settings, teams, rosters, free
agents, and box scores, where its parsing earns its place.

This narrows the remaining open question to one thing: **does ESPN populate `draftDetail.picks` while
a draft is in progress?** It is answerable with a single request against a mock draft, and the manual
pick-entry path is built regardless, since it also covers ESPN being unreachable at the worst moment.
