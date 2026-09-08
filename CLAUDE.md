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
uv run hal-mary serve          # web app + draft loop on the LAN; --reload for development
uv run hal-mary sync           # pull league state and draft picks from ESPN
uv run hal-mary espn-check     # are the cookies still good? exits nonzero when not
uv run hal-mary job <name>     # run one job on demand (board_build)
```

## Architecture in one paragraph

FastAPI serves a phone-friendly web app (Jinja + HTMX + Server-Sent Events) backed by SQLite.
APScheduler runs research jobs inside the same process. `hal_mary.claude_runner` is the **only**
module that spawns the `claude` binary; everything else calls through it. Football reasoning lives in
Markdown prompt files under `prompts/`, not in Python — Python does bookkeeping (which players are
gone, which roster slots are open, how many picks until her turn). Memory is SQLite tables plus an
FTS5-indexed `notes` table that every job writes to and every prompt retrieves from.

## The timing constraint that shapes everything

A `claude -p` call with web search takes 30 to 120 seconds. This league's pick clock is **90
seconds**, confirmed from the live ESPN payload. **Therefore research happens before the draft
(`jobs/board_build.py`) and on-the-clock advice (`draft/advisor.py`) runs with web tools off,
against a board that is already built.** Any change that puts a web-enabled Claude call on the
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
- **Until the first pick lands, the order on screen is the placeholder, and the page says so.**
  ESPN's board can only be read once a real pick exists, so the window between the draft opening and
  pick 1 is uncorrected — and if Caroline was drawn first overall the placeholder puts her opening
  pick five away, past `draft.advise_within_picks`, so no card is written for the pick she is on. A
  `/sync` after the draft opens closes it, which is why `docs/SETUP.md` makes that step
  unconditional and `partials/turn.html` says "provisional" while `turn.started` is false.
- **Never poll a live draft through `espn-api`.** `refresh_draft()` appends to a list cleared only
  in the constructor, and `_fetch_draft` returns early unless `draftDetail.drafted` is true — a flag
  that may only be set once the draft is over. `hal_mary.espn.client.draft_picks()` reads the raw
  `mDraftDetail` endpoint and ignores that flag; see `docs/DECISIONS.md`.
- **The ESPN fixtures in `tests/fixtures/espn/` are synthetic** until someone runs
  `uv run python scripts/record_espn_fixtures.py` with real cookies — the one exception is
  `draft_detail_prepopulated_real_league.json`, built field for field from the real pre-draft
  payload. No test may reach the network; `tests/conftest.py` blocks both HTTP stacks.
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
- **`app.routes` does not contain your routes.** This FastAPI represents each
  `include_router` as one opaque `_IncludedRouter` object holding the original router, so a test
  that walks `app.routes` looking for paths finds three pathless objects and silently checks
  nothing. `flatten_routes` in `tests/unit/test_web.py` unwraps them, and the test asserts the
  paths it expected to find before it asserts anything about them.
