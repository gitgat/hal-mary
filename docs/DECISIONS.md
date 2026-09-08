# Decisions

Dated entries for non-obvious choices and every reversal, newest last. Read before re-opening a
settled question. Each entry says what was decided, why, and what would make us change our minds.

---

## 2026-09-07 — The bot advises; Caroline clicks

**Decision:** hal-mary is read-only against ESPN. It never makes a pick, sets a lineup, or submits a
waiver claim on her behalf.

**Why:** Driving ESPN's draft room would mean browser automation against her logged-in account,
which is fragile against UI changes, plausibly against ESPN's terms, and carries a failure mode
where a bot makes a bad pick nobody chose. Advice she acts on has no such failure mode — the worst
case is she ignores it.

**Would revisit if:** she asks for autopilot after a season of trusting the advice, and only for
low-stakes actions like a waiver claim she has already approved.

---

## 2026-09-07 — Shell out to the `claude` binary rather than use the API

**Decision:** All model calls are `claude -p` subprocesses against the locally authenticated CLI.

**Why:** Bryan has a Claude subscription and no API key budget for this. The CLI carries the
subscription auth, ships web search and fetch as built-in tools, and supports structured output
(`--json-schema`), streaming (`--output-format stream-json`), and conversation continuation
(`--resume`). That covers everything the design needs.

**Cost:** Subprocess latency, and output parsing instead of typed SDK objects. Contained by putting
every invocation behind one module, `hal_mary.claude_runner`.

**Would revisit if:** an API key becomes available and per-call latency starts to matter more than
cost.

---

## 2026-09-07 — Prepared board, fast advisor

**Decision:** Deep research runs on a schedule before the draft and writes a tiered board to the
database. On-the-clock advice runs with web tools disabled against that board.

**Why:** A web-enabled Claude call takes 30 to 120 seconds. This league's pick clock is 90 seconds
(`draftSettings.timePerSelection`, confirmed from the live payload).
Doing research on the clock loses the pick. Splitting the work means the slow, expensive thinking
happens when there is time for it, and the fast call only has to reason over facts already gathered.

**Would revisit if:** measured on-the-clock latency with tools off exceeds about 20 seconds, in
which case the advisor drops to a deterministic board lookup with Claude explaining after the fact.

---

## 2026-09-07 — Models and tunables in `config.toml`, defaulting to Opus

**Decision:** No model name, timeout, cadence, tool allowlist, or budget is hardcoded. All of it
lives in `config.toml` keyed per job. Default model is `opus`.

**Why:** Bryan's explicit instruction. New models ship regularly and cost optimization is a decision
he wants to make by editing config, not Python.

---

## 2026-09-07 — Football reasoning lives in prompt files, not Python

**Decision:** `prompts/*.md` holds the strategy, tone, and evaluation criteria. Python holds
bookkeeping only: removing drafted players, counting open roster slots, computing picks-until-turn.

**Why:** Neither author of this repo knows football. Encoding half-understood heuristics in Python
would bake in mistakes we cannot see. Keeping them in prose lets the model apply current expertise
and lets us tune strategy without a code change or a test rewrite.

---

## 2026-09-07 — Entirely AI-authored, autonomous branch and merge

**Decision:** The agent creates branches, opens PRs, self-reviews with a reviewer subagent, and
merges to `main` without waiting for human approval. Durable notes live in `CLAUDE.md`, this file,
and the design doc — not in chat.

**Why:** Bryan's explicit grant, scoped to this repo. The draft is inside 48 hours and he is not
reviewing every diff. Chat context does not survive between sessions; files do.

**The discipline that pays for it:** TDD, a green `main`, and no completion claim without command
output. Those are recorded as hard rules in `CLAUDE.md`.

---

## 2026-09-07 — Every Claude call runs in an isolated environment

**Decision:** `claude_runner` passes `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` and
`--setting-sources ""` on every invocation. These are hardcoded in the runner, not exposed per job,
so no job can omit them.

**Why:** measured on this box, on the same trivial prompt:

| Invocation | Cost | Cache-creation tokens |
|---|---|---|
| Bare `-p --model opus` | $0.823 | 82,289 |
| Plus strict MCP config | $0.048 | 4,780 |
| Plus empty setting sources | $0.005 | 230 |

A bare `claude -p` inherits the operator's whole environment: every MCP server, plugin, skill, and
settings file installed for the user. That was 82,000 tokens of system prompt on a two-token
question, at 165 times the isolated cost. For a service running scheduled research jobs and answering
on a pick clock, that difference is the cost model.

Cost is not the only reason. Without isolation, a hal-mary job inherits whatever tooling happens to be
installed on the host, so its behavior would change when Bryan installs something unrelated to this
project. The bot must depend only on what this repository declares.

**Would revisit if:** a job genuinely needs an MCP server, in which case it gets its own explicit
`--mcp-config` rather than inheriting the operator's.

---

## 2026-09-07 — Draft picks come from the raw ESPN endpoint, not the library

**Decision:** `espn-api` handles league settings, teams, rosters, free agents, and box scores. Draft
picks are fetched directly:

```
GET https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{id}
    ?view=mDraftDetail
```

reading `draftDetail.picks` and ignoring the `drafted` flag.

