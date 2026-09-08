# CLAUDE.md — how to work in this repo

`hal-mary` is a Claude-powered fantasy football advisor. It watches Caroline's ESPN league,
researches the live internet through the local `claude` binary, and tells her — in plain English,
assuming zero football knowledge — who to draft, start, and claim.

**This application still never writes to ESPN itself.** For the draft that is the whole story:
Caroline makes every click. For in-season lineup and waiver changes it is now the front half of a
split — hal-mary decides and emits an *action*, and Claude Cowork's browser performs it in ESPN's own
interface through the MCP endpoint at `/mcp`. **hal-mary decides, Cowork executes, Cowork reports
back; Cowork never chooses.** That is a security boundary, not a tidy separation: Cowork's browser
reads pages five other league members write into, so an executor with no discretion gives injected
text nothing to redirect. See
[`docs/superpowers/specs/2026-09-07-cowork-manager-design.md`](docs/superpowers/specs/2026-09-07-cowork-manager-design.md)
and [`docs/COWORK.md`](docs/COWORK.md).

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

`.github/workflows/ci.yml` runs `uv run pytest` and `uv run ruff check src tests scripts` on every
pull request and every push to `main`, so "`main` stays green" no longer depends on someone
remembering. It runs with no `.env` and no secrets — a test that needs one is a broken test.

A test that asks about the **box** rather than about this code guards its precondition and skips,
naming what is missing in the skip reason: `_require_claude_code` in `tests/unit/test_doctor.py`
and `_why_systemd_verify_cannot_run` in `tests/unit/test_deploy.py`. Skipping for an absent
precondition is honest — the test still runs, and still bites, on every box that has it. Loosening
an assertion so it passes everywhere is not, and this repo has spent a day finding tests that
quietly checked nothing. Do the first; never the second.

Commit messages: imperative subject, body explaining *why*. Every commit made by an agent carries
the `Co-Authored-By` and `Claude-Session` trailers.

## Commands

```bash
uv sync                        # install deps
uv run pytest                  # full test suite
uv run pytest tests/unit -x    # fast loop while developing
uv run hal-mary serve          # web app + draft loop on the LAN; --reload for development
uv run hal-mary sync           # pull league state and draft picks from ESPN
uv run hal-mary espn-check     # are the cookies still good? exits nonzero when not
uv run hal-mary job <name>     # run one job on demand (board_build)

uv run hal-mary cowork-config  # Cowork's scheduled tasks, rendered for this league; --json
uv run hal-mary job <name>     # run one job on demand

uv run hal-mary doctor         # can this box run hal-mary? nonzero on a fatal problem
uv run hal-mary migrate        # apply pending schema migrations
uv run hal-mary backup         # snapshot the database, prune to backup.keep

# the chat page is at /chat; it is the one job with web tools on the request path
uv run hal-mary job <name>     # run one job on demand, by name
uv run hal-mary jobs           # list the jobs, their cadence and their last run
```

Deployment lives in `deploy/` — a systemd **user** unit, `install.sh` and `deploy.sh` — and the
runbook is the second half of `README.md`. Both scripts are covered by `tests/unit/test_deploy.py`,
which drives them against a fabricated box; `shellcheck --severity=style deploy/*.sh` must be clean.

## Architecture in one paragraph

FastAPI serves a phone-friendly web app (Jinja + HTMX + Server-Sent Events) backed by SQLite.
APScheduler runs research jobs inside the same process, started from `web.serve.start_scheduler` on
lifespan startup; `hal_mary.jobs.registry` is the one table of jobs and the one place a `job_runs`
row is written. `hal_mary.claude_runner` is the **only**
module that spawns the `claude` binary; everything else calls through it. Football reasoning lives in
Markdown prompt files under `prompts/`, not in Python — Python does bookkeeping (which players are
gone, which roster slots are open, how many picks until her turn). Memory is SQLite tables plus an
FTS5-indexed `notes` table that every job writes to and every prompt retrieves from.

## The timing constraint that shapes everything

