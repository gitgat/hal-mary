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

**Why:** A web-enabled Claude call takes 30 to 120 seconds. The ESPN pick clock is 60 to 90 seconds.
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