**Why:** reading the library's source turned up two defects that matter only for live polling.
`refresh_draft()` appends picks to a list that is cleared only in the constructor, so a five-second
poll grows it without bound. And `_fetch_draft` returns early unless `draftDetail.drafted` is true,
which may only happen once a draft completes — the window we care about is exactly the window it
might report nothing. Fifteen lines of `httpx` removes both problems.

**Cost:** we map player ids to names ourselves, which the library would otherwise do, and we now
run two HTTP stacks against one API — `requests` inside the library, `httpx` for ours. Contained by
keeping the raw path to a single method and giving the whole package one exception hierarchy:
`EspnAuthError`, `EspnLeagueNotFound`, `EspnUnavailable`.

**Pinned by:** `test_draft_picks_ignores_the_drafted_flag`, which feeds a payload with
`drafted: false` and three picks and asserts three picks come back. The library returns nothing for
that same payload.

**Would revisit if:** the library fixes both defects upstream.

---

## 2026-09-07 — Deployment targets the VM by FQDN, never the short name

**Decision:** everything that reaches the production VM uses `hal-mary.thehalf.io` or its IP.

**Why:** the bare short name `hal-mary` has no DNS record. It falls through Pi-hole's wildcard for the
domain and resolves to the keepalived ingress VIP, which currently answers as `birdo` — the swarm
manager and ingress host. An SSH to the short name during setup connected successfully and landed
there. Running the install steps would have put Node, uv, Claude Code, and a long-running service onto
the cluster's control plane, on a Pi booting from an SD card.

The homelab notes already warn never to target that VIP, because keepalived fails it over between
managers mid-session. Worth recording that the polarity is the reverse of the other documented gotcha
in those notes, where the short name was correct and the FQDN wrong. Neither rule generalizes. Resolve
the name and check what answers.

**Would revisit if:** someone adds a real DNS record for the short name, which would make it safe but
still not necessary.

---

## 2026-09-07 — Keyword search over notes, not embeddings; OR the terms

**Decision:** Note retrieval is FTS5 keyword search. `memory.search_notes` sanitises any query down
to quoted literal terms and ORs them, ranking by bm25 and then by recency. No vector index, no
embedding model.

**Why:** The corpus is small (hundreds of notes a season) and the queries are proper nouns — player
names, team abbreviations, "hamstring" — which is exactly where keyword search beats a similarity
score. An embedding store would add a model dependency and a second index to keep in sync with the
`notes` triggers for no recall we can currently demonstrate. Terms are ORed rather than ANDed
because this retrieval feeds a prompt: a four-word query that ANDs down to zero rows gives Claude no
context at all, while OR plus rank plus `limit` puts the best note first and drops the tail.

**The sanitiser is not optional.** FTS5's query language is not SQL, so parameter binding does not
protect it — a bound string is still parsed as a query. `Ja'Marr Chase`, `RB*`, a lone `-` and a
stray `"` each raise `sqlite3.OperationalError` and take down the page that asked. Every football
name that matters has an apostrophe in it eventually.

**Would revisit if:** recall proves weak in practice — a note that exists and is not retrieved
because the job phrased it differently from the query. The upgrade is a vector index alongside FTS,
not instead of it.

---

## 2026-09-07 — Every `claude` call is environment-isolated, and the flags are not configurable

**Decision:** `claude_runner` passes `--strict-mcp-config`, `--mcp-config '{"mcpServers":{}}'` and
`--setting-sources ""` on every single invocation. They are a module constant, not a `config.toml`
key, and a test asserts they appear in the argv for every job.

**Why:** Measured on this box with the CLI at 2.1.260. A bare `claude -p` inherits the operator's
whole environment — every MCP server, plugin and skill installed for the user running the daemon.
A two-token prompt pulled in 82,289 cached tokens and cost **$0.82**. With the two MCP flags it was
$0.048; adding `--setting-sources ""` brought it to **$0.005**. That is 165x, on a call this
application makes dozens of times a day, funded by a personal subscription.

**Why not configurable:** a per-job flag defaulting to "inherit everything" is one forgotten key away
from a $0.82 pick-clock call, and the failure is silent — the advice still arrives, it just costs a
hundred times more. A job that genuinely needs an MCP server gets an explicit new argument here and
a test to go with it.

**Verified:** `tests/integration/test_claude_live.py` runs the real binary under these flags and
fails if the call costs more than $0.10. A measured live run: $0.0017, 2.1s, `mcp_servers: []`.

**Would revisit if:** a future job needs a real MCP server, which would add an opt-in argument rather
than remove the default.

---

## 2026-09-07 — ESPN fixtures are recorded, scrubbed, and committed

**Decision:** The ESPN tests run against JSON fixtures in `tests/fixtures/espn/`, mocked in at
`requests.get` (for the library) and `httpx.MockTransport` (for the raw draft call). No test may
open a real network connection; a conftest fixture blocks both HTTP stacks at their real transport.
`scripts/record_espn_fixtures.py` re-records the fixtures from the real league in one command,
scrubbing cookies and pseudonymising SWIDs, member names and team names before anything is written.

**Why:** The suite has to pass on a box with no internet and no credentials, and a test that quietly
talks to ESPN passes until the day it does not. Mocking at `requests.get` rather than at our own
client means the real library parses the fixtures, so a fixture ESPN's library cannot read fails in
CI rather than at 6am on draft day.

