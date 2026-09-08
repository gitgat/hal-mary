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

## 2026-09-08 — Three poll cadences, chosen by the board, and a loop that can end

**Decision.** The draft loop runs in one of three phases and reads its interval back from the phase
on every pass: **idle** (`draft.idle_poll_seconds`, 300), **live** (`draft.poll_seconds`, 5,
unchanged) and **done**, which stops polling altogether. The phase comes from
`hal_mary.draft.loop.draft_phase`, over two numbers off the `mDraftDetail` read the loop already
makes: how many slots ESPN's board has, and how many of them have a real player in them.

**Why.** The loop was written for draft night and then left running forever, because nothing ever
told it the draft was over. Five seconds around the clock is 720 requests an hour, 17,280 a day and
**2,073,600 over a 120-day season**, against an unofficial, undocumented API, on one household's
cookies, for a six-team league whose draft is a single evening. The work that job actually requires
is about 2,160 requests, once — roughly **960x**. The risk is not politeness: it is ESPN
rate-limiting or blocking the account, and the moment anyone notices is the moment it matters. The
phases cost about **37,000 requests a season**, a 56x cut with nothing lost on the night, because
`poll_seconds` is untouched the whole time a draft is running.

**Why the board decides and the flags do not.** `draftDetail` carries `inProgress` and `drafted`,
and `EspnClient.draft_status()` reports both. Neither is an argument to `draft_phase`, deliberately,
because neither can be believed in the direction it would be used:

* **`drafted` cannot stop the loop.** The entry above records that ESPN may only set it once a draft
  is over — that is why `draft_picks` reads the raw endpoint and ignores it. A detector that stopped
  polling on the flag would go quiet mid-draft, which is the one direction in which being wrong
  costs Caroline picks. So a `drafted: true` over a partly filled board keeps the loop at live
  cadence and gets a warning naming both numbers; that discrepancy line is the whole job the flag is
  trusted with.
* **`inProgress` cannot start the fast clock.** It is about the draft lobby, not about picks. The
  live league answered `{"in_progress": false, "drafted": false, "slots": 96, "picks_made": 0}` on
  2026-09-08, with the draft still days away and all 96 slots pre-populated — and had it answered
  `true`, a rule that promoted on it would have pinned the loop to five seconds for months, which is
  the exact bug being removed. A flag that cannot be trusted when it is true and tells us nothing
  when it is false is not a signal.

What is left is arithmetic over facts: a slot with a real player in it is a pick that happened
(`pick_is_made`), and a board whose every slot is filled is a draft with nothing left to watch.
`EspnClient.draft_status()` is recorded off `_raw_draft_rows` rather than fetched, so consulting it
costs no request — a second GET on the pick-clock path would be the same bug in miniature.

**The button is an override, not the mechanism.** `POST /draft/started` puts the loop on live
cadence at once, wakes it, and runs a sync — which is what re-reads the order ESPN draws when the
draft opens, the step `docs/SETUP.md` makes unconditional. It lives in `partials/turn.html`, inside
the sentence explaining why it is needed, because an instruction pointing at a control on another
page is two things to get right at the one moment nobody has a spare minute. If nobody presses it
the loop still reaches live cadence from the first pick ESPN reports, within one idle interval: a
button that is the only path to correct behaviour is a button someone forgets on the one night it
matters. The override expires after `draft.live_override_seconds` (an hour) so a stray tap costs an
hour of fast polling rather than a season of it, and it is cleared the moment the board justifies
the cadence on its own.

**The page asks the loop rather than deriving the cadence.** `draft_page._watching` prefers
`DraftLoopThread.watching()` and only falls back to re-deriving from the board. The two agree on
every night but one — the night somebody presses the button, where the loop is live and the board
still shows no picks at all. It renders no cadence when nothing is polling (the page already bands
itself for that) or when the draft is over.

## 2026-09-08 — Shutdown actually stops the loop

**Decision.** `DraftLoop.stop()` is safe from any thread and wakes the loop out of its wait
immediately: a `threading.Event` for "should I keep going", and `loop.call_soon_threadsafe` to set
the `asyncio.Event` the wait is parked on. `DraftLoopThread.stop()` joins with a bounded timeout and
logs when the thread outlives it; the thread is a daemon, so the process exits either way.

**Why.** `run_forever` used to wait on `asyncio.wait_for(self._stop.wait(), timeout=poll_seconds)`
with `_stop` an `asyncio.Event` **set from the web app's thread**. That is the same trap
`hal_mary.events` is written around: off-loop, `Event.set` resolves the waiter's future through
`call_soon`, which does not write the loop's self-pipe, so a loop parked in `select()` sleeps out its
full timeout regardless. At five seconds that was a slow shutdown nobody looked at. At the new idle
cadence it would have been **five minutes**, and the deploy unit stops the service with `SIGTERM` on
every release — so each deploy could leave a thread polling ESPN behind, accumulating, every one of
them behaving perfectly on its own. That turns over-polling into hammering, invisibly.

**And the wait has to be bounded, or none of that runs.** uvicorn waits for every open connection
*before* it runs lifespan shutdown, and lifespan shutdown is where `start_draft_loop` registered
`thread.stop`. The draft page holds `/events` open for as long as a phone is looking at it and that
stream never ends on its own — so with no `timeout_graceful_shutdown`, `SIGTERM` to a server with one
page open **never completes**, and the loop polls on for the life of the process. Measured: still
alive 30 seconds after `SIGTERM`, log stuck on "Waiting for connections to close". That is not a
corner case, it is the state on draft night. `web.shutdown_timeout_s` (5) is passed through
`run_server`, and the measurement that matters is taken **with a client attached** — without one the
bug is invisible, which is how it survived the first round of this work.

Measured on the box, 2026-09-08. Before: 6 draft syncs in 25 seconds (the five-second loop), and
`SIGTERM` to the server process took **0.90s** — bounded by whatever was left of a five-second wait,
so up to 5s. After: 1 draft sync in 30 seconds, and `SIGTERM` — to the `uv` wrapper, which is what a
plain `kill` on the job hits — had both it and the server gone in **0.22s**, with the loop parked on
the 300-second idle wait. Without the threadsafe wake that same shutdown would have waited out the
idle interval; `test_a_real_loop_thread_stops_well_inside_an_idle_interval` is what fails if it is
ever removed.

