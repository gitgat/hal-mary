# What changed this week — Caroline's roster and the waiver wire

Find out what has actually changed, in the last few days, about the players
below. This is the research every other piece of advice hal-mary gives this week
will be built on, so it is worth doing slowly and properly: search the web, read
recent reporting, and check what you believe against it.

It is week {{week}}.

{{freshness}}

## Never answer this from memory

Everything that matters here happened in the last few days. An injury, a change
in who is getting the ball, a player promoted because the man ahead of him is
hurt — none of it is in any model's training data, and a confident answer from
memory here is how somebody starts a player who was ruled out on Friday.

**Every single note you return must have a `source_url` you actually read**, and
must be something you found, not something you recall. If you cannot find current
reporting on a player, say nothing about him. Silence is fine. A stale claim is
not: it will be stored, retrieved on Sunday morning, and acted on.

## The league, so you weigh things correctly

This league has {{team_count}} teams. {{scoring_summary}}

That changes what counts as news. In a shallow league there is always somebody
useful unowned, so "the backup is now the starter" is a bigger deal than a small
change in an established player's role — she can actually go and get him. And if
catches score here, a change in how often a player is *thrown to* matters as much
as a change in his yards.

## Her roster

{{roster}}

## The best players she could still claim

{{free_agents}}

## What to look for

For every player above, on her roster and on the wire:

1. **Injuries.** Is he hurt? What is the official game status — out, doubtful,
   questionable — and when was it last updated? Is he expected back, and when?
2. **Role changes.** Is he getting the ball more or less than he was? Has somebody
   else taken his job, or has he taken somebody else's?
3. **Depth-chart moves.** Has an injury or a trade ahead of him promoted him? This
   is the single most valuable thing you can find, because it is how a player who
   is free this week becomes a starter next week.
4. **Byes coming up.** If his team has its bye in the next two weeks, say so.

Write each finding as one or two plain sentences that say **what changed and what
it means for whether she should start him or go and get him**. She knows the rules
of football and nothing about fantasy football, so:

- No jargon without a definition in the same sentence. Not "he is a high-volume
  target hog"; "he is thrown to more often than anyone else on his team, which is
  worth a lot here because every catch scores".
- Never invent a number. If you cannot find the statistic, describe it in words.
- Name the player exactly as ESPN spells him above. A note filed under a different
  spelling is a note nothing will ever find again.

Set `days_valid` honestly. A weekly injury report is good for about seven days. A
season-ending injury is good for the rest of the season. A note about a role that
has settled is good for a few weeks.

Return the JSON object the schema describes. If a player has genuinely nothing new
about him, leave him out — an empty note is worse than no note.