A `claude -p` call with web search takes 30 to 120 seconds. This league's pick clock is **90
seconds**, confirmed from the live ESPN payload. **Therefore research happens before the draft
(`jobs/board_build.py`) and on-the-clock advice (`draft/advisor.py`) runs with web tools off,
against a board that is already built.** The five-second poll is draft night only: outside a live
draft the loop idles at `draft.idle_poll_seconds` and stops entirely once the board is full. Any change that puts a web-enabled Claude call on the
pick-clock path is wrong; `tests/unit/test_advisor.py` asserts the `draft_advice` job's tool list is
empty for exactly that reason.

## Gotchas

- **The ESPN API is unofficial.** Cookies (`ESPN_S2`, `SWID`) expire. The status page checks auth
  hourly; when it breaks in-season, that is the first thing to look at.
- **ESPN pre-populates the whole draft board before the draft starts.** `draftDetail.picks` holds
  one row per slot — 96 for a 6-team, 16-round league — every one with `playerId: -1` and no name,
  from the moment the league exists. A pick counts only when a real player is attached to it;
  `hal_mary.espn.client.pick_is_made` is the single definition and `draft_picks()` applies it at the
  boundary so nothing downstream has to. The empty rows are the pick schedule, not noise:
  `draft_schedule()` returns them.
- **`draftSettings.pickOrder` is provisional until the draft opens.** This league's `orderType` is
  `DRAFT_START`, so ESPN assigns the real order when the draft begins. Never cache a pre-draft order
  or a pre-draft `draft_schedule()`. On the first poll that sees a real pick,
  `DraftLoop._read_schedule` writes round one of ESPN's board to the **`draft_order` table**, once,
  and `league._espn_order` prefers it from then on. It is its own table rather than a
  `league_settings` column because that row is rewritten wholesale by every sync — a column there
  would be erased, mid-draft, by the `/sync` button. That write happens **once**, so it is validated
  before it happens: a first round shorter than the number of teams the board itself names is
  refused, because ESPN's first poll after pick 1 can catch the board mid-write and a distinct
  four-team order for a six-team league would then be stored permanently, discarded on every load,
  and lock out the good board for the rest of the night.
- **The draft page, the advisor and the draft loop read one draft order, through
  `LeagueContext.draft_order`.** Never hand one of them a pick window computed somewhere else. All
  three run the same snake arithmetic over the same list, so when that list is wrong they are wrong
  *together*: nothing on the page contradicts anything else, there is no staleness flag for it, and
  the advisor's own prompt asserts the false position too. Giving the advisor a separate source also
  makes the card's label disagree with the page's on every turn, which is the false-staleness bug
  `docs/DECISIONS.md` already records. `tests/unit/test_draft_order_source.py` pins all three
  against a fixture where ESPN's board and the stored `pickOrder` genuinely disagree — that fixture
  is the point of the test, because before it existed the fake client's default schedule *was* the
  snake and the divergence had never once been exercised. The **league page reads it too** —
  `teams.draft_slot` is the pre-draft placeholder, re-seeded by every sync, so rendering that column
  under a "Draft order" heading would put one screen in visible contradiction with another.
- **The page says the pick numbers are provisional until the drawn order has been read, and the
  predicate is `turn.order_drawn`, never `turn.started`.** ESPN's board can only be read once a real
  pick exists, so the window between the draft opening and pick 1 is uncorrected — and if Caroline
  was drawn first overall the placeholder puts her opening pick five away, past
  `draft.advise_within_picks`, so no card is written for the pick she is on. A `/sync` after the
  draft opens closes that, which is why `docs/SETUP.md` makes that step unconditional. But "the
  draft has started" is not what makes the numbers trustworthy: with picks entered by hand and
  nothing polling ESPN, the order is never read and the placeholder runs all night, so a note gated
  on `started` would vanish at pick 1 in exactly the case that needs it most. `partials/turn.html`
  gates on the stored order being absent and says something plainer once the draft is running,
  because by then nothing is on course to correct the numbers by itself.
