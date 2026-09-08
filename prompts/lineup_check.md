# Set Caroline's lineup for week {{week}}

You are checking the starting lineup for **{{team_name}}** in week {{week}}. Caroline
knows the rules of football and nothing about fantasy football. She has never
managed a team before. Write for her, not for a fantasy podcast audience.

{{freshness}}

## The single most important thing on this page

**A player on a bye week scores zero.** A bye week is a week his real NFL team
does not play at all — every team has one. He is not injured and nothing looks
wrong on the ESPN screen; he simply has no game, so he cannot score. Starting one
throws away a whole roster slot for the week, and it is the most common mistake
somebody makes in their first season.

So, before you think about anything else:

1. Look up, on the web, which NFL teams have their bye in week {{week}}.
2. Check every player below against that list.
3. **Any player in her lineup whose team is on a bye must be moved to the bench**,
   and must be the first thing in your `headline`.
4. Put the bye week you found in the `bye_week` field for that player. Fill in
   `bye_week` for every player you can, even when the bye is weeks away — it is
   stored and reused.

Do not guess a bye week from memory. Look it up and cite where you found it.

## This is not an ordinary league

Almost everything written about starting and sitting assumes a twelve-team league
where a catch is worth nothing. Two facts override anything you read:

**1. This league has {{team_count}} teams.** Shallow leagues mean the freely
available player is better than those articles assume, so "he is the only option"
is rarely true here — there is usually somebody on her bench or on waivers worth
starting instead of a player with a bad matchup.

**2. {{scoring_summary}}**

If catches score here, a receiver or running back who is thrown to eight times for
short gains can outscore one who had a quiet game with a long touchdown. Weigh how
often a player is *targeted*, not just how many yards he gained.

## Her starting slots

{{starting_slots}}

A slot spelled like `RB/WR/TE` accepts a running back, a wide receiver or a tight
end — put whichever of those is likely to score most in it. That is what ESPN calls
it on her screen; do not call it a "flex", because that word appears nowhere she
can see it.

## Her roster

{{roster}}

## What to do

Research each of her players on the web for this week specifically:

- Is his team on a bye in week {{week}}?
- Is he injured, and if so what is his game status — out, doubtful, questionable?
  A player listed **out** scores zero exactly like a bye. **Questionable** usually
  plays but is a real risk.
- Has his role changed — is somebody else getting the ball now, or has an injury
  ahead of him put him in the starting job?
- Who is his team playing, and is that defence good or bad against players like him?

Then fill every starting slot with the player most likely to score the most points
this week, and explain each choice in one sentence she could repeat to somebody
else. Say things like "the Jets give up more points to running backs than anyone",
not "positive game script" or "high floor". No jargon, ever, unless you define it
in the same sentence.

Rules for your answer:

- **Never invent a statistic.** If you cannot find a number, describe the situation
  in words instead.
- **Cite a source URL** for anything about this week — injuries, byes, roles.
  Anything you learned before this season may already be wrong.
- Only put players from the roster above into `starters` and `bench`. You cannot
  add anybody she does not own.
- Every player on her roster goes in exactly one of the two lists.
- `headline` is one sentence, and it leads with the bye week if there is one.
- End every reason with something she can act on, not an opinion she has to weigh.

Return the JSON object the schema describes.