## 2026-09-08 — A hand-entered pick is numbered from the picks, never from the rowid

**Decision.** `record_manual_pick` numbers a pick `store.next_overall_pick(conn)` — one past the
highest pick that names somebody — and writes it with an upsert. `DraftLoop._update_phase` takes
`store.picks_made(conn)`, a **count** of picks that name somebody, rather than the highest number.

**Why.** `draft_picks.overall_pick` is an INTEGER PRIMARY KEY, so the old NULL insert took its number
from SQLite's rowid: one past the highest *row*. Any database that ran a sync before `pick_is_made`
existed at the client boundary still holds ESPN's 96 pre-populated placeholder rows, and there the
first hand-entered pick of the draft was numbered **97**. `next_overall_pick` then answered 98,
`draft_phase(97, 96)` answered `done`, and the draft loop broke out of `run_forever` and never polled
again.

The failure was total and silent, and every part of the page conspired to hide it: `loop_error` is
`None` for a thread that exited cleanly, `_watching` renders nothing in the `done` phase, and
`DraftLoopThread._ask` will not forward "The draft has started" to a dead thread — so there was no
in-app recovery either. The trigger is the *first* pick entered by hand, which is exactly what
happens on the night ESPN goes down: the contingency path ended the loop.

**Two locks, deliberately.** The numbering is the cause and is fixed at the source. Counting picks
rather than reading the highest number removes the whole class: no stray row number, from any future
path, can make a draft that has barely started look finished. `store.picks_made` and
`store.next_overall_pick` now say in their docstrings which question each answers — "how far along is
the draft" and "which slot is next" — because they are only the same number while every pick is
numbered consecutively from one, and this is what it cost to learn that.

The upsert is not incidental: the slot the pick lands on may be one of the placeholder rows. It can
only ever be a placeholder or an empty slot, because `next_overall_pick` is one past the last pick
that names somebody, so a real pick is never overwritten.

---

---


---

## 2026-09-07 — Cowork's browser is the hands; the split is the safety

**Decision:** the "bot advises, Caroline clicks" entry above is superseded **for in-season lineup and
waiver actions only**. The draft is unchanged and stays advisory. hal-mary now emits machine-readable
*actions*, and Claude Cowork performs them in ESPN's own interface through an MCP endpoint at `/mcp`.

**Why the blocker moved:** the original entry rejected autopilot because it meant browser automation
against her logged-in account. Cowork already has a browser and can use MCP connectors, so the
automation is not ours to write or maintain, and ESPN's undocumented write endpoints are never
touched.

**Why Cowork never chooses, which is the actual load-bearing part.** Cowork's browser reads league
pages carrying five other members' team names, message-board posts and transaction notes — text those
people write, which is the classic prompt-injection surface. If Cowork were selecting who to drop, a
hostile team name would be an instruction. Because hal-mary names the player and the slot and Cowork
only performs it, there is nothing for injected text to redirect. Every design choice on this surface
falls out of that: no tool returns options, no tool returns reasoning to interpret, and
`report_observation` content is stored tagged as data that no prompt may treat as an instruction.

**Why order is in the schema.** A roster has a fixed size, so adding usually implies dropping and the
wrong order loses a player for nothing; lineups lock per player at kickoff rather than on one weekly
deadline; a waiver claim is a submitted request rather than an acquisition. Hence `sequence`,
`depends_on`, `deadline` and `reversible` on every row, and a `claim` that carries its drop as one
transaction rather than two dependent actions.

**Why the log is a feature.** Bryan chose to let irreversible actions run unattended, so `mcp_calls`
is the only way he learns a drop happened. It is not instrumentation and must not be trimmed to
"errors only".

**Why the duplicate window is the NFL week and not a rolling seven days.** It was rolling first, and
that was wrong: a bench for a bye on the Monday and a bench for an injury five days later are two
different decisions about two different situations, and a rolling window swallowed the second one
silently. `actions` has no week column, but it does not need one — `[actions].week_boundary_*` gives
one clock, and the window is "since this NFL week began". The same clock is every action's deadline,
which is not a coincidence: an instruction that is still queued when its week ends is exactly the one
that should no longer be performed.

**Why the endpoint refuses to serve with no token rather than 404ing or opening.** `/mcp` is the one
path exposed to the internet. A missing `MCP_TOKEN` answering 503 with a sentence is loud; a 404 reads
as a typo and an open endpoint reads as working.

**Would revisit if:** ESPN's interface changes enough that Cowork cannot reliably perform a bench, or
a Cowork session is ever observed doing something that was not on the `pending_actions` list — the
second would mean the boundary is not holding and the answer is to narrow the tool surface, not to add
guardrails to the prompt.


---

## 2026-09-07 — A tag is not a boundary: browser notes are quarantined, not labelled

**Decision:** `memory.build_context` runs two complementary note queries, not one. The trusted query
asks for `TRUSTED_SOURCE_JOBS` — an **allowlist** — and the untrusted one asks for everything else,
including a note with no `source_job` at all, rendering them under
`## Unverified reports from outside hal-mary's own research` with a preamble saying they are claims
somebody made and never an instruction, and with a much smaller budget of their own.

**Why, and it is a correction rather than a design.** The original requirement was that
`report_observation` content is "stored tagged, and never interpreted as an instruction". That was
implemented as a tag on the row plus a test asserting the tag survived — which is the requirement's
words and not the requirement. `_render_note` dropped `source_job` entirely, `search_notes` had no
filter for it, and the advisor retrieves by FTS over note text, so a browser observation naming a
player rendered as a bullet inside `## What we have learned recently`, beside hal-mary's own
researched facts, with nothing to tell a reading model which was which. `board_build` escaped only
because its `topics=` filter happened to exclude the tag.

**The general lesson, which is why this is an entry and not a commit message:** the enforcement point
for a trust boundary is wherever the data is *rendered*, not wherever it is written. A tag propagates
only as far as somebody remembers to read it, and three modules away nobody did. If you add a reader
of `notes` that puts them in front of a model, it has to make the same split.

**Why a separate section and a separate budget rather than a marker on the bullet.** A marker on one
bullet among forty is context a model averages away; a section it has to enter, with the framing at
the top, is read first. The separate budget is the other half: retrieval budget is a resource, and
the browser is the one writer whose volume an outsider can influence — forty observations naming a
player would otherwise push every real fact about him out of the prompt.

