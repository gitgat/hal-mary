# Build Caroline's draft board

Research the {{season}} fantasy football season and produce a ranked, tiered draft
board of about {{board_size}} players for **this specific league**. This runs before
the draft, so take the time to do it properly: search the web, read several
independent sources, and check what you believe against them.

You are not summarising a ranking. You are building one. Every list you will find
was written for a different league from this one, and the gap between those lists
and the right answer here is the entire value of this job. If the board you return
could be got by retyping any single article you read, the job has failed even when
every row in it is defensible.

## 1. The three facts that override anything you read

Almost every ranking online is written for a twelve-team league in which a catch
is worth nothing on its own. **The three facts below beat anything you find.** If
you rank players for the league those lists assume rather than the one described
here, every piece of advice built on this board will be confidently wrong.

**1a. This league has {{team_count}} teams, and here is what that actually means:**

{{league_shape}}

Work out from those numbers where **replacement level** sits — the quality of the
player Caroline could pick up free, off the unowned list, in any week of the
season. That number is the whole game, and it is different here from the league
every published list assumes:

- The fewer teams there are, the more good players go undrafted, so the worst
  starter she could end up with at any position is much better than those lists
  imply. Look at the counts above: if only a handful of players at a position
  start league-wide each week, then the tenth-best or twentieth-best player at
  that position is sitting unowned all season.
- So **hoarding a position matters less**, and *streaming* one — taking whoever
  has a good matchup that week instead of owning a star — becomes viable,
  especially at quarterback, kicker and defence.
- So **do not tell her to reach** for a position out of fear it will dry up.
  Mostly it does not. A "run" on a position needs a lot of teams to happen.
- But **the very best players are worth relatively more**, because the gap
  between an elite player and the freely available replacement is the whole edge
  in a shallow league. Elite scarce talent up top, indifference at the bottom.
- If this league has twelve teams or more, none of the above applies and the
  published rankings need less adjustment.

Say this out loud in the notes where it changes an answer. A player the internet
ranks 40th because his position is thin in a deep league does not deserve that
rank here, and the note should say why in one clause.

**1b. The scoring, which decides how much a catch is worth.**
{{scoring_summary}}

If catches score here, players who catch a lot of passes are worth substantially
more than a ranking written for catch-free scoring gives them: pass-catching
running backs, receivers thrown to constantly even for short gains, and tight ends
used like receivers. A running back who only runs the ball is worth correspondingly
less. If catches score nothing, do the opposite. Either way, adjust the rankings
you find rather than copying them.

**1c. What the season is actually a race for.**
{{playoff_summary}}

This changes which kind of player is worth more. When the places are decided by
total points across the season, the roster that scores the most in total wins,
so steady weekly scoring is worth more than a player who is enormous three times
and absent otherwise. When most of the league reaches the playoffs, a single bad
week is survivable and there is less reason to chase a boom-or-bust player early.
When the places are decided by won-lost record, or when only a few teams qualify,
the reverse holds and a high ceiling is worth more. Reason from the sentence
above; do not assume the usual answer.

**1d. Caroline picks from slot {{my_draft_slot}} of {{team_count}} in a
{{draft_type}} draft.**
That gives her picks **{{first_two_picks}}** first, and in full: {{all_my_picks}}.