**Why the pseudonymising and not just redaction:** a real ESPN payload carries the league's members
by first and last name, and their team names. Recording those verbatim would put Caroline's
leaguemates' real names into a public git history, irreversibly. Blanking them instead would
collapse the team-to-owner links and the fixtures would stop meaning anything, so each distinct name
maps to a stable fake.

**Known gap:** the fixtures committed with this decision are **synthetic** — hand-built to the
shapes in the `espn_api` source, because there were no credentials when the client was written. They
pin the mapping honestly but cannot be trusted about ESPN's real vocabulary. Re-record them the
first time cookies exist.

---

## 2026-09-07 — A pick is not a pick until a player is attached to it

**Decision:** `EspnClient.draft_picks()` returns only slots that a real player has been drafted into.
`playerId` must be present and greater than zero; `-1`, `0`, `null` and a missing key all mean "not
yet picked". The rule is written down once, as `hal_mary.espn.client.pick_is_made`, and applied at
the client boundary. `EspnClient.draft_schedule()` returns every slot, made or not.

**Why:** ESPN **pre-populates the entire draft board before the draft starts.** The first real sync
of Caroline's real league returned 96 rows — 6 teams by 16 rounds — every one carrying `playerId: -1` and
no name, with `draftDetail.drafted` and `inProgress` both false. Reading those as picks broke the
draft path in the ordinary case, not an edge case: `sync_draft` returned 96 "new" picks on its first
call, so the loop would believe a whole draft happened in one tick; `picks_until_mine` would reason
from a finished board; `apply_picks` would try to match a player with id `-1` and no name against
every board row — and the board deliberately allows negative ids for researched players, so that is a
real collision, not a theoretical one. The advisor would then be asked to recommend a pick for a
draft it thought was over.

**Why the client layer:** it is the one place that knows ESPN's vocabulary. Filtering in `sync_draft`
would leave the same trap set for the draft loop, the board and every future consumer, each of which
would have to remember a rule that is invisible in the data. The boundary filters once; nothing
downstream carries the knowledge.

**Why keep the placeholder rows:** they are the pick schedule. They say which team owns each overall
pick and how many rounds the draft runs, which is exactly what `picks_until_mine` and
`my_upcoming_picks` need. Reading that from ESPN beats deriving it from a pick order plus a snake
rule that we would have to keep in step with the league's settings by hand.

**Why the schedule is not persisted:** it is derived from `draftSettings.pickOrder`, and this
league's `draftSettings.orderType` is `DRAFT_START` — ESPN assigns the real order when the draft
begins. The pre-draft board is built from a provisional order (currently the identity `[1,2,3,4,5,6]`
in a league only four of six managers have joined), so a cached schedule would be a plausible-looking
lie about who picks when. It costs one HTTP call the loop is already making to re-read, and the
`mDraftDetail` response carries `settings.draftSettings` — pick order, type and clock — alongside the
picks, so one request answers both questions.

**Pinned by:** `test_a_prepopulated_board_is_no_picks_at_all` and
`test_sync_draft_ignores_espns_prepopulated_board`, both against
`tests/fixtures/espn/draft_detail_prepopulated_real_league.json` — 96 slots built field for field
from the real payload, and the only fixture in the tree that is not synthetic.

**Would revisit if:** ESPN ever starts using a positive placeholder id, which would make the rule
unenforceable from the pick row alone and would need cross-checking against `draftDetail.inProgress`.

---

## 2026-09-07 — The web app takes a connection *factory*, not a connection

**Decision:** `create_app(settings, connect=...)` is given a callable that opens a
`sqlite3.Connection`, and every request opens and closes its own. There is no long-lived connection
on the app object.

**Why:** A `sqlite3.Connection` may not be used from a thread other than the one that opened it —
the driver raises. The web app's code runs on at least three: uvicorn's event loop, Starlette's
`TestClient` portal thread, and the `asyncio.to_thread` worker that runs a sync off the loop. A
shared connection would work in development and fail in whichever of those a given deployment
happened to hit. Opening a local SQLite file costs microseconds, WAL means readers never block the
writer, and the factory is also the seam the tests inject a temporary database through.

**Consequence for later tasks:** anything that runs off the event loop — the draft poll loop, the
scheduler's jobs — opens its connection *inside* its own worker. Passing one in is the bug.

---

## 2026-09-07 — `/events` closes its generator explicitly

**Decision:** `/events` is served by `EventStreamResponse`, a `StreamingResponse` subclass whose
`stream_response` calls `body_iterator.aclose()` in a `finally`.

**Why:** Starlette ends a stream by cancelling the task iterating it, which leaves the async
generator suspended rather than closed — Python finalises it whenever the garbage collector gets
there. That generator holds the `EventBus` subscription. Caroline's phone locks its screen, the
socket drops, and the subscription outlives the connection; a draft evening of that is a bus fanning
out to dozens of dead queues. `aclose()` throws `GeneratorExit` in, which unwinds the
`async with bus.subscribe()` block and unsubscribes synchronously, so it completes even inside the
cancelled scope it runs in.

**Verified:** `tests/unit/test_web.py` drives `/events` at the ASGI layer and asserts
`bus.subscriber_count == 0` immediately after an `http.disconnect`. It has to be driven at that
layer: Starlette's `TestClient` and httpx's ASGI transport both buffer a whole response before
returning it, so an endless stream deadlocks them and neither can deliver a disconnect at all.