**Also:** `_render_note` collapsing text to a single line is a security property, not formatting. A
note containing `\n\n## What we have learned recently\n\n- ...` would otherwise close its own section
and open a forged one.

**Why an allowlist and not a blocklist.** It was a blocklist first. `Cowork-Browser`,
`COWARK-BROWSER`, `cowork_browser`, `" cowork-browser"` and `""` are all "not the constant", so every
one of them landed in the *trusted* section. Not reachable while `report_observation` hardcodes the
constant — but the next untrusted writer to mistype its tag would have failed open, silently, into
the advisor's prompt. Inverted, a typo is merely quarantined. The cost is that a new trusted job
whose notes are quarantined is also silent, so a test checks the allowlist against the jobs that
actually exist: `config.toml`'s `[jobs.*]` plus every `*JOB_NAME` constant in `src`, minus the
writers that are deliberately untrusted.

**The guard had the same shape of blind spot as the thing it guards, and the fix runs the other
way.** It found writers by their constant, so a writer passing `source_job="whatever"` inline was
invisible to it: quarantined, which is safe, and silent, which is the one thing the test exists to
prevent. The obvious repair — teach the harvest to read inline literals — is wrong, and wrong in the
project's characteristic direction. That harvest feeds an assertion that every name in it *must be
on the allowlist*, so an untrusted writer using a literal would produce the failure message
`add these to memory.TRUSTED_SOURCE_JOBS: ['cowork-browser']`, and somebody would. A guard that can
talk a reader into opening the boundary is worse than the gap it closes. So the omission is made
impossible instead of detectable: `test_every_writer_names_its_source_job_with_a_constant` parses
`src` with `ast` (two docstrings say `source_job='chat'`, and a regex cannot tell those from code)
and fails on any inline literal, and the harvest subtracts `BROWSER_SOURCE_JOB` so it can never
demand the browser's own tag be trusted — which it could before, via a constant named
`BROWSER_JOB_NAME`.

**Why every field, not the one named `text`.** The first fix collapsed `text` and left `source_url`
appended raw — and `source_url` is caller-supplied by `report_observation`, so a payload delivered
through it produced a forged **trusted** heading after the quarantine, as the last thing the advisor
reads. `player_name` and `topic` were the same shape. Every rendered field now goes through
`_one_line`. The test that missed it asserted on `block.split(UNTRUSTED_HEADING)[0]`, which reads as
"the whole rendering" and is only the half its author was thinking about; the replacements assert on
the entire block and count headings that start a line, because a `##` inside a bullet is inert.

**Why the collapser is its own module.** The third time this boundary was dropped it was not the note
renderer at all: `espn.sync._league_memory_body` interpolated `teams.name`, `teams.abbrev`,
`teams.owner` and the league name raw into `memory/league.md`, which `standing_memory()` reads whole
into `## What you always know` — section one, the most trusted text there is, with no quarantine and
no allowlist behind it. A leaguemate renames their team and it syncs straight in; team names are the
first example in this project's own threat statement, and a newline is not even required for the text
to land verbatim in the most trusted section.

Three renderers, in three modules, each rediscovering the same requirement and each getting it wrong
in a field nobody was thinking about. So `hal_mary.prompt_text.one_line` is a module of its own that
both import: the next renderer inherits the defence rather than having to remember it, and its
docstring carries the history so the reason survives the next refactor.

**Would revisit if:** a second *trusted* writer appears — add it to `TRUSTED_SOURCE_JOBS` rather than
inventing a second mechanism. An untrusted one needs no change at all, which is the point of the
direction.

---

## 2026-09-07 — `serve` boots degraded; `hal-mary doctor` is what refuses

**Decision.** There is no startup preflight in `serve`. A separate command, `hal-mary doctor`,
answers "could this box actually run hal-mary" and exits nonzero for a fatal problem;
`deploy/install.sh` and `deploy/deploy.sh` both run it and stop on that. The service itself starts
whatever it finds, and the running application reports the same facts on `/status`.

Task 11 deferred this decision to the deployment unit because the boot-or-degrade policy belongs
with whoever owns the service, not with the code that loads the config.

**Why.** The two failure modes are not symmetric, and the asymmetry runs the opposite way from
intuition. A service that refuses to start because the memory directory is missing is *down* — at
2am, with nobody watching, and with the one page that would have explained why now unreachable,
because that page is served by the process that refused to start. A service that starts and says on
`/status` that its memory directory is missing is still serving the draft page, still accepting
hand-entered picks, and is telling the truth in the place someone would actually look. Refusing to
boot converts a degradation into an outage, and it does so precisely when the degradation is
cheapest to tolerate.

The other half is that refusing has a *right* moment: install and deploy, when a human is at a
terminal watching the output and a refusal costs nothing but their next thirty seconds. Putting a
unit on the box that cannot make a single model call — because `claude` was never logged in — is a
real and silent failure, and that is exactly what `install.sh` now stops.

**Consequences.**

* `Check.fatal` in `src/hal_mary/doctor.py` is a policy dial, not a severity label: it means "should
  install.sh or deploy.sh stop over this". Missing `.env` keys, a missing or logged-out `claude`, an
  unwritable database directory and a database on NFS are fatal. A missing memory directory or a
  pending migration is a warning, printed and passed over. Nothing in doctor ever stops `serve`.
* Doctor touches no network and spawns no process. `espn-check` is the command that asks ESPN
  whether the cookies still work, deliberately kept separate: expired cookies must not block a deploy
  that is fixing something else, and a preflight that costs a Claude call is one people learn to
  skip. The `claude` login check therefore reads Claude Code's own `~/.claude.json` and says in its
  output that it is a heuristic.
* `/status` and doctor overlap on purpose. They are the same facts for two different people: whoever
  has a browser and a running service, and whoever has a shell and no service yet. Both read through
  `Settings` — `missing_secrets()`, `resolved_paths()` — so a check cannot drift from what the
  application actually resolves.
* `deploy.sh` has `HAL_MARY_SKIP_DOCTOR=1` for the one case the rule gets wrong: deploying the fix
  that makes a not-yet-ready box ready.

---

## 2026-09-07 — The database backup is a subcommand using SQLite's online API, not `cp` in a script

