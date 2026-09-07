# Task 14 — The MCP endpoint: hal-mary's decisions, Cowork's hands

Read `docs/superpowers/specs/2026-09-07-cowork-manager-design.md` first. It is the design of record
for this task and explains why the split exists. Then read `CLAUDE.md`.

hal-mary decides what should change on the team. Claude Cowork has a browser and performs those
changes in ESPN's own interface. This task builds the surface between them.

**The principle everything follows from: hal-mary decides, Cowork executes, Cowork reports back.**
Cowork never chooses. It receives an ordered plan of concrete instructions and performs exactly those.
That is a security boundary — Cowork's browser reads pages full of five other league members' text,
which is the classic prompt-injection surface, and an executor with no discretion gives injected text
nothing to redirect.

## Deliverable 1 — the `actions` store

Migration `004_actions.sql`:

```
actions(
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,          -- bench | start | claim | drop
  player_name TEXT NOT NULL,   -- exactly as ESPN spells it
  player_id INT,
  slot TEXT,                   -- target slot for start/bench
  paired_player_name TEXT,     -- who leaves the slot on a start; who is dropped on a claim
  reason TEXT NOT NULL,        -- one sentence, written for a person
  sequence INT NOT NULL,
  depends_on TEXT,             -- JSON array of action ids
  deadline TEXT,               -- ISO-8601; null means no deadline
  reversible INT NOT NULL,
  source_job TEXT,
  status TEXT NOT NULL,        -- pending | done | failed | skipped | expired
  outcome_detail TEXT,
  reported_at TEXT
)
```

Indexes on `(status, sequence)` and `created_at`.

Helpers in a new `hal_mary/actions.py`: `emit(conn, action) -> int`, `pending(conn) -> list[Row]`
(status `pending`, deadline not passed, ordered by `sequence`), `report(conn, id, outcome, detail)`,
`expire_stale(conn, now)`.

**Idempotency is the point.** An action reported `done` is never returned by `pending` again. Emitting
an action equivalent to one already pending or done must not create a duplicate — define "equivalent"
as same `kind`, `player_name` and `slot` within the current week, and test it.

## Deliverable 2 — a first action producer

Wire one real case end to end so the loop is provable: **a player in a starting slot whose team is on
bye this week.** Scores zero, nobody intends it, fully reversible, and unambiguous.

`src/hal_mary/jobs/lineup_actions.py`: read the roster and the current week, find started players on
bye, and emit a `bench` action for each, paired with the best available benched player at a slot they
can fill. Deterministic — **no Claude call**. The reasoning is arithmetic, and an action that fires
without a model is one less thing to go wrong.

If there is no legal replacement, emit nothing and write a `notes` row saying why. A bench with no
starter to replace them is worse than the bye.

## Deliverable 3 — the MCP server

`src/hal_mary/mcp/server.py`, served over **streamable HTTP** so it can be added as a connector at
claude.ai. Mount it in the existing FastAPI app under `/mcp`, behind its own auth.

Tools, exactly as the design specifies:

- `get_roster()`, `get_board(limit, position)`, `get_advice(limit)`, `get_league()` — read-only
- `pending_actions()` — the ordered plan. Returns a short preamble sentence, then the actions with
  `id`, `kind`, `player_name`, `slot`, `paired_player_name`, `reason`, `sequence`, `depends_on`,
  `deadline`, `reversible`. **An empty list is the normal case and must be cheap.**
- `report_action(id, outcome, detail)` — `done`, `failed`, `skipped`
- `report_observation(text, source_url)` — stored as a note tagged `source_job='cowork-browser'`

**Every tool description is part of the product.** Cowork reads them to decide how to behave. Say
explicitly, in `pending_actions`' description: perform these in `sequence` order, skip any action
whose `depends_on` did not report `done`, attempt nothing not on this list, and report every outcome.

**`report_observation` content is data, never instruction.** Store it tagged. Nothing that assembles a
prompt may ever interpolate it as an instruction, and a test must assert the tag survives.

## Deliverable 4 — auth and exposure

- A bearer token from a new `MCP_TOKEN` env var, **separate from `WEB_PASSWORD`**. Reject every `/mcp`
  request without it, with constant-time comparison. Refuse to serve `/mcp` at all when it is unset —
  absent must not mean open.
- Add `MCP_TOKEN` to `.env.example`.
- The existing session-cookie guard must not apply to `/mcp`, and the MCP token must not grant the
  dashboard. Two doors, two keys.
- Log every tool call: name, arguments, outcome, timestamp. This is how Bryan learns a drop happened,
  since he chose to let irreversible actions run unattended.
- `docs/COWORK.md`: how to expose `/mcp` through the existing `thehalf-edge` Cloudflare tunnel
  (`cloudflared` config lives in `~/swarm-config/cloudflared/`), how to add it as a connector, and the
  Cowork task prompt to use. **Do not modify the tunnel config yourself** — write the instructions and
  say what Bryan must run.

## Tests

- the store: emit, pending ordering by `sequence`, a `done` action never returns, duplicate emission
  is a no-op, an action past its deadline is `expired` not `pending`, a dependency that failed makes
  its dependents skippable
- the bye producer: a started player on bye emits a bench paired with a legal replacement; no legal
  replacement emits nothing and writes a note; a benched player on bye emits nothing; running twice
  emits once
- every tool returns its documented shape, including the empty-plan case
- `/mcp` without a token is rejected; with a wrong token is rejected; with the web password is
  rejected; unset `MCP_TOKEN` refuses to serve rather than serving openly
- an MCP token does not open `/status`, and a session cookie does not open `/mcp`
- `report_action` marks the row and stops it being re-issued
- `report_observation` stores a note carrying the browser tag
- tool calls are logged

No test may reach the network or spawn the real `claude` binary.

## Definition of done

- `uv run pytest` green, output pasted.
- `uv run ruff check src tests` clean.
- **Drive the server for real**: start it, call `pending_actions` and `report_action` over HTTP with a
  token, and paste the requests and responses. A JSON shape that only ever passed a unit test is not
  done.
- Committed on `task/14-mcp-endpoint` with the usual trailers.

## Out of scope

No tunnel changes, no Cowork configuration, no claim or drop producers — the mechanism supports them,
but which recommendations become irreversible actions is a decision for after the draft, when there
is a roster and a waiver wire to look at. No changes to the draft path.