---

## 2026-09-07 — The session cookie is signed with a key derived from the password

**Decision:** The `itsdangerous` signing key is `scrypt(WEB_PASSWORD,
salt="hal-mary.web.session.v1", n=2**14, r=8, p=1)`, derived once per process and cached. There is
no separate secret key, and none is stored. PBKDF2 with 600k rounds is the fallback for an OpenSSL
build that refuses scrypt's memory bound.

**Why derive it from the password:** three properties, all wanted. There is no second secret to
manage or leak. Sessions survive a restart, so a reboot at 6am does not log Caroline out at 7. And
changing `WEB_PASSWORD` invalidates every outstanding cookie, which is the only thing "change the
password" can usefully mean for a single shared password.

**Why a KDF rather than a hash:** this shipped as a bare SHA-256, and that was wrong. The session
cookie crosses the LAN in cleartext — there is no TLS on a home network — so a captured cookie (a
guest device, a phone backup, a router that logs) is something an attacker can test password guesses
against *offline*. Against a single SHA-256 that is billions of guesses a second on a laptop GPU,
and a password two people chose to type on a phone does not survive billions of guesses. scrypt
makes each guess cost 16 MB of memory as well as time.

**Cost:** tens of milliseconds, once per process. An attacker who learns the password can mint
cookies — but they could simply log in, so nothing is lost. A stolen *database* still does not yield
the key, because the password is not in it.

---

## 2026-09-07 — Failed logins are counted per client, not globally

**Decision:** `LoginLimiter` locks a client out after `web.login_max_attempts` failures for
`web.login_lockout_seconds`, keyed on the client address, held in memory per process. A locked-out
client is refused **without** its password being compared, and every failure is logged: the address
and the running count, never the attempt itself.

**Why per client and not one global counter:** the KDF above only covers *offline* guessing. Online,
a device on the network could try the household password thousands of times a second and nothing
anywhere would have said so. But a global counter would hand that same device a way to lock Caroline
out of her own app thirty seconds before her pick — a denial of service dressed as a security
control. Per client is weaker (several addresses buy several budgets) but it cannot be turned
against her, which on a home LAN is the better trade.

**Why in memory:** a restart forgives everyone, which is right for a household. The alternative is a
table to maintain and a lockout that outlives the fix for it.

**Why the password is never logged:** a log full of near-miss guesses is its own disclosure, and it
is the file most likely to be pasted into a chat window while debugging.

---

---

## 2026-09-07 — The league's own settings have a manual fallback in `config.toml`

**Decision:** `hal_mary.league.load_league_context` is the single accessor for the league's size,
scoring, roster slots, draft order and Caroline's slot. A synced `league_settings` row wins field by
field; a `[league]` section in `config.toml` fills in whatever it does not supply. Neither source
available raises `LeagueUnknown` naming both fixes. `board_build` and the advisor read the league
only through it and never touch `EspnClient`.

**Why:** hal-mary has to be able to run a whole draft with **no ESPN access at all** — the draft is
close and the cookies may not hold. Only two things genuinely need ESPN on draft night: discovering
picks, which already had `record_manual_pick`, and the league settings, which had nothing. Without
this, a box with no working cookies cannot compute a single pick number or roster need, and a model
handed no league context assumes a twelve-team standard-scoring draft — this league is six-team full
PPR, so every recommendation would be confidently wrong.

**Why field-by-field rather than whole-source:** a sync that landed without the roster slots is a
real outcome, and "we know the team count but not the roster" is more useful than falling back
wholesale to a section someone may have filled in months ago.

**Would revisit if:** ESPN ever becomes a dependency hal-mary can assume, which it will not.

---

## 2026-09-07 — The draft loop reconciles the board against every recorded pick

**Decision:** Each poll applies `loop.pending_picks(conn)` — every recorded pick the board does not
yet agree with — rather than only the list `sync_draft` calls new. Picks already filed as unmatched
are skipped, so the pass converges and publishes nothing on an idle poll.

**Why:** `sync_draft` returns a pick as new exactly once. Anything that goes wrong on that one pass —
a duplicate that came back unmatched, a hand-entered pick that ESPN later attributes to a team, a
board rebuilt after a pick was applied — stays wrong for the rest of the draft, and the failure is
silent: the board thinks a player is available who is not, and recommends him. Reconciling against
the whole pick list means divergence heals itself on the next five-second poll.

**Cost:** one board load and one 96-row read per poll, which is nothing.

**Would revisit if:** the pick list ever became large enough for the read to matter, which at 96 rows
it is not.

---

## 2026-09-07 — Downstream keeps its own guard against a pick that names nobody

**Decision:** On top of `pick_is_made` at the client boundary, `draft/store.py` excludes rows with no
`player_name` and no positive `player_id` from `next_overall_pick` and `recent_picks`, and the draft
loop skips them when reconciling. Research-built board rows carry synthetic ids starting at
**-1001**, never near `-1`.