- **Never poll a live draft through `espn-api`.** `refresh_draft()` appends to a list cleared only
  in the constructor, and `_fetch_draft` returns early unless `draftDetail.drafted` is true — a flag
  that may only be set once the draft is over. `hal_mary.espn.client.draft_picks()` reads the raw
  `mDraftDetail` endpoint and ignores that flag; see `docs/DECISIONS.md`.
- **`EspnClient.current_week()` distinguishes two failures.** No cookies at all raises
  `EspnAuthError` — that is a configuration problem the status page must report. ESPN answering
  badly (401, an outage, a nonsense value) returns `None`, because inventing a week from the
  calendar would move the bye check onto the wrong players, which is worse than not making it.
  Every caller handles `None`; `jobs/season.current_week` falls back to the database and then to
  the week the model established.
- **The ESPN fixtures in `tests/fixtures/espn/` are synthetic** until someone runs
  `uv run python scripts/record_espn_fixtures.py` with real cookies — the one exception is
  `draft_detail_prepopulated_real_league.json`, built field for field from the real pre-draft
  payload. No test may reach the network; `tests/conftest.py` blocks all **three** HTTP stacks in
  play — `httpx` (our raw ESPN reads), `requests` (what `espn-api` uses) and `httpx2` (which arrives
  with the `mcp` SDK). Adding a dependency that brings a fourth means adding it there too.
- **`memory/league.md` is generated, gitignored, and contains real people.**
  `hal-mary sync` rewrites it from the live ESPN payload: real leaguemates' names and the league
  id. Only the placeholder `memory/league.example.md` is tracked. Never `git add -f` it, never
  re-track it, and never paste its contents into a commit, an issue, or a test fixture. Names in
  git history cannot be removed by a later commit. `tests/unit/test_project_files.py` pins both the
  ignore rule and the template's shape.
- **hal-mary must be able to run a draft with no ESPN at all.** Picks have a manual path
  (`draft.loop.record_manual_pick`); the league's own settings have the commented-out `[league]`
  section of `config.toml`. `hal_mary.league.load_league_context` applies the precedence — a synced
  row wins field by field — and is the *only* way `board_build` and the advisor read league
  settings. Do not add a second path.
- **A pick that names nobody is ignored downstream too.** `EspnClient` already filters ESPN's
  pre-populated slots, and `draft/store.py` and the draft loop ignore any recorded pick with no name
  and no positive player id — counting one puts the next pick at 97, which reads as "the draft is
  over" before it has begun. Research-built board ids start at **-1001** so a `-1` can never collide
  with a real board row.
- **A hand-entered pick is numbered from the picks, never from the rowid.**
  `draft_picks.overall_pick` is an INTEGER PRIMARY KEY, so a NULL insert takes one past the highest
  *row number* — and any database that synced before `pick_is_made` existed still holds ESPN's 96
  placeholder rows, which made the first pick of the night **97**. `draft_phase(97, 96)` is `done`:
  the loop stopped for good, on the one night the manual path exists for, with nothing on the page
  saying so. `record_manual_pick` uses `store.next_overall_pick` and upserts, and the cadence counts
  picks (`store.picks_made`) rather than reading the highest number. Both locks matter; keep both.

- **`/mcp` has its own key, and it is not `WEB_PASSWORD`.** `MCP_TOKEN` opens the MCP endpoint and
  nothing else; a session cookie does not open `/mcp` and the MCP token does not open the dashboard.
  Two doors, because the tunnel exposes `/mcp` to the internet and the dashboard is LAN-only. With
  `MCP_TOKEN` unset the endpoint answers 503 — **absent never means open** — and
  `tests/unit/test_mcp.py` pins all of it.
- **The MCP tool descriptions are part of the product.** They are the only instructions Cowork ever
  gets. Edit them like user-facing copy, and never write one that asks Cowork to choose between
  options; a test walks every description looking for exactly that.