**Decision.** `hal-mary backup` (`src/hal_mary/backup.py`) snapshots the database through
`sqlite3.Connection.backup`, writes to a temporary name and renames into place, and prunes to
`backup.keep` files. `deploy/hal-mary-backup.timer` runs it nightly. There is no backup shell script.

**Why, on the copy.** The database is opened in WAL mode so the web app can read while a job writes.
In WAL mode a committed row can live entirely in `hal.db-wal` with nothing of it in `hal.db`, so
`cp hal.db` produces a snapshot that opens cleanly, passes `PRAGMA integrity_check`, and is missing
the most recent writes — the ones the season is actually made of. Copying all three files is no
better: they are copied at different instants, so the `-wal` can be newer than the `-shm` header it
is validated against. SQLite's online backup API reads through the same WAL the writers use and
produces one self-contained file.

**Why, on the subcommand.** `DB_PATH` is anchored to the directory holding the resolved
`config.toml`, and `HAL_MARY_CONFIG` can move that. A shell script would have to re-implement that
resolution, and the failure when it got it wrong would be a backup of a file that does not exist —
silently, at 4am, discovered during a restore. Reading the path through `Settings` is the only way
to be certain the backup is of the database the service opens.

**Consequences.** Backing up a database that does not exist raises rather than succeeding quietly:
"backed up nothing, successfully" is the report that hides a misconfigured `DB_PATH` for a season.
Retention matches only files named `<stem>-<timestamp><suffix>`, because a backup directory is a
directory on someone's disk and deleting a file we did not write is not a mistake anyone gets to
make twice. `keep` is a count of files rather than days, because a timer can miss a night and "the
last fourteen" is the window someone reasons about while restoring.

---

## 2026-09-08 — `DB_PATH` defaults outside the checkout, and doctor checks where it landed

**Decision.** `config.DEFAULT_DB_PATH` is `~/hal-mary-data/hal.db`, not `./hal.db`. `hal-mary
doctor` gained a `database location` check: a database under the directory holding `config.toml` is
a warning, and fatal once `~/hal-mary-data` exists.

**Why.** These two are the same bug seen from either end, and the combination was silent. Task 11
made every configured path anchor to `config.toml`'s directory — correct, and it quietly changed
what `./hal.db` *means*: no longer "wherever you started the process", but "inside the checkout".
`.env.example` ships `DB_PATH=` empty, empty falls through to the default, and the default was
relative. So the path of least resistance put `hal.db` — and the `backups/` directory that follows
the database — inside the one directory `git pull` rewrites, `git checkout <sha>` moves, and a
re-clone loses. `install.sh` meanwhile created and blessed `~/hal-mary-data`, which nothing then
used, and doctor reported nine checks and zero failures over the whole arrangement.

The runbook said to set it absolutely. A runbook instruction with nothing enforcing it is a
comment.

**Consequences.** The default is now correct with no `.env` at all, which is the state a box set up
in a hurry is in. Two tests in `test_config.py` pin it, and the tests that were exercising
*anchoring* through the default now pass an explicit relative `DB_PATH`, because those two things
had been conflated. The location check is fatal only when `~/hal-mary-data` exists, because that
directory is `install.sh`'s own artifact: if it is there and the database is not in it, someone
skipped a step in the runbook — whereas a developer's checkout has no such directory and no
deployment to break.

---

## 2026-09-08 — doctor resolves `claude` against the unit's PATH, not the caller's

**Decision.** `hal_mary.doctor.UNIT_PATH` mirrors `Environment=PATH=` in
`deploy/hal-mary.service`, and the binary check searches both that and the caller's `PATH`,
reporting disagreement as fatal. `tests/unit/test_deploy.py` asserts the constant and the unit file
are identical.

**Why.** `shutil.which` asks about the PATH of whoever is running doctor, and the only PATH that
matters is the one the service will have. The two differ in practice: `claude` installs into
`~/.npm-global/bin`, which Ubuntu's `.bashrc` adds — and `.bashrc` returns early for a
non-interactive shell, so that directory is absent under `ssh host 'command'`. Checking only
`os.environ` therefore fails a perfectly healthy box every time `install.sh` is run
non-interactively, and — the worse direction — passes a box where `claude` sits somewhere the
unit's fixed PATH will never look. That second case is precisely the silent failure the unit file's
own comment warns about: the service starts, serves every page, and fails every model call.

**Consequences.** The check reports *where* it found the binary and against which PATH, so a
disagreement names both. Two files now encode one fact, which is why the drift guard is a test
rather than a comment.

---

## 2026-09-08 — Chat keeps the CLI's own session, so it is the one caller that persists one

**Decision.** `ClaudeRunner.run` and `.stream` take `persist_session: bool = False`.
`--no-session-persistence` is now added only when a call neither resumes a session nor asks to keep
one. `hal_mary.chat` is the only caller that passes `persist_session=True`.

**Why.** The runner already supported `resume`, and `chat_sessions.claude_session_id` already existed
to hold what it would resume — but the flag that made every job one-shot meant the CLI *discarded*
the session whose id chat then stored. The second message of every conversation would have named a
session that was never written, and the whole point of a chat page is that "why did you say that?"
has something to refer back to.

The default stays as it was, because it is right for everything else: a scheduled job that left a
session on disk every run would accumulate them for no reader.

**A resume that fails is forgotten.** A stored id the CLI no longer holds would fail every message
after it, forever, and nothing on the page would say why. So a failed call clears
`claude_session_id`: the conversation loses its thread once, rather than the page losing every reply.

## 2026-09-08 — The question is recorded by a POST and answered by a GET

**Decision.** `POST /chat/send` persists the user's message and returns a fragment containing an empty
assistant bubble pointed at `GET /chat/stream/{session_id}`, which runs the Claude call and streams
it. Which question a stream answers is *derived* — `chat.pending_question` is the newest message in
the conversation when it is hers — not stored.

**Why.** An answer takes up to a minute with web search on, and a phone is not a reliable reader for
that long. Splitting the two means the question survives the wait: a page reloaded mid-answer finds
an unanswered question and reattaches to the same stream instead of losing what she typed. It also
keeps the POST an ordinary CSRF-checked form post — `EventSource` cannot issue one — and leaves the
no-JavaScript path working, because the page renders the same pending bubble a reload would need.

Derived rather than stored because a "needs answering" flag would need clearing on four different
failure paths, and the one that got missed would leave a conversation permanently convinced it owed
a reply.

