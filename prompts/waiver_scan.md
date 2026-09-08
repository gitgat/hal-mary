# Who Caroline should claim off waivers this week

It is week {{week}}. Work out which of the available players below are worth
adding to her team, rank them best first, and for each one say who she should
drop to make room and how hard she should go after him.

{{recency}}

{{freshness}}

## What waivers are, so the advice matches what she sees

Every player nobody owns is available. For the first day or two after a player is
dropped — or after the week's games finish — he is on *waivers*: everybody's
requests are collected and settled at one moment, and the team with the best
waiver priority (or the highest bid, in a league that bids) gets him. After that
he is a free agent and it is first come, first served.

So timing matters and she has to be told it. **Say when these claims stop being
possible in the `deadline` field**, in words — "Wednesday morning, when ESPN
processes this week's claims" — not as a date she has to convert.

## This is not an ordinary league

This league has {{team_count}} teams. {{scoring_summary}}

Both of those change waiver advice more than anything else you will read online:

- **With {{team_count}} teams, the wire is deep.** Almost every article about
  waivers is written for a twelve-team league where the available players are
  genuinely bad. Here, useful players go unowned all season, so she can afford to
  be picky and should almost never drop somebody good to chase somebody
  speculative.
- **Streaming works here.** Taking whoever has an easy matchup this week at
  quarterback, kicker or defence, and swapping him next week, is a perfectly good
  strategy in a shallow league. Say so when it applies.
- **If catches score, target volume is the thing to chase.** A player who is
  thrown to eight times a game for short gains is worth more here than one who
  occasionally breaks a long run.

Her starting slots:

{{starting_slots}}

## Her roster

{{roster}}

## Available players

{{free_agents}}

That list is ordered by how many ESPN leagues have already rostered the player,
most first. Treat that as a popularity signal and nothing more — the interesting
claim is usually somebody the rest of the world has not caught up with yet.

## What to do

Research the available players on the web, for this week specifically. What you
are looking for, in order of how much it is worth:

1. **Somebody whose role just changed.** A backup promoted because the starter
   ahead of him is hurt or has been benched is the single most valuable thing on a
   waiver wire, because he goes from scoring nothing to starting.
2. **Somebody with an easy run of games coming up**, especially at a position she
   is streaming.
3. **Cover for a bye or an injury on her own roster.** Look at her roster above:
   if a starter has a bye or is listed as out, she needs a body for that week.

Then, for each claim, decide who she drops. Rules:

- **Only ever name a player from her roster above.** Naming anybody else is an
  instruction she cannot follow, and it will be discarded.
- Drop from her bench, not her lineup, unless the lineup player is genuinely
  finished for the season.
- Never suggest dropping somebody better than the player being added. In a
  {{team_count}}-team league that trade is almost always a loss.
- If she has a free roster spot, set `drop` to null and say so.

Give at most {{max_claims}} claims, best first. **If nothing is worth claiming,
return an empty list** — "do nothing this week" is a real and often correct
answer, and padding the list with speculative names costs her real players.

Rules for your answer:

- Never invent a statistic. If you cannot find the number, describe it in words.
- Cite a `source_url` for anything about this week.
- Write for somebody who knows the rules of football and nothing about fantasy
  football. No jargon unless you define it in the same sentence.
- `bid` is in words she can act on: "use most of your budget on him, he is the
  best player anyone will see this month", or "only if he is free".

Return the JSON object the schema describes.
