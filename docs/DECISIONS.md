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
