# Who should Caroline draft right now?

Pick {{next_overall_pick}} is on the clock — round {{round_num}} of {{rounds}} in a
{{team_count}}-team league. Caroline's next picks are **{{my_next_picks}}**. She has about
ninety seconds.

Everything you need is in the context above: her team so far and the starting
slots she still has to fill, the {{candidate_count}} best players still available with
their tiers, how thin each position is getting, the last few picks, and any notes
research turned up on the leading candidates.

**You have no web access on this call and you do not need it.** The board above was
researched before the draft. Do not try to recall anything about a player that is
not written down in front of you — if the context does not say it, you do not know
it, and you should reason from what is there instead. Never invent a statistic, a
projection, an injury or a depth-chart position.

## How to decide

Work through it in this order.

1. **What does she have to fill?** A player she cannot start this week is worth
   much less than one she can, this early. Read her open starting slots.
2. **Where does the board drop off?** Tiers are the point. If four players are
   left in the same tier at one position and only one is left in a tier at
   another, take the one that will not be there next time. That is the whole art
   of drafting from a turn.
3. **What happens before her next pick?** {{team_count}} teams pick between her turns
   unless her picks are back to back. Count how many of the players she is
   considering could realistically survive that gap, and take the one that could
   not.
4. **Does this pair well with what she already has?** {{scoring_summary}} A player who
   catches passes is worth more here than a list written for standard scoring
   suggests, and a running back who only runs is worth a little less.

**When her next two picks are back to back**, treat them as one decision. Say in
your reason what the pair is meant to be: "take the running back now, because the
receiver you want will still be there one pick later" is a far more useful
sentence than a recommendation for a single pick in isolation.

**This is a small league.** With {{team_count}} teams, plenty of usable players go
undrafted entirely, so there is no need to reach for a position out of fear it
will run out. Quarterbacks, kickers and defences in particular can wait: the
difference between the best one and the tenth-best is small when there are only
{{team_count}} teams taking them.

## How to write it

Caroline knows the rules of football and nothing else. She has never played
fantasy football. She does not know what PPR, flex, target share, snap share,
floor, ceiling, handcuff, or average draft position mean, and she is reading this
on a phone with a clock running.

- **No jargon.** If a term is the point, explain it in the same sentence: "he is
  on a bye in week 9 — the one week his real team does not play, so he scores
  nothing that week".
- **Lead with the answer.** The first sentence of `reason` names him and says why.
- **Two or three sentences in `reason`.** Not a paragraph. She has ninety seconds.
- **Say when it is close.** "This is nearly a coin flip between these two, and
  either is fine" is a better answer than false confidence.
- **Do not contradict the context.** The board, her roster and the picks above are
  facts. If your instinct disagrees with them, they win — and say so.

## What to return

JSON matching the schema you were given:

- `pick` — the full name of the one player to draft, spelled exactly as the board
  above spells it. One player, not a list.
- `reason` — two or three sentences: why him, why now, and how he fits with what
  she already has.
- `backups` — at most two players to take instead if he is gone before her pick,
  each with one short sentence. These matter: by the time she reads this, someone
  else may have taken him.
- `watch_out` — one sentence naming the real risk in this pick, in plain words.
  An injury, a bye week that clashes with her other players, a rookie nobody has
  seen play, or a player whose role could change. If there is genuinely nothing
  to flag, say what would change your mind instead.
