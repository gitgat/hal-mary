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

**Cost:** we map player ids to names ourselves, which the library would otherwise do.

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