- **`memory.TRUSTED_SOURCE_JOBS` is an allowlist, and everything else is quarantined.**
  `build_context` runs *two* complementary queries: the trusted one asks for that set and nothing
  else, and the untrusted one asks for everything that is not on it — including a note with no
  `source_job` at all — rendering them under their own heading with their own smaller budget so a
  flood cannot crowd out real research. **That split is the boundary; the tag is only how it is
  recognised.** It is an allowlist rather than a blocklist so a mistyped or unregistered tag fails
  *closed*: under a blocklist, `Cowork-Browser` and `cowork_browser` were both "not the constant" and
  landed in the trusted section. A new job whose notes would be quarantined fails a test in
  `test_memory.py` rather than going quiet.
- **Every value that reaches a prompt goes through `hal_mary.prompt_text.one_line`.** Not just the
  ones that look dangerous. It collapses on all Unicode whitespace, because a value carrying
  `\n\n## What you always know` closes its own section and opens a forged one in the most trusted
  part of the prompt. This boundary has now been dropped **three times, by three different
  renderers**: `memory._render_note` dropped `source_job`; it then collapsed `text` and appended
  `source_url` raw (caller-supplied by `report_observation`); and `espn.sync._league_memory_body`
  interpolated team names, abbreviations, owners and the league name straight into
  `memory/league.md`, which `standing_memory()` reads whole into section one. **A leaguemate renames
  their team and it syncs into standing memory** — team names are the first example in this
  project's own threat statement. The collapser lives in its own module so the next renderer
  inherits the defence instead of remembering it, and every test of it asserts on the **whole**
  rendered output: a test that asserts on a slice reads as though it checks everything and checks
  only the half its author was thinking about.
- **The MCP reporting tools cap their inputs** (`MAX_OBSERVATION_CHARS`, `MAX_DETAIL_CHARS`,
  `MAX_URL_CHARS`) and refuse with a `ToolError`, which is the only exception type whose message the
  SDK puts in front of Cowork — anything else becomes "Error executing tool <name>" and a cap the
  caller cannot read is one it keeps hitting. Unbounded, one observation produced a 2.5 MB memory
  block, and a prompt that size on a 90-second pick clock is a draft nobody gets advice in.
- **Every emitted action carries a deadline, and it is the end of that NFL week.** Without one there
  is no expiry *and* no other revocation path: `expire_stale` only touches rows that have a deadline,
  so an instruction emitted in week 5 would still be pending in week 7 and an executor that had been
  offline would come back and bench a healthy starter. `[actions].week_boundary_*` sets the
  rollover, and the same boundary scopes emission equivalence — the same bench twice on a Sunday is
  one click, the same bench next Saturday is a new decision about a new situation.
- **Cowork's scheduled prompts live in `cowork/tasks.toml`, not in code.** A Cowork scheduled task is
  a saved prompt on a cadence, so the prompt *is* the cron job. Every prompt there is static and
  generic — no player, no week, no strategy — and `mode = "read_only"` is enforced: the loader
  refuses a read-only task that lists `report_action` *or* `pending_actions`. `docs/COWORK.md`
  carries the same prompts, generated by `scripts/render_cowork_doc.py`, and a test re-runs it with
  `--check` so the two cannot drift. Note what `ACTING_TOOLS` does **not** cover: the browser that
  same session is holding, which is logged into ESPN. The read-only prompts therefore say hal-mary
  has given them no tool that changes anything, rather than claiming they are incapable of it.
- **`waiverHours` is not the hour waivers are processed.** It is how long a player sits on
  waivers before he clears; the processing hour is `waiverProcessHour`. Caroline's league sets the
  first to 24, which is not an hour of any day, so reading it as the hour left the waiver run with
  no time at all. `waiverProcessDays` is a *list* and this league names six of them — Monday and
  Wednesday through Sunday — so "the processing day" is not a single value either.
  `cowork.waiver_settings` returns every day, and `_derive_waivers` aims at the batch that follows
  hal-mary's own waiver scan, because a claim run in front of the scan that fills its queue submits
  nothing, reports success, and is silent about it.