**Why:** the entry above filters ESPN's pre-populated board at the one place that knows ESPN's
vocabulary, and that is the right place. This is a second lock on the same door, and it earns its few
lines because **the failure it prevents is total and silent rather than partial and loud.** One
placeholder row reaching the `draft_picks` table by any route at all — a hand-entered pick that went
wrong, a fixture, a future code path, a restore of an older database — puts the next overall pick at
**97 in a 96-pick draft**. Every end-of-draft check in the system then reads "the draft is over"
before the draft has started: `my_upcoming_picks` returns empty, the loop reports `draft_over`, the
advisor is never called. hal-mary sits there advising nothing, all night, with no error anywhere and
nothing on the page to say why. There is no partial version of this failure and nothing to notice it
by. The synthetic-id floor of -1001 is the same argument from the other side: the board deliberately
allows negative ids, so `-1` colliding with a researched player is a real collision, not a
theoretical one.

**Would revisit if:** never, really. It costs one SQL predicate.

---

## 2026-09-07 — One draft-loop tick has one time budget, and it starts before the ESPN read

**Decision:** `draft.advice_budget_s` (60s) bounds a whole tick — the ESPN sync *and* every Claude
attempt. The deadline is an instant fixed at the top of `run_once` and passed into `advise`, which
starts an attempt only when it can finish inside what is left. The cost of an attempt is its
`timeout_s` **plus** `claude_runner.TIMEOUT_TEARDOWN_S`, exported from the runner rather than copied.

**Why:** the pick clock is 90 seconds. Sizing the attempts by adding up `timeout_s` values gave 55
and felt safe, and it was wrong twice over. A timed-out call also pays the SIGKILL reap (5s) and the
stdout join (2s) *after* its deadline, so two timeouts are 69s, not 55. And `sync_draft` runs earlier
in the same tick, bounded at 25s by `espn.connect_timeout_s + read_timeout_s`. A slow ESPN followed
by two Claude timeouts is ~94 seconds against a 90-second clock: the pick is gone before the
deterministic card renders, which defeats the entire point of having a deterministic card.

**Why not lean on the two picks of runway** that `advise_within_picks = 2` nominally buys: that
assumes the other five managers use their clocks. Once the top of the board is gone people pick in
ten seconds, so two picks is twenty seconds of runway, not one hundred and eighty. Lead time is not
a budget.

**Why a runtime gate rather than better arithmetic:** arithmetic in a comment drifts the moment
anyone tunes a timeout, and the symptom is a recommendation arriving after the pick was made — which
nobody notices until draft night. The gate makes the bound true by construction: whatever the sync
spent, the advisor spends only the remainder, and when nothing fits the board's own card renders
immediately. The worked sum in `config.toml` is the explanation, not the guarantee.

**What is not bounded, stated plainly because the next person will trust the comment.** The gate
bounds the *Claude* spend. It cannot cancel an HTTP read already in flight, and a tick makes up to
three of them — `warm()`'s `player_name_map`, `draft_picks()` (which fetches the name map itself when
the warm failed), and `_read_schedule()` on the tick the draft opens — each nominally 25s and more in
the pathological case, because httpx times out per operation and not per request. The true bound on a
tick is therefore `max(advice_budget_s, whatever the ESPN reads took)`, roughly 50-75s worst case,
not a flat 60. That is still the right shape: ESPN spending 50 seconds leaves 10, nothing fits, and
Caroline gets the board's own card at once rather than nothing at all. The lever for the ESPN half is
`espn.read_timeout_s`, not these two job timeouts.

**What it costs:** a slow sync costs an attempt, not the card. That is the right trade — skipping the
sync instead would risk recommending a player taken five seconds ago, which is the failure the whole
unmatched-pick machinery exists to prevent, while losing an attempt only downgrades a researched
recommendation to a ranked-list one.

**Would revisit if:** the measured tools-off latency moves far from 15s, or the league changes its
pick clock. Both are one config edit, and the test that pins the sum fails first.


---

## 2026-09-07 — Every configured path is anchored to `config.toml`, not the working directory

**Decision:** `hal_mary.config` resolves `paths.prompts_dir`, `paths.memory_dir`,
`claude.scratch_dir`, `claude.system_prompt_file` and `DB_PATH` **once, at load**, against the
directory holding the resolved `config.toml`. Absolute values pass through untouched. `Settings`
exposes absolute `Path` objects, and no consumer resolves a configured path again.

**Why:** Every consumer used to do its own `Path(value)`, which resolves against the *process*
working directory. That is the checkout for a developer and something else entirely under the
systemd unit. The failure was not a crash: `standing_memory()` found no directory, returned `""`,
and every prompt went out without the standing context that says who Caroline is and what the
league's rules are. The service looked healthy and the advice quietly got worse. Two implementers
found it independently, from different modules, which is the sign that the interface — a string
each caller resolves for itself — was the problem rather than any one caller.

The config file's own location is the only anchor that is right in a developer's checkout, under
systemd, and under any future packaging. `HAL_MARY_CONFIG` moves the anchor with it, which is what
makes the override actually usable for a deployment.

**Why in a model validator rather than in `load_settings`:** so that no route to a `Settings` — a
direct construction in a test, a `model_validate`, a future loader — can produce one carrying a
working-directory-relative path. Callers cannot get this wrong because they are never handed
anything they could get wrong.

**Verified:** `tests/unit/test_paths.py` builds a complete little deployment under `tmp_path`,
chdirs somewhere with none of it, and checks every consumer still reads the right file. All six of
its tests fail against the code this replaced.

---

## 2026-09-07 — A missing memory directory warns and shows on the status page; it does not raise

