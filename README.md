# hal-mary

A Claude-powered fantasy football advisor for people who do not follow football.

Caroline is in an ESPN fantasy league. She and Bryan know the rules of the game and nothing else —
no players, no strategy, no idea how a draft works. `hal-mary` closes that gap: it watches the league
on ESPN, researches the live internet through the local `claude` binary, and tells her what to do in
plain English. She makes every click herself; the application never writes to ESPN.

## What it does

- **Draft day.** Builds a tiered board ahead of time, follows the draft pick by pick, and when her
  turn approaches, names who to take and why — in seconds, because the research already happened.
- **In season.** Sweeps injury and role news, scans waivers on Tuesday, checks the lineup before
  Sunday kickoff, and recaps what happened and what to learn from it.
- **Any time.** A chat box wired to Claude with web access and the league's full history in memory,
  so "should I trade this guy?" is a question she can just ask.

## Running it

```bash
uv sync
cp .env.example .env        # fill in ESPN cookies and league id
uv run hal-mary sync        # pull league state
uv run hal-mary serve       # web app + scheduler
```

See [`CLAUDE.md`](CLAUDE.md) for conventions and commands,
[`docs/superpowers/specs/`](docs/superpowers/specs/) for the design of record, and
[`docs/DECISIONS.md`](docs/DECISIONS.md) for why it is built this way.

## Status

Under active construction against a draft deadline. This repository is entirely AI-authored.