- **This league's flex slot is spelled `RB/WR/TE`, not `FLEX`.** Prose that explains "a FLEX slot"
  defines a term that appears nowhere on Caroline's screen.
- **Database on local disk, never on NFS.** In this homelab `/var/data` is a TrueNAS NFS export
  mounted on every node, and SQLite on NFS corrupts. The production VM keeps `hal.db` on its own
  disk.
- **Every configured path is absolute by the time you see it.** `hal_mary.config` anchors
  `paths.*`, `claude.scratch_dir`, `claude.system_prompt_file` and `DB_PATH` to the directory
  holding the resolved `config.toml` — never the working directory, which is the checkout for you
  and something else under systemd. Use `settings.paths.memory_dir` as given; a `Path(...)` around
  it is the bug, not a safety net. `Settings.resolved_paths()` is what the status page renders.
  See `docs/DECISIONS.md`.
- **`DB_PATH` defaults to `~/hal-mary-data/hal.db`, not `./hal.db`.** `.env.example` ships it
  empty, and every configured path is anchored to `config.toml`'s directory — so a relative default
  put the database, and the `backups/` that follows it, *inside the checkout*: the one directory a
  deploy replaces and a rollback moves. `doctor`'s `database location` check reports a database
  under the checkout, and is fatal once `~/hal-mary-data` exists. Two tests in `test_config.py` pin
  the default; do not make it relative again to make a test tidier.
- **`doctor` resolves `claude` against the *unit's* PATH, not the caller's.** `hal_mary.doctor.
  UNIT_PATH` mirrors `Environment=PATH=` in `deploy/hal-mary.service`, and
  `tests/unit/test_deploy.py` asserts the two are identical so they cannot drift. Checking only
  `os.environ["PATH"]` is wrong in both directions: Ubuntu's `.bashrc` returns early for a
  non-interactive shell, so `~/.npm-global/bin` is missing under `ssh host 'cmd'` (fails a healthy
  box), and a `claude` somewhere only your login shell can see passes while the service still
  cannot find it.
- **`serve` boots degraded; `hal-mary doctor` is what refuses.** There is no startup preflight.
  A service that will not start because the memory directory is missing is down at 2am with nobody
  watching, and the page that would have said why is served by the process that refused to start.
  So the checking that *stops* something happens in `install.sh` and `deploy.sh`, where a human is
  looking, and the running service reports the same facts on `/status`. `Check.fatal` in
  `hal_mary.doctor` is a policy dial — "should an install stop over this" — not a severity label,
  and nothing in doctor may ever gate `serve`. Doctor touches no network and spawns no process;
  `espn-check` is the separate command that asks ESPN. See `docs/DECISIONS.md`.
- **The systemd unit sets `Environment=PATH=` explicitly, and that line is load-bearing.** A
  systemd user unit does not inherit the interactive shell's PATH, and both `uv` and `claude` live
  under `~/.local/bin` and `~/.npm-global/bin`. Drop them and the service starts, serves every page,
  and every Claude call fails with `claude: not found` — up and useless, which is the worst state.
  `tests/unit/test_deploy.py` pins both directories.
- **Never back up the database with `cp`.** It is a WAL database written to while the service runs,
  so a copy of the main file alone is a torn snapshot that opens cleanly and has lost the newest
  notes. `hal-mary backup` uses SQLite's online backup API, and it is a subcommand rather than a
  shell script because `DB_PATH` is anchored to `config.toml`'s directory and a script would
  re-derive it wrongly. See `docs/DECISIONS.md`.
- **A missing memory directory warns and shows on the status page; it never raises.**
  `standing_memory()` is on the pick-clock path, so thinner advice beats no advice. An existing but
  empty directory is silent — "there are no notes" and "I am looking in the wrong place" are
  different problems and the status page says which.