**Decision:** `standing_memory()` on a missing directory logs a warning naming the resolved path and
still returns `""`. The condition an operator actually sees is on the status page: a "Where the
files are" card listing every resolved path with whether it exists, and a red-box problem naming the
directory and the config file to check. An *existing but empty* directory gets a different, softer
problem line.

**Why not raise, given that silence is what made the original bug invisible:** because of who is on
each end. `standing_memory()` is on the 90-second pick-clock path, called for every advisor attempt,
every scheduled job and every chat message. Raising would convert "the advice is thinner than it
should be" into "there is no advice at all", at the exact moment the application exists for — and it
would do so on draft night, in front of Caroline, who cannot fix a path. Thinner advice is
recoverable; no advice is not.

The person who could act on a crash is Bryan, at deploy time, and he is not the person a stack trace
mid-draft would reach. So the loudness is put where he will actually be looking: the status page is
the page you open when the advice seems off, and it now answers "which directory am I reading?"
directly. That is what the brief's requirement reduces to — an operator being able to tell "there
are no notes" from "I am looking in the wrong place" — and two distinct problem sentences say it
more precisely than one exception could.

**Would revisit if:** a startup-time check is added (a `hal-mary doctor`, or a `serve` preflight).
That is the right place for a hard failure, because it runs before anyone is depending on the
answer, and it would not change the runtime behaviour above.

---

## 2026-09-07 — The `claude` child gets an environment allowlist, never a denylist