Look at those first two numbers. If they are consecutive, or nearly so, she is at
the turn: the two picks are one decision rather than two, and she then waits a
long time. Rank the board so that a pair taken at the turn can complement each
other rather than duplicate a position, and say so in the notes where it matters
("if she takes a running back with the first of the pair, this is the receiver to
pair with him"). If instead her picks are evenly spaced, say which single player
is the right one at each.

## 2. Sourcing: no single website may decide this board

This is a hard requirement, not a preference. The last board built here cited one
ranking article for more than half its players, which means it was that article
with corrections — and every other manager in this league can read that article.

- **Consult at least four genuinely independent outlets** before you rank
  anybody. Independent means different organisations, not four pages on one site
  and not four articles that all cite the same projection model. A ranking, a
  market average draft position, an injury tracker and a beat reporter are four
  different kinds of evidence; four rankings are close to one.
- **No single website may be the `source_url` for more than
  {{max_source_players}} of the {{board_size}} players.** If you notice yourself
  reaching for the same link repeatedly, that is the signal to go and read
  something else, not to keep citing it.
- **`source_url` is the source that actually decided this player's place** — the
  page that made you rank him where you did. It is not a general reference and it
  is not the last page you happened to open. If a player's placement was decided
  by an injury report, cite the injury report, not a ranking that predates it.
- **Where two sources disagree, that disagreement is the information.** Say in
  the note which one you followed and why — "one site has him tenth and the
  market is drafting him thirtieth; the market is right because ..." — and cite
  the one that decided it. A player everybody agrees on needs no such sentence;
  a player they split on is where the board earns its keep.
- Prefer recent sources over authoritative-sounding old ones. If you cannot find
  a source for a claim, do not make the claim.

## 3. What to research

Search the web. Do not rank from memory — your training data is older than this
season, and a player who changed teams, got hurt, or lost his starting job since
then is exactly the player a bad board gets wrong.

Look for, and cite:

1. **Current expert rankings**, from at least two independent organisations,
   ideally updated within the last two weeks, and matched to this league's
   scoring where you can find them ranked that way.
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
   throws more, a backfield now split between two players. A player who changed
   teams over the summer is the single most common thing a stale list gets wrong,
   so check every name you are unsure about.
7. **Bye weeks.** A bye week is the one week in the season a player's real team
   does not play, so he scores nothing that week. Record each player's bye week
   accurately, from a schedule, and never guess it — every player on the same
   real-life team has the same bye week, so check yours agree.

## 4. How players interact with each other

A pick is not judged on its own. It is judged against the roster it joins, and
this section is what a ranked list bought off the internet cannot give her. Where
one of these applies, **say so in that player's note, naming the other player**,
because the note is the only thing that survives into draft night.

**Two players from the same real team who score together.** A quarterback and one
of his own receivers score on the same throw: the quarterback gets points for the
pass and the receiver for the catch, so the two of them have big weeks together.
This is called a *stack*. Be honest about what it does — it does not add points,
it makes her weekly total swing further in both directions. In a league where the
places are decided by total points over a whole season, extra swing is worth
little and can cost her; in a one-off tournament it is worth a lot. Do not
cargo-cult tournament advice into a season-long league. Mention a pairing where
one player is genuinely better because of the other (a receiver whose quarterback
is excellent, a tight end in an offence that throws to tight ends), and say when
it is only extra variance.

**The backup to a running back she has already drafted.** If the starter gets
hurt, his backup inherits the whole job overnight and becomes a starter-quality
player for free. The jargon for this is a *handcuff*; never use that word in a
note. It is worth a late pick only when the starter is one of her most valuable
players and the backup would clearly take over — not as a routine habit, and
never in the early rounds. In a league this shallow, note that the same backup
may well be sitting unowned when the injury actually happens, which weakens the
case for spending a pick on him now.

**Bye-week collisions.** If too many of the players she would start share the
same bye week, she has one week where she cannot field a full team. Look at the
bye weeks of the players clustered near each of her picks, and where several of
the obvious choices share one, say so in the note: "he does not play in week 9,
the same week as ..., so if she already has one of them she should lean the
other way." Do not move a clearly better player down the board for a bye week —
it is a tiebreaker between similar players, not a ranking factor.

**Two players who take work from each other.** Two running backs on the same real
team split the carries; a receiver's value drops when his team signs another
one. Where a player's placement depends on a teammate, name the teammate.

**Position balance across the whole draft.** She must fill every starting slot
listed below, and one round is one player. Late in the board, prefer the player
who fills a slot she will otherwise struggle to fill over a marginally better
player at a position she will already have three of.

## 5. The rest of the league's settings

- Season: {{season}}
- League: {{league_name}}
- Teams: {{team_count}} teams
- Scoring type as ESPN reports it: {{scoring_type}}
- Draft: {{draft_type}}, {{rounds}} rounds, {{total_picks}} picks in total
- Draft date: {{draft_date}}
- Starting lineup and bench:
{{roster_slots}}

The slot ESPN spells `RB/WR/TE` — some leagues call the same thing a **flex** — is
one extra starter who can be a running back, a receiver or a tight end, whichever
of the three she has the best spare option at. `BE` is the bench: players she owns
but does not start, who score nothing. `IR` is injured reserve and is not drafted,
which is why there are {{rounds}} rounds and not one more.

## 6. Tiers

Group the players into tiers, best first, tier 1 being the best.

**A tier is a set of players who are genuinely interchangeable** — if the top of a
tier is taken, you would shrug and take the next one. A tier ends where you would
be meaningfully disappointed to get the next player instead.

Tiers, not ranks, are what make "who should I take" answerable on a short clock.
Ranks say player 14 is better than player 15, which is a distinction too fine to
act on. Tiers say "these four are the same, and after them there is a real drop" —
which tells Caroline whether to take the position she needs now or wait one more
round. Get the tier boundaries right even if the order inside a tier is arguable.

Every player you return must have a tier. Tiers may be any size; a tier of one
player is a real answer and means he is alone at his level.

## 7. The note on each player

One or two sentences per player, and they are written for a beginner.

**Caroline knows the rules of football and nothing else.** She knows what a
touchdown is and what a quarterback does. She has never played fantasy football.
She does not know what PPR, flex, target share, snap count, floor, ceiling,
handcuff, stack, streaming, or average draft position mean. Write as though
explaining to a smart friend who has never heard any of these words: no jargon,
and where a number is the point, say what the number means. Never let a bare
position code stand alone as a description — "quarterback", not "QB".

Bad: "Elite target share and red-zone usage; RB1 upside with a high floor."
Good: "He is thrown the ball more than almost anyone in the league, and here every
catch is worth a point, so he scores steadily every week."

Each note should say **why he is at this level** and **what the risk is**. The risk
half is not optional — a note with no risk in it is a note that will look
foolish. "He was hurt for most of last season and has not proved he is back" is
worth more to her than another sentence of praise.

Where one of these applies, it belongs in the note too, in a clause:

- the league's shape moved him away from where the internet has him, and why;
- the sources disagreed about him, and which one you believed;
- he depends on, or competes with, a named team-mate;
- his bye week collides with other players she is likely to own.

## 8. What to return

Return JSON matching the schema you were given: an object with a `players` array,
each entry having `name`, `position`, `pro_team`, `tier`, `rank`, `bye_week`,
`note` and `source_url`.

- `name` — the player's full name, spelled the way ESPN spells it. This is how his
  name is matched to the draft as it happens, so a misspelling means hal-mary will
  not notice he has been taken.
- `position` — one of QB, RB, WR, TE, K, D/ST. This field is a code because ESPN's
  data is; the prose in `note` is not.
- `pro_team` — the abbreviation of the real NFL team he plays for **this** season.
- `rank` — overall rank for this league, starting at 1, no gaps and no ties.
- `tier` — 1 is best, as described above.
- `bye_week` — the week number his team does not play, or null if you could not
  find it. Do not guess it.
- `source_url` — the one page that decided his place, subject to the cap in
  section 2.

Include kickers and defences, but rank them where they belong in a
{{team_count}}-team league: near the end, because the difference between the best
and the twentieth-best is small and Caroline should spend her early picks on
players who score every week.
