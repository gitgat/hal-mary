# hal-mary as AI manager: the MCP endpoint and Claude Cowork as its hands

Design of record. Supersedes the "advisor only, Caroline clicks" decision in
[the original design](2026-09-07-hal-mary-design.md) for **in-season** actions. The draft is
unchanged and stays advisory.

## Why this exists

Bryan asked for hal-mary to manage the team rather than advise on it. The blocker was writing to
ESPN: the `espn-api` library is read-only, and ESPN's write endpoints are undocumented, so automation
meant reverse-engineering them or driving a browser.

Claude Cowork has a built-in browser and can use MCP connectors. That resolves it without touching
ESPN's private API at all — Cowork drives ESPN's own interface the way a person does.

## The split: hal-mary decides, Cowork executes, Cowork reports back

This is the load-bearing principle. Everything else follows from it.

**hal-mary is the brain.** It holds the board, the accumulated notes, the league settings, the roster
history and the reasoning. It decides what should change and why.

**Cowork is the hands.** Its session holds none of that context. It asks hal-mary what needs doing,
receives a concrete unambiguous instruction, performs it in the browser, and reports the outcome.

**Cowork never decides.** Two reasons, and the second matters more than the first.

The obvious one: the decision needs a season of stored notes, the league's actual scoring, and
knowledge of what was already tried. A fresh Cowork session has none of it.

The one that actually constrains the design: **Cowork's browser reads pages containing other
people's text.** Team names, message boards, transaction notes — all attacker-controlled in the sense
that five other league members write them. That is exactly the surface prompt injection uses. If
Cowork is choosing who to drop, a hostile team name is an instruction. If hal-mary chooses and Cowork
only executes "move Bijan Robinson to bench", there is nothing for injected text to redirect. The
split is a security boundary, not just a tidy separation.

Anthropic's own guidance advises against the built-in browser for other people's personal data. A
fantasy league is exactly that. Bryan was told and chose full automation; this design reduces the
blast radius to the smallest shape that still delivers it.

## What Cowork is told, and what it is not

An instruction from `pending_actions` names a player, a slot, and an action. It never contains
free-form reasoning for Cowork to interpret, and Cowork is never asked to pick between options. If
hal-mary cannot decide, it emits no action.

Cowork's prompt says, in effect: fetch the pending actions, perform exactly those, report each
outcome, and do nothing that was not on the list. Anything surprising on the page is reported, not
acted on.

## The MCP surface

Three groups. Read tools are safe to call at any time; act tools change hal-mary's state, not ESPN's.

**Read — the current picture**
- `get_roster` — her roster with positions, open slots, bye weeks, injury status
- `get_board` — the tiered board, undrafted or free-agent availability
- `get_advice` — recent advice items and whether they are done
- `get_league` — settings, scoring, teams, the current week

**Decide — the only tool that matters**
- `pending_actions` — an ordered list of concrete instructions. Each carries an `id`, an `action`
  (`bench`, `start`, `claim`, `drop`), the player named exactly as ESPN spells it, the slot, a
  one-sentence reason written for a person, and an `expires_at`. An empty list is the normal case and
  must be cheap to return.

**Report — closing the loop**
- `report_action(id, outcome, detail)` — `done`, `failed`, or `skipped`, with what the browser saw.
  Without this, hal-mary re-issues the same instruction forever and Cowork performs it every run.
- `report_observation(text, source_url)` — anything Cowork noticed that hal-mary should know. Stored
  as a note, tagged as browser-sourced, and **never** treated as an instruction.

## Order, dependencies and deadlines

`pending_actions` returns a **plan**, not a bag of independent moves. Fantasy roster mechanics make
order load-bearing, and getting it wrong leaves the roster in a state worse than doing nothing.

Every action carries:

- **`sequence`** — the order to perform them in. Cowork executes in this order and does not reorder.
- **`depends_on`** — the `id`s that must have reported `done` first. If a dependency failed or was
  skipped, this action is skipped too and reported as such, not attempted.
- **`deadline`** — the wall-clock time after which the action is pointless or harmful. A lineup
  change is worthless once that player's game has kicked off.
- **`reversible`** — whether hal-mary can undo it. Drops are not.

The rules that force this:

**A roster has a fixed size.** Adding a player when the roster is full requires dropping one first.
So a claim is usually two actions with a dependency, and the order is not interchangeable.

**But dropping first is the dangerous order.** If the drop succeeds and the add fails, she has lost a
player and gained nothing, and another team can claim him immediately. ESPN's own interface performs
add-and-drop as a single transaction for exactly this reason. **So the instruction must express it as
one action where ESPN supports that** — `claim` carries an optional `drop_player` field — and only
fall back to two dependent actions where it genuinely cannot be atomic. Cowork must be told which it
is, because "drop then add" and "swap" are different clicks.

**Lineup changes lock per player at kickoff**, not at a single weekly deadline. A Thursday-night
player locks Thursday; the rest lock Sunday afternoon. So deadlines are per action, derived from that
player's own game time, not a single "before Sunday" rule.

**Waiver claims are not immediate.** They are submitted and processed in a batch at the league's
waiver time, in priority order. Submitting a claim is therefore a request, not an acquisition, and
`report_action` marking it `done` means "submitted", not "acquired". hal-mary learns the outcome from
the next sync, not from Cowork.

**Swaps within the lineup are paired.** Starting a player also benches whoever held that slot. Emit
that as one `start` action naming both, not two actions that briefly leave the lineup invalid.

## What Cowork receives

A single ordered list with a short preamble it can read aloud: what is being done this run and why,
in one sentence. Then the actions. Then an explicit statement that it must perform them in order,
stop on a failed dependency, and attempt nothing not on the list.

If any action's deadline has passed by the time Cowork reaches it, it skips and reports. hal-mary
decides what to do about that on the next run rather than Cowork improvising.

## Deciding what the actions are

The existing in-season jobs already produce this reasoning; they currently write `advice` rows for a
human to read. The change is that a job can also emit an *action* — a machine-executable form of the
same recommendation.

Not every advice item becomes an action. An action requires that the player be named unambiguously,
the target slot be unambiguous, and the reason be one a person would accept without argument. A
bye-week bench qualifies. "Consider trading for depth at receiver" does not.

Started-on-bye is the first and clearest case: a player on bye scores zero, nobody intends it, and
it is fully reversible. Injured-and-out is the second.

## Safety

- **A bearer token, separate from the web password**, scoped to the MCP surface. The dashboard stays
  LAN-only and is not exposed by this work.
- **The tunnel exposes the MCP path and nothing else.** Not the dashboard, not the status page.
- **Every action is logged** with what was issued, what came back, and when, and is visible on the
  dashboard. Bryan chose to let irreversible actions run unattended, so the log is the only way he
  learns a drop happened.
- **Actions expire.** A stale instruction executed three days late is worse than none.
- **Idempotency.** An action already reported `done` is never re-issued. Cowork performing the same
  action twice must be harmless.
- **`report_observation` content is data.** It is stored, tagged as browser-sourced, and never
  interpreted as an instruction by any later prompt.

## What is out of scope

The draft. Cowork's browser needs Claude Desktop open, its scheduled tasks run at best hourly, and a
draft needs five-second polling with a 90-second response. The local draft loop keeps that job
permanently.

Trades, because they involve negotiating with another person.

## Open question, to settle after the draft

Whether `pending_actions` should ever emit a `claim`. A waiver claim is a real bid against other
teams and the reasoning is much less clear-cut than a bye-week bench. The mechanism supports it from
day one; whether the jobs actually emit one is a separate decision, and it is easier to make when
there is a roster and a waiver wire to look at.