**Decision:** `claude_runner` passes `env=child_environment()` to `subprocess.Popen`. The child gets
`ENV_PASSTHROUGH` — `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `TMPDIR`, `TZ`, `LANG`,
`LANGUAGE`, `LC_*`, the `XDG_*` dirs, `CLAUDE_CONFIG_DIR`, the CA-bundle and proxy variables — and
nothing else.

**Why:** it inherited the parent's whole environment, including `ESPN_S2`, `SWID` and
`WEB_PASSWORD`. Nothing exploits that today: most jobs run with tools off and the model is not asked
to read its own environment. But `board_build`, `news_sweep`, `waiver_scan` and `chat` run with
`WebSearch` and `WebFetch` on, and those are exactly the calls a prompt-injected "print your
environment" could reach. The cookies are a live session on Caroline's ESPN account and
`WEB_PASSWORD` is the household password to this app; the child has no use for any of them.

**Why an allowlist:** a denylist has to be extended every time a secret is added to `.env`, and the
day someone forgets is the day it leaks. An allowlist is wrong only when the binary needs something
that is not on it, and that failure is immediate and loud.

**What is deliberately left out, stated rather than widened silently:** `ANTHROPIC_API_KEY` and the
rest of `ANTHROPIC_*`. The binary authenticates from the subscription credentials under `HOME`,
which is why `HOME` is on the list, and a live run confirms that is enough. A box that meant to bill
an API key or point at a gateway would have to add those explicitly — which is the right way round,
given the whole point of this application is that it runs on a subscription.

**Why the fake binary stopped reading its knobs from the environment:** `tests/fake_claude/claude`
was driven by `HAL_MARY_FAKE_*` variables, which would have required widening the production
allowlist for the test suite's own sake — leaving the allowlist tests asserting a list production
does not use. It reads `fake_knobs.json` from its working directory (the scratch dir) instead.

**Verified:** the fake writes the environment it was actually given to a file, and the test asserts
the secrets are not in it — evidence from the child, not from the code that built the dict. A live
run from `/tmp` with `ESPN_S2`, `SWID` and `WEB_PASSWORD` set in the parent: child env keys
`['HOME', 'LANG', 'LOGNAME', 'PATH', 'SHELL', 'TERM', 'USER', 'XDG_RUNTIME_DIR']`, no secrets,
exit 0, $0.0017.

**Would revisit if:** a CLI upgrade needs a variable that is not on the list. The symptom is the live
integration test failing to authenticate, which is loud.

---

## 2026-09-07 — CSRF is a property of the router, not of a handler

**Decision.** Every POST on the authenticated router requires a double-submit token: a
`hal_mary_csrf` cookie issued by middleware on the first response, echoed back in a hidden
`csrf_token` field (or an `X-CSRF-Token` header) and compared with `secrets.compare_digest`. A
mismatch is a 403 with a sentence, never a redirect.

**Why.** 7a shipped without one on the grounds that its only POST was a sync, whose worst case was
an extra read of ESPN. Manual pick entry is the first genuinely state-changing POST: any page open
in any tab on the house network could otherwise write a pick into Caroline's draft, mark the wrong
player gone, and make the next recommendation actively wrong — which is precisely the failure the
unmatched-pick warning exists to catch.

Putting the check on the router rather than in each handler is the same argument as `require_session`:
a route added to the private router is protected by construction, and nobody has to remember. That
covers `/logout` too, which used to be a hidden form post away from logging her out mid-draft.

**Consequences.** Every rendered form carries the token, so `page()` and the fragment renderer both
inject it and a test walks the draft page asserting no form is missing it. Tests that post to a
private route go through a `post()` helper that supplies the token; a test that posts without one is
testing the refusal, not the handler. The cookie is `HttpOnly`: the token is rendered server-side, so
nothing needs to read it in the browser, and a cookie script cannot read is one XSS cannot steal.

---

## 2026-09-07 — The draft loop runs on its own thread, started where the app is built

**Decision.** `hal_mary.draft.runner.DraftLoopThread` runs `DraftLoop.run_forever` on a daemon
thread with **its own** SQLite connection, and `web.serve.app_from_env` starts it with the app's own
`EventBus`. `start()` returns a bool and records `error`; it never raises.

**Why.** `run_once` deliberately blocks — on the web app's loop it would freeze every request for the
length of a Claude call, including `/events`, which is how the page learns anything happened. And
`db.connect` leaves `check_same_thread` on, so a connection opened on the web thread and used by the
loop is an intermittent `ProgrammingError` rather than a clean failure. The connection is therefore
opened *inside* the thread, which is why `start` waits on an event to report how it went rather than
simply returning.

It starts in `app_from_env` rather than in `serve` because that is where the app — and so the bus the
loop publishes onto — is built, and it is what `--reload` re-runs, so development and production take
one path.

**Consequences.** A loop that cannot start (no ESPN cookies, no `claude` binary, an unwritable
database) is logged and nothing more: the page still renders, and picks can still be entered by hand.
That is the right trade, because the page is where the operator would find out.

---

## 2026-09-07 — A fallback advice card is a different object, not a footnote

**Decision.** Advice with `source == "fallback"` renders with a dashed amber border and a band
saying it is the ranking list's own answer. A researched card gets a solid border and a
"Researched" chip. The difference is visible without reading.

**Why.** The advisor always produces a card — that is its promise — but a card computed from tiers
alone knows nothing about who is already on Caroline's team or who went in the last four picks.
Advice she cannot tell apart from a researched recommendation is advice she cannot weigh, and she
will act on it at the same speed either way. The distinction has to survive a glance, a grayscale
screen and a colour-blind reader, which is why the border style changes as well as the colour.

**Consequences.** `attempts == 0` (no model call was even started) and `attempts > 0` with a fallback
result differ only in one sentence — "had no time to think about this one" versus "ran out of time
thinking about this one" — because to her they mean the same thing about how much to trust the card.

---

## 2026-09-07 — A card is labelled with the pick it is *for*, not the pick it was written on

**Decision.** The draft page reads `my_next_picks[0]` from the advice payload as the pick a card is
for, and calls a card stale by comparing that against **her next pick** rather than against the pick
on the clock. `next_overall_pick` on the payload is a fallback for rows written before this was
understood.

**Why.** The advisor is deliberately early: the loop fires as soon as she is within
`draft.advise_within_picks` (2), and `_last_advised_pick` then stops it firing again for the same
pick. So a card for her pick 6 is normally written while pick 4 is on the clock, and
`advisor._persist` stores 4. Reading that as "the pick this card is for" made a correct, current
recommendation announce that it was out of date, dim itself, and promise a replacement that no code
would ever write — on her turn, every turn. Because `advise_within_picks > 0`, that was the normal
path, not an edge case.

**Consequences.** Anything that changes *when* the advisor runs has to keep `my_next_picks[0]`
meaning "the pick this card reasoned about". A cheaper fix would have been to store the target
explicitly in the payload; it was not taken because the value is already there and a second field
saying the same thing is a second field that can disagree.

**How this was missed, which matters more than the bug.** The test and the manual browser check both
seeded an advice row by hand with `next_overall_pick=6` and five picks made — an alignment the loop
cannot produce. The fixture encoded what the author believed the loop stored, so no assertion over it
could contradict the belief that produced the bug, and the two checks agreed with each other about a
state that does not exist. Advice fixtures on the draft page are now built by **running the loop**
(`advise_through_the_loop` in `tests/unit/test_draft_page.py`), and the board is advanced through the
loop's own `sync_draft` / `apply_new_picks` pair rather than by writing pick rows directly.

---

## 2026-09-07 — Every inference on the draft page needs a falsifier

**Decision.** "Working out your pick now" is only claimed when a `draft` sync row exists and is
younger than `web.draft_stale_seconds`, and a draft loop that failed to start puts its reason on the
page.

**Why.** The band is derived, not observed: the advisor publishes when it is *done* and says nothing
when it starts, so the page infers "a card is being written" from her pick being inside the
advisor's window with no card for it. `start_draft_loop` deliberately tolerates a loop that will not
start, so with nothing polling that inference is unfalsifiable and the page promises a card forever
while she waits for it. `sync_draft` writes a `sync_runs` row every tick, which is the loop's own
evidence of life and costs nothing to read.

**Consequences.** A page with a board, a league and picks but no poller says "No advice yet" rather
than "Working out your pick", and a failed loop is a band across the top naming the reason and
pointing at manual entry — which still works. The same reasoning applies to anything else the page
infers about work in progress: state what would make it false, and check that.

---

## 2026-09-07 — One draft order, stored in its own table, read by all three consumers

**Decision.** On the first successful `_read_schedule` — the poll that first sees a real pick — the
draft loop persists round one of ESPN's own draft board into a new `draft_order` table, once.
`league._espn_order` prefers that stored order over `draftSettings.pickOrder`, so the draft page, the
advisor and the loop all derive Caroline's pick window from `LeagueContext.draft_order` and cannot
disagree about which picks are hers.

**Why.** `draftSettings.orderType` on this league is `DRAFT_START`: ESPN draws the order when the
draft opens, and the `pickOrder` the pre-draft sync stored is a placeholder — in this league the
identity permutation `[1,2,3,4,5,6]`, which is a 1-in-720 coincidence as a real draw. Nothing re-runs
`sync_league` during a draft (only the CLI and the `/sync` button), so that placeholder is frozen for
the whole night.

The failure it produced was silent, which is what made it worse than an ordinary wrong number. The
page and the advisor run the *same* snake arithmetic over the *same* field, so on divergence they do
not contradict each other — they agree and are wrong together, with no staleness flag and nothing on
screen to notice. Caroline reads "4 picks until yours" while she is on the clock, and the prompt
asserts the same false position, so the recommendation is reasoning from it too.

**Why a table rather than a column on `league_settings`.** `sync._write_league_settings` is an
`INSERT OR REPLACE` of the whole row, so a column there is erased by the next `hal-mary sync` — and
re-populated from ESPN's stale pre-draft `pickOrder`, which is the value the stored order exists to
override. Someone tapping `/sync` mid-draft would silently undo the fix. The separate table is also
the honest shape: this is not a setting ESPN reports, it is an observation the loop made at a
particular moment, and `recorded_at` says when.

**Why write-once.** The order is drawn once. A loop restarted mid-draft reads the board again, and
ESPN is unofficial enough that a second answer could differ; moving her pick window while she is
looking at it is worse than keeping a slightly older reading. `store.store_draft_order` refuses to
overwrite, and an empty or malformed first round writes nothing rather than clearing what is there.

**What write-once costs, and what pays for it.** Because there is no second chance, the first write
has to be validated against the league rather than merely against itself. ESPN's first poll after
pick 1 can catch the board mid-write: round one's last slots come back with `team_id: null` while
the later rounds already name every team, so `[1, 6, 5, 4]` reads as a perfectly well-formed
four-team order — distinct, more than two entries, and completely wrong for a six-team league. Stored
permanently, `_espn_order` would discard it for wrong length on every load and `store_draft_order`
would refuse the good board on the next poll and every restart after it. One flaky read would
re-arm the exact bug this entry exists to remove, behind a single log line. So `_store_order` refuses
a first round shorter than the number of distinct teams the board itself names — a count the later
rounds carry even while round one is still filling in — and a refusal leaves the write available for
a whole board.

And because two readings agreeing is the *only* property this design protects, a second reading that
disagrees is logged with both orders. Write-once means nothing changes on screen when that happens,
which is correct and is also exactly why it must not happen in silence: the log line is the only way
a person can find out that ESPN told us two different things about who picks when.

**The cheaper fix that was rejected.** Passing the loop's already-computed `upcoming` window into
`advise` is a two-line change and it is wrong: it makes the card's label schedule-derived while the
page stays arithmetic-derived, and a card labelled from a different source than the page reads as
stale on every turn — the bug the entry above removed. One source, read by all three, is the whole
point. For the same reason `DraftLoop._upcoming_from_schedule` is gone: the loop now reads the
league like everything else, and `_schedule` survives only as the "already read this process" guard.

**How this was missed.** The divergence path had never been exercised, because the fake ESPN client's
default `draft_schedule` was built by snaking the same identity order the placeholder holds — so the
two sources had literally never disagreed in any test. `draft_fixtures.SHUFFLED_DRAFT_ORDER` and
`DIVERGENT_SCHEDULE` exist so they do, and `test_draft_order_source.py` opens with a test whose only
job is to fail if they ever agree again.

**The league page reads it too.** `teams.draft_slot` is re-seeded from the stale pre-draft
`pickOrder` by every sync, so a "Draft order" heading over that column would have `/league` visibly
contradicting `/draft` about which pick is hers — with "Your team" beside the row she is most likely
to believe. `_league_context` renders the stored order when there is one and calls the numbers
provisional when there is not.

**Residual gap, and the two things done about it.** The order is only readable once the draft is
running, so between the draft opening and the first pick landing every component still shows the
placeholder. Nothing can close that window inside hal-mary without caching a pre-draft board, which
is the lie this avoids — but the window is sharper than "a few seconds of stale numbers". If ESPN
draws Caroline **first overall**, the placeholder puts her next pick at 6, five away and outside
`draft.advise_within_picks`, so she gets **no advice card at all** for her opening pick while the
page says four picks out. That is the worst moment available to have nothing on screen.

A `/sync` after the draft opens closes it, because `pickOrder` has been drawn for real by then. So
that sync is an unconditional first step in `docs/SETUP.md` rather than a fallback for when
something looks wrong, and `partials/turn.html` says the numbers are provisional, and what to tap.
A person reading the page should not have to have read the runbook.

**And that note is gated on `turn.order_drawn`, not on `turn.started`.** "The draft has started" is
not what makes the numbers trustworthy; hal-mary having read ESPN's board and stored the drawn order
is. The two come apart in precisely the case the no-ESPN contingency exists for — picks entered by
hand, nothing polling ESPN, the order never written — where the placeholder is in charge for the
whole night. A note gated on `started` would disappear at pick 1 there, leaving numbers nobody has
any reason to doubt and nothing on course to correct them: the silently-wrong class this entire
entry exists to remove, reintroduced by its own mitigation. Gated on the stored order, the note says
"provisional" exactly while the numbers are provisional. Past the first pick it also says something
plainer, because by then the order should have been readable and was not, and the sentence must not
promise a fix that is not coming.
