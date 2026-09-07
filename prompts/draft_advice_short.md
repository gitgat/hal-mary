# Quick: who should Caroline draft right now?

**The first attempt at this did not come back in time. This is the retry, and the
pick clock has been running for it.** Answer immediately from what is in front of
you. Do not deliberate, do not weigh a fifth option, and do not ask for anything
you were not given.

Pick {{next_overall_pick}}, round {{round_num}} of {{rounds}}, {{team_count}} teams. Her next picks
are {{my_next_picks}}. The {{candidate_count}} best players left are above, with their tiers,
along with the starting slots she still has to fill.

Take the best player she can actually start, preferring the position where the
board drops off soonest. {{scoring_summary}}

Return the JSON you were given the schema for:

- `pick` — one name, spelled as the board spells it.
- `reason` — **one sentence**, no jargon, saying why him.
- `backups` — one player to take instead if he is gone, with a short reason.
- `watch_out` — one short sentence, or an empty string if nothing stands out.

Caroline has never played fantasy football and does not know any of the words the
hobby uses. Write as you would to a friend who knows football and nothing else.