- **The `claude` child gets `claude_runner.ENV_PASSTHROUGH` and nothing else.** No `ESPN_S2`, no
  `SWID`, no `WEB_PASSWORD`, no `ANTHROPIC_*`; the binary authenticates from `~/.claude` under
  `HOME`. Do not widen that list without saying why in `docs/DECISIONS.md` and running the live
  integration test.
- **Tests must never spawn the real `claude` binary** except the one live integration test, which
  skips when the binary or its auth is missing. Unit tests point `config.claude.binary` at
  `tests/fake_claude/claude`, which is driven by a `fake_knobs.json` in its working directory —
  deliberately not by environment variables, which the allowlist above would have to be widened
  for.
- **`TestClient` cannot test a stream.** Starlette's `TestClient` (and httpx's ASGI transport)
  buffer the whole response before returning it, so an endless response like `/events` deadlocks
  them and neither can deliver an `http.disconnect`. Drive the app at the ASGI layer instead; see
  `EventProbe` in `tests/unit/test_web.py`.
- **The web app opens a connection per request**, from the `connect` factory passed to
  `create_app`. Anything running off the event loop — a sync, the draft loop, a scheduled job —
  opens its own connection inside its own worker. See `docs/DECISIONS.md`.
- **Every POST on the private router needs a CSRF token.** `create_app` puts
  `Depends(require_csrf)` on the router beside `Depends(require_session)`, so a state-changing route
  is protected by construction. Rendered forms get the token from `page()` / the fragment renderer;
  a test that posts to a private route goes through `post()` in `tests/unit/test_web.py`, and one
  that posts without a token is testing the 403. See `docs/DECISIONS.md`.
- **`web.shutdown_timeout_s` is what makes `SIGTERM` work at all.** uvicorn waits for open
  connections *before* running lifespan shutdown, which is where the draft loop is told to stop —
  and `/events` never ends while a phone has the page open. Unbounded, a `SIGTERM` to a server with
  one page open never completes and the loop keeps polling; measured still alive at 30 seconds.
  `run_server` passes it as `timeout_graceful_shutdown`. Verify shutdown **with a client attached**;
  without one the bug does not appear.
- **The draft loop runs on its own thread with its own connection.** `DraftLoopThread` opens the
  connection *inside* the thread — `db.connect` leaves `check_same_thread` on — and
  `web.serve.app_from_env` starts it with the app's own `EventBus`. A loop that will not start is
  logged and nothing more: the page must render, and picks must be enterable by hand, on a box with
  no ESPN credentials at all.
- **The draft page derives "working on it" rather than storing it.** The advisor publishes when it
  is *done* and says nothing when it starts, so `draft_page.draft_context` infers it: her pick is
  inside `draft.advise_within_picks` and the newest card is not for the pick on the clock. Anything
  that changes when the advisor runs has to change that inference too, or the card will read as
  current when it is not.
- **Position codes never stand alone.** `web/positions.py` is the one place slot and position labels
  live, shared by the team page and the draft page. "QB" is not a word Caroline has any reason to
  know, so no heading, filter or empty state may be a bare code — and the flex slot is labelled by
  what it accepts rather than called a "flex".
- **The draft loop has three cadences, and the board picks which.** `draft_phase` in
  `draft/loop.py` reads two numbers off the `mDraftDetail` payload the loop already fetches — how
  many slots ESPN's board has, and how many hold a real player — and returns idle
  (`draft.idle_poll_seconds`), live (`draft.poll_seconds`) or done, which stops the loop. **The
  `drafted` and `inProgress` flags are reported by `EspnClient.draft_status()` and decide nothing**:
  `drafted` is set late (that is why the raw endpoint exists) so it cannot stop the loop, and
  `inProgress` describes the lobby, not picks, so it cannot start the fast clock. A `drafted: true`
  over a partly filled board keeps polling and logs the discrepancy. Five seconds forever is
  2,073,600 requests a season for a job that needs 2,160; see `docs/DECISIONS.md`.