## 2026-09-08 — A disconnected chat stream finishes its answer rather than throwing it away

**Decision.** When the browser vanishes, the SSE generator's close sets a stop flag the worker checks
between chunks; it cannot interrupt a read already blocked on the subprocess, so a call in flight
runs to the end and its reply is written into the conversation. `chat.answer` persists whatever
arrived if it is closed part-way, and persists nothing at all if nothing arrived.
`hal_mary.web.chat_page.Answering` — one instance per application — allows one live answer per
conversation, so the page that comes back while the first call is still finishing is told to wait
rather than starting a second one.

**Why.** The alternative — killing the call with the response — throws away a searching Claude call
that had already been paid for, at the exact moment (a phone locking its screen) when it is most
likely to happen. Persisting means she reloads and the answer is there.

The interlock is the other half. Without it, a flaky connection turns one question into as many
concurrent calls as there were reconnects, each writing its own assistant message, and the
conversation ends up with three answers to one question and three entries in `claude_calls`.

Nothing is written for a disconnect that arrived before any text did: an empty assistant bubble
claims to have answered, while an unanswered question is a thing the page can offer to ask again.

---

## 2026-09-08 — CI enforces a green `main`; two tests are honestly red

---

## 2026-09-08 — CI enforces a green `main`, and a test about the box skips rather than fails

**Decision.** `.github/workflows/ci.yml` runs `uv sync --frozen`, `uv run pytest` and
`uv run ruff check src tests scripts` on every pull request and every push to `main`, on
`ubuntu-latest`, with no secrets of any kind. `ruff format --check` runs beside them as advisory
only. Actions are pinned to exact tags, not floating majors.

**Why.** `main` stayed green across nineteen pull requests because one session ran the suite before
every merge. Nothing enforced it, so the hard rule in `CLAUDE.md` was a comment with a person behind
it. It is now a required signal that outlives the session.

**What running it in a clean environment found.** With `env -i`, a fresh `HOME` and no `.env` in the
checkout, ten tests failed that pass on this workstation. Eight were one real bug: `deploy/install.sh`
runs under `set -u` and read `$USER`, which an interactive login sets and a `sudo -u`, a cron job, a
container shell and a systemd unit do not. It died *after* writing the unit files and *before*
enabling linger and starting the service — a half-install that leaves units which never start and
never survive a reboot, invisible from a developer shell. `install.sh` now derives the name with
`id -un`.

**The other two, and the rule they encode.** They were not asserting anything about this code. They
assert things about the *machine*: that Claude Code is installed and logged in, and that
`systemd-analyze verify --user` has a runtime directory and a real program at every `ExecStart`.
Those hold on a developer box and on the VM and are false on a CI runner by design. They now guard
that precondition and skip, naming what is missing:

* `tests/unit/test_doctor.py::_require_claude_code` resolves `settings.claude.binary` the way
  doctor does — the unit's PATH, then the caller's — and checks `claude_config_path`. Same source of
  truth as the check itself, so guard and check cannot disagree.
* `tests/unit/test_deploy.py::_why_systemd_verify_cannot_run` names three preconditions, not one.
  `systemd-analyze` on PATH is only the first, and it is the one a runner *does* have — which is why
  checking it alone produced a red build that said nothing about this repository. The other two are
  `XDG_RUNTIME_DIR` (without it verify dies at "Failed to initialize manager" before reading a unit)
  and an executable at each unit's `ExecStart`, read out of the unit files rather than written down,
  so the guard cannot drift. On a runner `uv` lives in the actions tool cache, not `~/.local/bin`.

**The distinction, stated so it survives.** A test skipped for an absent **precondition** is honest:
it still runs, and still bites, everywhere the precondition holds. A test whose **assertion** was
loosened until it passed everywhere checks nothing anywhere. Widening the doctor test's tuple to
tolerate both Claude checks would have been the second kind, and would have cost the thing that test
exists for — catching a fatal check that is about our config rather than about somebody's laptop.
A suite that is red by construction is worse than no CI, because it teaches everyone to ignore the
build; a suite that lies is worse still. Neither, here.

**`ruff format` is advisory.** The tree was never formatted: 44 of 70 files would change. Formatting
the world is its own commit, taken deliberately — not a side effect of adding CI. Make the step
blocking on the commit that does it.

**Would revisit if:** the suite outgrows ten minutes (split it), or a second Python version starts
mattering (it does not; 3.12 in both places).

---

## 2026-09-08 — The deploy's own environment is scrubbed out of the suite it gates on

**Decision.** `deploy.sh` runs the suite as
`env -u HAL_MARY_REEXEC -u HAL_MARY_PREVIOUS uv run pytest`, and `Box.env` in
`tests/unit/test_deploy.py` drops **every** inherited `HAL_MARY_*` variable before putting the
fabricated box's own back. Both halves, because either alone leaves the trap.

**Why.** `deploy.sh` re-execs itself after the pull with `HAL_MARY_REEXEC=1` (so it does not loop)
and `HAL_MARY_PREVIOUS=<sha>` (so the rollback SHA survives the re-exec), and then runs the suite.
That suite spawns `deploy.sh` subprocesses, which inherited both markers, skipped the re-exec they
exist to test, and failed — three tests, red only when run from inside a deploy and green
everywhere else, including CI and a bare `uv run pytest` on the same box.

Every component behaved correctly. The suite was red, so `deploy.sh` refused to restart the service
and left the previous code running. The bug was that the red condition existed *only* inside
`deploy.sh`, and so was permanent: no deploy could ever complete. It was found by running the real
script against the real VM; the unit tests could not see it, because the thing that was wrong was
what they inherited.

Scrubbed by prefix on the test side rather than by name. The two markers are what bit, but an
operator with `HAL_MARY_SKIP_DOCTOR` exported, or a shell carrying the unit's `HAL_MARY_CONFIG`,
would steer the fabricated box just as invisibly, and so would whatever variable either script
grows next. On the `deploy.sh` side the two are named, because the scope is narrow and exact: what
this script sets on itself, it takes back off before handing over. `HAL_MARY_PREVIOUS` is still
needed *after* the suite, for the rollback line, so it is removed for that one command rather than
unset.

