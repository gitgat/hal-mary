# Build Caroline's draft board

Research the {{season}} fantasy football season and produce a ranked, tiered draft
board of about {{board_size}} players for **this specific league**. This runs the day
before the draft, so take the time to do it properly: search the web, read recent
sources, and check what you believe against them.

## This league is not a normal league

Almost every ranking list you will find online is written for a twelve-team
league with standard scoring. **This league is neither.** If you rank players as
though it were, every piece of advice built on this board will be confidently
wrong. Three facts change the answer, and they are all true at once.

**1. It is a six-team league — {{team_count}} teams.**
A six-team league is small. Far more good players go undrafted than in a twelve-team
draft, so the worst starter Caroline could get at any position is still pretty
good. Practical consequences:

- Hoarding a scarce position matters much less. Someone useful is always available.
- Streaming a position — taking whoever has a good matchup each week instead of
  owning a star — is genuinely viable here, especially at quarterback, kicker and
  defence.
- Do not tell her to "reach" for a position because it will dry up. In a
  {{team_count}}-team league it mostly does not.
- Elite players are worth relatively more, because the gap between an elite player
  and the freely-available replacement is the whole edge in a shallow league.

**2. It is full PPR.**
{{scoring_summary}}
That means players who catch a lot of passes are worth substantially more than a
standard-scoring ranking gives them: pass-catching running backs, receivers who
get thrown to constantly even for short gains, and tight ends who are used like
receivers. A running back who only runs the ball is worth less here than his raw
yardage suggests. Adjust the rankings you find rather than copying them.

**3. Caroline drafts last in round one, and her first two picks are back to back.**
She has draft slot {{my_draft_slot}} of {{team_count}}, and it is a snake draft, so she picks
**{{first_two_picks}}** — one after the other — and then waits a long time. Her full
set of picks is: {{all_my_picks}}.

Two picks together is one decision, not two. Rank the board so that the pair at
the turn can complement each other rather than duplicate a position, and say so
in the notes where it matters ("if she takes a running back at 6, this is the
receiver to pair with him at 7").

## The rest of the league's settings

- Season: {{season}}
- League: {{league_name}}
- Teams: {{team_count}} teams
- Scoring type as ESPN reports it: {{scoring_type}}
- Draft: {{rounds}} rounds, {{total_picks}} picks in total, snake order
- Draft date: {{draft_date}}
- Starting lineup and bench:
{{roster_slots}}

A slot listed as **RB/WR/TE** — some leagues call the same thing a **flex** — is one
extra starter who can be a running back, a receiver or a tight end, whichever of
the three she has the best spare option at. `BE` is the bench: players she owns
but does not start. `IR` is injured reserve and is not drafted, which is why
there are {{rounds}} rounds and not one more.

A **bye week** is the one week in the season a player's real team does not play,
so he scores nothing that week; if too many of her starters share a bye week she
will have a bad week, so record each player's bye week accurately.

## What to research

Search the web. Do not rank from memory — your training data is older than this
season, and a player who changed teams, got hurt, or lost his starting job since
then is exactly the player a bad board gets wrong.

Look for, and cite:

1. **Current expert rankings for full PPR scoring**, from at least two independent
   sources, ideally updated within the last two weeks.
2. **Average draft position** — where each player is actually being drafted in
   real drafts this season. Average draft position is the market's opinion; where
   your ranking disagrees with it sharply, say so in the note, because that is
   either the best value on the board or a mistake.
3. **Injuries** — who is hurt now, how long he is expected to be out, and who is
   merely "limited in practice". An injured star is not automatically a bad pick,
   but Caroline needs to be told.
4. **Holdouts, suspensions and contract disputes** — anyone who may not play the
   opening weeks.
5. **Rookies and second-year players** — genuinely uncertain, sometimes worth it.
   Say plainly when a ranking is a guess about someone with no professional record.
6. **Changed roles** — a player who lost or won a starting job, a new coach who
   throws more, a backfield now split between two players.

Prefer recent sources over authoritative-sounding old ones. Cite the URL you
actually read for each player you make a specific claim about, in `source_url`.
If you cannot find a source for a claim, do not make the claim.

## Tiers

Group the players into tiers, best first, tier 1 being the best.

**A tier is a set of players who are genuinely interchangeable** — if the top of a
tier is taken, you would shrug and take the next one. A tier ends where you would
be meaningfully disappointed to get the next player instead.

Tiers, not ranks, are what make "who should I take" answerable on a 90-second
clock. Ranks say player 14 is better than player 15, which is a distinction too
fine to act on. Tiers say "these four are the same, and after them there is a
real drop" — which tells Caroline whether to take the position she needs now or
wait one more round. Get the tier boundaries right even if the order inside a
tier is arguable.

Every player you return must have a tier. Tiers may be any size; a tier of one
player is a real answer and means he is alone at his level.

## The note on each player

One or two sentences per player, and they are written for a beginner.

**Caroline knows the rules of football and nothing else.** She knows what a
touchdown is and what a quarterback does. She has never played fantasy football.
She does not know what PPR, flex, target share, snap count, floor, ceiling,
handcuff, or average draft position mean. Write as though explaining to a smart
friend who has never heard any of these words: no jargon, and where a number is
the point, say what the number means.

Bad: "Elite target share and red-zone usage; RB1 upside with a high floor."
Good: "He is thrown the ball more than almost anyone in the league, and here every
catch is worth a point, so he scores steadily every week."

Each note should say **why he is at this level** and **what the risk is**. The risk
half is not optional — a note with no risk in it is a note that will look
foolish. "He was hurt for most of last season and has not proved he is back" is
worth more to her than another sentence of praise.

## What to return

Return JSON matching the schema you were given: an object with a `players` array,
each entry having `name`, `position`, `pro_team`, `tier`, `rank`, `bye_week`,
`note` and `source_url`.

- `name` — the player's full name, spelled the way ESPN spells it. This is how his
  name is matched to the draft as it happens, so a misspelling means hal-mary will
  not notice he has been taken.
- `position` — one of QB, RB, WR, TE, K, D/ST.
- `rank` — overall rank for this league, starting at 1, no gaps and no ties.
- `tier` — 1 is best, as described above.
- `bye_week` — the week number his team does not play, or null if you could not
  find it. Do not guess it.

Include kickers and defences, but rank them where they belong in a
{{team_count}}-team league: near the end, because the difference between the best
and the twentieth-best is small and Caroline should spend her early picks on
players who score every week.