- **"The draft has started" is an override, never the mechanism.** `POST /draft/started` puts the
  loop on live cadence and syncs, but the loop still finds the draft on its own within one idle
  interval from the first pick ESPN reports. Any change that makes the button load-bearing is wrong:
  it is the control someone forgets on the one night it matters. The override expires after
  `draft.live_override_seconds`.
- **`DraftLoop.stop()` must stay callable from another thread, and must wake the wait.** The web
  app's shutdown calls it from its own thread. `asyncio.Event.set` from off the loop resolves the
  waiter through `call_soon`, which never writes the loop's self-pipe, so the loop sleeps out its
  full timeout — five minutes on the idle cadence, once per deploy, each one leaving a thread still
  polling ESPN. The stop flag is a `threading.Event` and the wake-up goes through
  `call_soon_threadsafe`, the same rule `hal_mary.events` follows.
- **Chat is the only caller that keeps a Claude session.** `ClaudeRunner` adds
  `--no-session-persistence` to every one-shot job; `hal_mary.chat` passes `persist_session=True`
  so the CLI keeps the session whose id lands in `chat_sessions.claude_session_id` and the next
  message can `--resume` it. A failed call clears that id, because a session the CLI no longer holds
  fails every message after it. See `docs/DECISIONS.md`.
- **The chat stream opens its connection inside the worker.** `ClaudeRunner.stream` is a blocking
  generator that finishes by writing a `claude_calls` row, and `db.connect` leaves
  `check_same_thread` on — so `web/chat_page.py` does the whole call in one `run_in_threadpool`:
  open the connection, build the runner, drain the answer, hand chunks across a queue.
  `iterate_in_threadpool` hops threads per `next()` and fails on the last chunk *intermittently*,
  which is the worst way for it to fail. The runner's docstring carries the pattern.
- **One live chat answer per conversation.** `chat_page.Answering`, one per app, is the interlock;
  a second stream for a conversation already being answered says so and ends. Without it a phone
  reconnecting three times buys three concurrent Claude calls and gets three answers to one
  question. Per app, not per process: two apps in one process is every test run.
- **A chat question is recorded by the POST and answered by the GET**, and which question a stream
  answers is derived (`chat.pending_question`), never stored. Anything that changes when the reply is
  persisted changes what the page believes is outstanding.
- **Every job has one shape, and `run_job` is the only thing that records a run.**
  `run(conn, settings, runner, client) -> str`, registered with
  `@registry.register(name, phases=(...))`. `registry.run_job` opens and closes the `job_runs` row,
  catches `BaseException` (not just `Exception`) and **never re-raises** — a failing job must not
  take the web process down. The two exceptions: `KeyboardInterrupt` / `asyncio.CancelledError` are
  re-raised, and `UnknownJob` is raised *before* a row is opened. A job that does its own
  bookkeeping writes a second row and the status page reports a run that started twice; see
  `docs/DECISIONS.md`.
- **The job modules are imported lazily, from `registry.JOB_MODULES`.** They import `register` from
  the registry, so the registry must not import them at module level. Adding a job means adding its
  module to that tuple; forgetting to is why `hal-mary job <name>` says the name does not exist.
- **A started player on a bye scores zero, and the alarm for it is arithmetic.**
  `jobs/lineup_check.py` computes it in Python from the roster and the week, writes it as its own
  `advice` row, and writes it **even when the Claude call fails**. Either source saying bye is
  enough — over-flagging costs ten seconds, under-flagging costs the week. Do not make it depend on
  a model call.
- **There is no bye week in `players`.** `season.bye_weeks` reads `board.bye_week` and keys it by
  **normalised name**, because a board row researched before the first sync carries a synthetic
  negative id that will never join to a roster row.
- **Name the weekday in a cron, never number it.** APScheduler's
  `CronTrigger.from_crontab` counts `day_of_week` from **Monday**; crontab(5) counts from Sunday.
  So `0 9 * * 0` — the obvious spelling of "Sunday morning" — fires on **Monday**, after every
  Sunday game has been played. Use `sun`/`tue`/`wed,sat`. `tests/unit/test_scheduler.py` refuses a
  digit in that field and asserts the computed `get_next_fire_time`, because asserting the cron
  *string* renders somewhere catches none of this.