**Verified.** `test_the_deploy_tests_pass_with_the_markers_already_in_the_environment` runs the
three tests that failed on the VM in a real pytest subprocess with both markers set — the exact
condition, as a test. `test_the_deploy_markers_never_reach_the_suite_it_gates_on` reads the `uv`
stub's record of the environment it was handed. The rest of the suite was checked for the same
class of leak by running all of it under `HAL_MARY_CONFIG`, `HAL_MARY_ENV` and `DB_PATH` pointed at
a decoy deployment: 1155 passed, unchanged.
## 2026-09-08 — A job run is recorded in exactly one place

**Decision.** `hal_mary.jobs.registry.run_job` opens and closes the `job_runs` row for every job,
and nothing else does. `board_build.build_board` — which used to open its own — now returns its
outcome dict and lets `board_build.run`, the registered entry point, raise `JobFailed` on a failure
the registry then records.

**Why.** Every job now has one shape, `run(conn, settings, runner, client) -> str`, because that is
what lets the scheduler, `hal-mary job`, and the button on the status page treat them
interchangeably. If bookkeeping also lived inside a job, running that job through the registry would
write two rows: one opened by the job and closed, and one opened by `run_job` — and the status page
would report a job that started twice and finished once. `tests/unit/test_job_registry.py`'s
`test_run_job_records_exactly_one_row_for_the_board_build` is the lock on that door.

**`run_job` never re-raises, and neither does its own bookkeeping.** It catches `BaseException`, not
`Exception`: a `MemoryError` out of a research job on a Sunday morning is still not a reason for the
web process to stop serving the draft page. `KeyboardInterrupt` and `asyncio.CancelledError` are the
two that genuinely mean "stop" and are re-raised, because a job that could not be cancelled would be
a job that outlives a shutdown. A database that cannot even open the `job_runs` row is logged and the
job runs anyway.

The one thing that *does* raise out of `run_job` is `UnknownJob`, and it happens before a row is
opened — so a typo never leaves behind a run that looks like it started and never finished.

---

## 2026-09-08 — The bye-week alarm is arithmetic, and it survives a failed research call

**Decision.** `jobs/lineup_check.py` computes, in Python, which players in Caroline's *starting*
slots are on a bye this week, and writes that as its own `advice` row with the player's name in the
headline. It writes that row **even when the Claude call fails**, and raises `JobFailed` afterwards.
A flag is raised if *either* source says bye: the `board.bye_week` researched before the draft, or
the `bye_week` the model just returned from the live NFL schedule.

**Why.** A started player on a bye scores **zero** — not a low score, nothing. It is the single most
common mistake somebody makes in their first fantasy season, it is never intentional, and it is
entirely determined by a roster and a calendar. Making it depend on a web-enabled model call that
takes 30 to 120 seconds and sometimes times out would mean the one piece of advice hal-mary can
always give is the one it gives least reliably.

That is also why it is a separate `advice` row rather than a line inside the lineup card. She reads
these on a phone; a warning three quarters of the way down a card is a warning that gets scrolled
past. The alarm is written *after* the lineup card so that a newest-first feed puts it on top.

**The asymmetry is deliberate.** Flagging a player whose bye is actually next week costs her the ten
seconds it takes to look at ESPN. Failing to flag one costs every point that roster slot could have
scored, and she finds out on Monday. So a disagreement between the two sources raises the flag and
says which source claimed it, rather than resolving it quietly in favour of either.

**Would revisit if:** a reliable bye-week source exists in the database for every rostered player —
`players` has no `bye_week` column today, and the board only covers players who were researched
before the draft. `season.bye_weeks` matches by normalised **name** rather than id for exactly that
reason: a board row built before the first ESPN sync carries a synthetic negative id that will never
join to a real roster row.

---

## 2026-09-08 — Which jobs exist at all depends on the phase, and the phase is re-checked daily

**Decision.** Every job declares its phases (`pre_draft`, `draft_live`, `in_season`, `off_season`)
to `registry.register`. `scheduler.current_phase` derives the current one from the league's draft
date and the windows under `[scheduler]` in `config.toml`, and `scheduler.apply_phase` makes the
running `AsyncIOScheduler` hold exactly that phase's enabled, cron'd jobs. A reserved job,
`_phase_check`, re-evaluates it daily and re-applies.

**Why.** A nightly board build is exactly right the week before the draft and is a paid, web-enabled
Claude call producing a board for a draft that already happened every morning after it. A lineup
check the week before has no lineup. Encoding that as one flag per job in `config.toml` would mean
somebody has to remember to flip five of them on draft night — which is the night nobody is going to
be editing TOML. The daily re-check is what lets a process started on Monday become an in-season
process on Wednesday without a restart.

`max_instances=1` and `coalesce=True` on every registered job. A research call can take fifteen
minutes; a second copy starting on top of it means two `claude` subprocesses, two budgets, and two
writers into one SQLite file. `coalesce` collapses a backlog — a box that was asleep — into one run
rather than firing every missed hour in a row.

**With no draft date, a made pick is the evidence.** ESPN can leave `draftSettings.date` null, and
this league's was null when it was read. `current_phase` then answers `in_season` if any pick names
somebody and `pre_draft` otherwise — using the same "a pick with no name and no positive player id
is one of ESPN's pre-populated slots" rule as everything else. It never raises: a process that will
not start because it could not work out the date is a far worse failure than one that assumes the
draft has not happened.

---

## 2026-09-08 — The in-season sizes live in `[research]`, not `[season]`

**Decision.** The `[research]` section of `config.toml` carries `free_agent_size`,
`free_agent_shortlist`, `note_limit`, `note_shelf_life_days` and `waiver_claims`.

**Why.** The obvious name is `[season]`, and it cannot be used: `Settings.season` is already the
ESPN season *year*, read from the environment, and a second `season` attribute is a `SyntaxError` at
the call site that builds `Settings` — which is how this was found. `[research]` also reads more
accurately: these are how much live state the research jobs put in front of Claude, not facts about
the season.


---

## 2026-09-08 — Weekday names in every cron, and Pacific rather than UTC

**Decision.** Every `cron` in `config.toml` names its weekday (`sun`, `tue`, `wed,sat`), never
numbers it. `[scheduler].timezone` is `America/Los_Angeles` and `scheduler_timezone()` is the only
place it is read. `tests/unit/test_scheduler.py` asserts the computed `get_next_fire_time` lands on
the intended weekday, and separately refuses any digit in a cron's day-of-week field.

**Why.** APScheduler's `CronTrigger.from_crontab` numbers `day_of_week` from **Monday**. crontab(5)
numbers it from Sunday. Everything shipped in the first cut of this task used the crontab spelling,
so every weekday job fired **one day late**, verified against the branch's own APScheduler 3.11.3:

```
0 9 * * 0    lineup_check  intended Sunday   -> Mon 2026-10-12 09:00
0 8 * * 2    waiver_scan   intended Tuesday  -> Wed 2026-10-07 08:00
0 7 * * 3,6  news_sweep    intended Wed+Sat  -> Thu + Sun
0 10 * * 2   weekly_recap  intended Tuesday  -> Wed 2026-10-07 10:00
```

The bye-week alarm — the single highest-value thing hal-mary produces — would have been written
after every Sunday game had already kicked off, and waiver claims submitted after ESPN had processed
them. The job would have run, succeeded, and shown green.

**Nothing caught it**, because the tests asserted that the cron *string* rendered on the status page
and in `hal-mary jobs`. A cron string is not a fire time. The two tests added here assert the thing
that matters and refuse the spelling that caused it.

UTC was the second half of the same mistake: these cadences are timed against NFL kickoffs, and
`0 8 * * sun` in UTC is 01:00 Pacific — before the Sunday inactive lists the lineup prompt is told
to go and read, and an hour adrift again whenever the clocks change.

`misfire_grace_time_s` (3600) is set for the same family of reasons: APScheduler's default grace is
**one second**, so a fire missed while the loop was blocked is skipped with only a log line.

---

## 2026-09-08 — The lineup check runs three times a week, because ESPN locks per player

**Decision.** `JobConfig.cron` accepts a string or a list, exposed as `crons` / `cadence`.
`lineup_check` ships three: Sunday 08:00, Thursday 15:00 and Monday 15:00, Pacific. Each cadence is
a separate APScheduler registration (`lineup_check`, `lineup_check#2`, `lineup_check#3`) so
`max_instances=1` still means one copy of each.

**Why.** ESPN's `rosterLocktimeType` on this league is `INDIVIDUAL_GAME`: a player locks at **his
own kickoff**, not at one deadline for the week. A Thursday-night starter ruled out on Wednesday
evening is already lost by the time a Sunday-morning check runs, and the same is true of Monday
night. One weekly run silently covers about two thirds of the games.

They could not share a cron: the hours differ, and folding them into `0 8,15 * * sun,thu,mon` would
be six web-enabled Opus calls a week instead of three. Cowork's own `cowork/tasks.toml` independently
arrived at the same sunday/thursday/monday split, which is corroboration rather than coincidence.

---

## 2026-09-08 — "No byes" and "byes not checked" are different sentences

**Decision.** `lineup_check._bye_check` returns a `ByeCheck` carrying `alarms`, `unchecked`
(starters with no bye week on file) and `checked` (whether the week was known at all). All three
reach the `job_runs` summary and the lineup card. A run that could not check says
"DID NOT CHECK BYE WEEKS"; a clean run says "nobody in your lineup is on a bye".

**Why.** Both conditions used to render as an empty alarm list, which is silence, which reads as
"nobody is on a bye" — the one reassuring sentence that must never be produced by not looking. The
week goes unknown in an ordinary way (cookies expire on Friday, Sunday's ESPN call returns nothing,
and now the stored week covers that), and a starter goes unchecked in an even more ordinary one: a
player claimed off waivers in October was never on the researched board, so `board.bye_week` has no
row for him.

The job is **not** failed for either. Thinner advice beats none on a Sunday morning — the same rule
`standing_memory()` follows. It just has to say which it is giving.

**The proper fix is a `players.bye_week` column** filled from ESPN's `proTeamSchedules_wl` view;
the fixture already exists at `tests/fixtures/espn/pro_schedule.json`. Until then, `unchecked` is
the honest report of the gap rather than a hidden one.


---

## 2026-09-08 — Two schedules, one timezone, and an ordering that has to hold

**Decision.** `[cowork].timezone` is `America/Los_Angeles`, the same as `[scheduler].timezone`. They
stay two keys, and the drift is made loud in two places: a test asserts the shipped config gives
them the same value, and `cowork.render` adds a warning to the rendered output when they differ.
The three Cowork lineup runs in `cowork/tasks.toml` were re-timed to sit between hal-mary's own
`lineup_check` and kickoff: Sunday 10:30 -> **09:00**, Thursday 17:30 -> **16:00**, Monday 16:30 ->
**16:00**.

**Why the re-timing.** Those times were written against Eastern kickoff quotes — 10:30 is two and a
half hours before a 13:00 ET Sunday start, 17:30 is comfortably before 20:15 ET on Thursday. Read in
the operator's actual zone they are half an hour *after* the Sunday early window kicks off and
fifteen minutes after the Thursday night game does. `[cowork].timezone = "UTC"` hid that: the
renderer warned about the zone, but the numbers beside it looked perfectly reasonable, and a
placeholder that prints a plausible wrong time is worse than one that prints nothing.

**Why they stay two keys.** They are genuinely different ideas — the zone hal-mary's own cron is
read in, and the zone a person types into somebody else's web form — and Task 14 documented that
distinction deliberately. Collapsing them would be right for this deployment and wrong for an
operator who is not sitting next to the box. What was actually missing is not one key; it is that
nothing ever compared them.

**The invariant worth more than either.** The two schedules are a pipeline: hal-mary works out the
lineup changes and queues them as `actions`; Cowork opens ESPN and performs them. If a Cowork run
drifts in front of the check that fills its queue, it finds nothing, reports "nothing to do", and is
*correct* — so the failure is completely silent and the lineup simply never changes.
`test_every_cowork_lineup_run_happens_after_the_check_that_fills_its_queue` and
`test_both_schedules_finish_before_the_ball_is_kicked` pin both ends of that window, in local time,
against the real kickoff hours.

**Also.** The summary table's `When` column was a fixed 28 characters, sized for `UTC`. A real IANA
name is nineteen characters and pushed every following column out of true, in the one output whose
entire purpose is being read by a person. The widths are measured now, with a test.