- **Cadences are read in `scheduler.timezone`, not UTC.** These jobs are timed against NFL
  kickoffs; "Sunday morning" in UTC is 02:00 Pacific. `scheduler_timezone(settings)` is the one
  reader, and `misfire_grace_time_s` is set because APScheduler's default grace is *one second* — a
  fire missed while the loop was blocked is otherwise dropped in silence.
- **There are two schedules and they have to interleave.** hal-mary's own jobs run in-process in
  `[scheduler].timezone`; Cowork's run in Cowork at times pasted from `hal-mary cowork-config`, in
  `[cowork].timezone`. hal-mary *decides* and queues the actions, Cowork *performs* them, so every
  Cowork lineup run must sit **after** that day's `lineup_check` and **before** kickoff. The two
  zones must match: `tests/unit/test_schedule_agreement.py` refuses a config where they do not, and
  `cowork.render` warns in the rendered output. Move one `at` in `cowork/tasks.toml` without
  checking the other schedule and the Cowork run finds an empty queue and correctly reports that
  there was nothing to do — forever.
- **A job may have several cadences.** `JobConfig.cron` takes a string or a list; use
  `config.crons` / `config.cadence`, never `config.cron`. `lineup_check` has three, because ESPN
  locks each player at **his own kickoff**: a Thursday starter ruled out on Wednesday is lost by
  Sunday morning. Each cadence is its own APScheduler id — `lineup_check`, `lineup_check#2` — so
  `max_instances=1` still means one copy of each.
- **`season.current_week` reads `LeagueContext.current_week`, not `roster_slots.week`.**
  `espn.sync` writes every roster row of the current snapshot with a NULL week, so that column
  looks like a source and is not. The order is ESPN, then the week the last sync stored, then
  `None` — never the calendar. The fallback is what covers cookies that expired on Friday.
- **An empty bye list means two different things and must not render as one.**
  `lineup_check.ByeCheck` carries `checked` (was the week known at all) and `unchecked` (starters
  with no bye week on file — anyone added after the board was built). Both reach the summary and the
  lineup card. Silence here reads as "nobody is on a bye", which is the one sentence she must not be
  told wrongly.
- **Which jobs are scheduled depends on `scheduler.current_phase`**, re-checked daily by the
  reserved `_phase_check` job. `max_instances=1` and `coalesce=True` on everything. A scheduled run
  opens its own connection and its own `ClaudeRunner` inside its own thread.
- **`[research]`, not `[season]`.** `Settings.season` is already the ESPN season year, so a
  `[season]` config section cannot exist. The in-season prompt sizes live under `[research]`.
- **Advice bodies are untrusted text.** They carry what a model wrote from the open web, so
  `web.app.advice_body` escapes first and *then* honours `**bold**` and blank lines. Nothing else is
  interpreted, and the order must not be reversed.
- **`app.routes` does not contain your routes.** This FastAPI represents each
  `include_router` as one opaque `_IncludedRouter` object holding the original router, so a test
  that walks `app.routes` looking for paths finds three pathless objects and silently checks
  nothing. `flatten_routes` in `tests/unit/test_web.py` unwraps them, and the test asserts the
  paths it expected to find before it asserts anything about them.
- **The deploy's environment must not reach the suite it gates on.** `deploy.sh` re-execs itself
  after the pull with `HAL_MARY_REEXEC=1` and `HAL_MARY_PREVIOUS=<sha>`, then runs `uv run pytest` —
  and `tests/unit/test_deploy.py` spawns `deploy.sh` subprocesses. Inheriting those markers made
  three tests fail from inside a deploy and nowhere else, which meant the guardrail refused every
  restart forever. The suite is now run with the two removed for that one command, and `Box.env`
  drops every inherited `HAL_MARY_*` before setting the fabricated box's own. Anything either script
  learns to set has to be scrubbed the same way; `docs/DECISIONS.md` says why.