**Not covered, and worth a follow-up:** the `waivers` task derives its time from the league's own
processing day less `waiver_lead_minutes`. For this league (Wednesday 10:00) that lands Tuesday
10:00, safely after the Tuesday 08:00 `waiver_scan`. A league that processed on a Tuesday would
derive a Monday run — in front of the scan that fills it — and nothing would catch that, because the
derivation depends on league settings rather than on anything in the repo.

---

## 2026-09-08 — The transcript is owner-only, and a full disk is reported as a disk

**Decision.** `claude.scratch_dir` and its `transcripts/` subdirectory are created `0700` and
tightened to `0700` if they already exist wider; every transcript is created `0600` by `os.open`
rather than `Path.open` plus a `chmod`. Separately, an `OSError` escaping the transcript block in
`ClaudeRunner._execute` is caught and reported as `ok=False` naming the transcript, unless the
result was already recorded — in which case it is logged and the call still succeeds.

**Why the mode.** A transcript is not a log line, it is the *entire* prompt for one call: her
roster, the built board, every retrieved note, and any system prompt assembled at runtime that
exists nowhere else on disk. At the default umask that file is `0644` in a `0755` directory. On a
single-user VM that is theoretical, and it stops being theoretical the first time anything else runs
on the box — which is exactly the kind of change nobody re-audits file modes for.

`os.open` with the mode rather than open-then-chmod, because the chmod leaves a window in which the
file exists at `0644`, and the window is the whole thing being fixed. Existing directories are
*tightened* rather than left alone because `mkdir(exist_ok=True)` ignores `mode` when the directory
is already there: without the tighten, only a box that had never run hal-mary before would get the
narrower mode, and the deployed one never would.

**Why the write guard, and why it was worse than a missing guard.** `mkdir` and `open` were already
inside a guard whose comment says why — `run()` is called from an APScheduler job and from an SSE
handler, neither of which has anywhere to put an exception. `handle.write` was not. So an `ENOSPC`
part way through a call escaped as a raw `OSError`, *and* the `finally` then recorded the row as
`"stream abandoned by caller"`. That second part is the reason this is an entry: the guard's absence
costs a crash, but the mislabel costs an afternoon, because it names a different subsystem
confidently and sends the reader to the SSE client while the box is out of space.

**Why `except OSError` on the block rather than a wrapper around each write.** The block contains
exactly four unguarded disk touches and they are all the transcript — the header, the stream loop,
the drain after a timeout, and the flush `with` performs on the way out. Everything else in there
either carries its own guard (`Popen`) or swallows its own `OSError` (`_kill_group`), so an
`OSError` arriving at that clause is the disk and cannot be anything else. A wrapper type would have
been the same guarantee with a class in between.

**Why a failed flush does not fail the call.** By the time `close()` runs the result has been built,
recorded and yielded. Failing then would throw away a good answer to protect a debugging file, so
that case logs and returns — and the `recorded` flag is what tells the two apart, which is also what
stops a second `claude_calls` row being written for one call.

**Would revisit if:** a caller ever needs to distinguish "the model failed" from "the disk failed"
programmatically rather than by reading the sentence — that wants a field on `ClaudeResult`, not a
different exception.

---

## 2026-09-08 — Re-reporting an action can correct anything except the fact that it happened

**Decision.** `actions.report` refuses a transition that leaves `done` and allows every other one.
`failed` → `done`, `skipped` → `done` and `done` → `done` all work; `done` → `failed` and
`done` → `skipped` raise `ValueError`, which `report_action` turns into a sentence for Cowork.

**Why re-reporting stays open at all.** A Cowork session that loses its place and runs the list again
has to be able to correct its own earlier report, and a tool that accepts a report only once is a
tool that strands a row nobody can close. That is the same reasoning as the `LookupError` on an
unknown id: an action that cannot be closed is re-issued every run for the rest of the season.

**Why `done` is the one direction that is closed.** `done` is a claim about ESPN, not about hal-mary
— the bench was clicked, the drop went through. A *stale* session reporting a failure after a
different session already succeeded would un-complete something that really happened, and the action
would then be handed out and performed a second time. For a `bench` that is noise; for a `drop`,
`reversible` is false and the second one takes a different player. The asymmetry is the asymmetry of
the world: a failure can turn out to have been a success, but a success does not turn out not to have
happened.

**Why the guard is in the `WHERE` clause.** Two sessions reporting at once is the exact scenario the
rule exists for, so a `SELECT` followed by an `UPDATE` would leave the race it is meant to close. The
row is read only when nothing moved, and only to say whether the id was wrong or the row was already
done.

**Why a `ValueError` and not a silent no-op.** Cowork is told the refusal in a sentence it can act on
— `report_observation` is where a contradiction belongs — because a report that looks accepted and
was not is how a session concludes it has finished a list it has not.

**Would revisit if:** an action ever genuinely needs undoing from hal-mary's side. That is a new
verb — an `undo` action emitted like any other, with its own row — not a backwards edit of the one
that already ran.
## 2026-09-08 — The waiver run is derived from the batch that follows the scan

`cowork.waiver_settings` read `waiverHours` as the hour ESPN processes claims. It is not that. It
is the length of the waiver period — how long a dropped player sits before clearing. The hour lives
in `waiverProcessHour`. Every test that covered this wrote `"waiverHours": 10` by hand, so the
fixture agreed with the bug and could not contradict it. Read against the real payload, which sets
`waiverHours` to 24, the derivation refused its own input ("24 is not an hour of a day") and the
waiver run never got a time. It failed closed, which is why nothing noticed.

`waiverProcessDays` is also a list, and this league names six days. Taking `days[0]` modelled a
weekly waiver run that does not exist here.

Both are now read properly, and the run is aimed at **the processing batch that follows hal-mary's
own `waiver_scan` job**, with the lead shortened when it would otherwise reach back past the scan.
That ordering is the same pipeline invariant the lineup jobs have: hal-mary queues the claims and
Cowork submits them, so a submit run in front of the scan finds an empty queue, reports "nothing to
do", and is *correct* — the failure says nothing and costs a week of claims.

The guard is a property test over every scan weekday and hour crossed with four processing-day
shapes, not an example. An example passed by construction, because the derivation adapts to
wherever the scan is: moving the scan could not make it fail. The property test also has to assert
the batch is the *next* one — "between the scan and the batch" is satisfied by aiming a week out,
and the first version of the test passed with the batch chosen by `max` instead of `min`.
