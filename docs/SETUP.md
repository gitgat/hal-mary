# Setup

What a human has to do once, by hand, before hal-mary can see the league. Everything else the
application does for itself.

## 1. ESPN credentials

hal-mary reads the league through ESPN's fantasy API, which authenticates with two browser cookies.
There is no API key and no OAuth flow. This is why the step is manual.

**Whose account matters.** Use the account that is actually in the league. Caroline's team is the one
hal-mary advises, so her cookies are the ones to use. A cookie from an account that cannot see the
league returns HTTP 401 no matter how well-formed it is.

In a desktop browser, signed in to `fantasy.espn.com`:

1. Open the league page.
2. Open developer tools (`F12`), go to **Application** in Chrome or **Storage** in Firefox.
3. Under **Cookies**, select `https://fantasy.espn.com`.
4. Copy the value of **`espn_s2`**. It is long, a few hundred characters, and contains `%` escapes.
   Copy it exactly, including those.
5. Copy the value of **`SWID`**. It looks like `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`. **Keep the
   braces.** Stripping them is the single most common reason for a 401 here.

These are session credentials. Anyone holding them can act as that ESPN account, so they go in `.env`
and nowhere else. `.env` is gitignored. Never paste them into a commit, an issue, or a chat log.

They also expire, typically after a few weeks or when the account signs out everywhere. When
in-season syncing suddenly fails, expired cookies are the first thing to check. The `/status` page
says so explicitly when it detects a 401.

## 2. League and team identifiers

The league id is in the URL of any league page:

```
https://fantasy.espn.com/football/league?leagueId=123456
                                                  ^^^^^^
```

The team id is in the URL of Caroline's team page, as `teamId=`. If the team page does not show one,
run `uv run hal-mary sync` with the league id set and read the team list it prints.

## 3. `.env`

```bash
cp .env.example .env
```

Fill in:

| Key | What it is |
|---|---|
| `ESPN_S2` | the `espn_s2` cookie value |
| `SWID` | the `SWID` cookie value, braces included |
| `LEAGUE_ID` | the league id from the URL |
| `TEAM_ID` | Caroline's team id |
| `SEASON` | the season year, e.g. `2026` |
| `WEB_PASSWORD` | a password for the web app; anyone on the network who knows it can read the league |
| `DB_PATH` | where the database lives; defaults to `./hal.db`. A relative value is resolved against the directory holding `config.toml`, **not** the working directory — see below. Set it absolute for a real deployment. |

### Every path is resolved against `config.toml`, not the working directory

`paths.prompts_dir`, `paths.memory_dir`, `claude.scratch_dir`, `claude.system_prompt_file` and
`DB_PATH` are all anchored to the directory holding the `config.toml` that was actually loaded —
which `HAL_MARY_CONFIG` can move. Absolute values are used exactly as given.

This matters because the failure it prevents is silent. Resolved against the *working* directory, a
service started anywhere but the checkout finds no `memory/`, so every prompt goes out without the
standing context that says who Caroline is and what the league's rules are — no crash, no error, just
worse advice — and a relative `DB_PATH` opens a brand new empty database instead of the real one.

The status page has a **"Where the files are"** card listing every resolved path and whether it
exists. If the advice ever looks like it has forgotten who it is talking to, look there first:
a missing memory directory is called out by name in the problems box at the top of that page.

Verify with:

```bash
uv run hal-mary espn-check     # exits nonzero if the cookies do not work
uv run hal-mary sync           # pulls the league and prints a summary
```

That first sync creates `memory/league.md` — it does not ship in the repo, because it is written
from the live ESPN payload and holds real leaguemates' names and the league id. It is gitignored
for that reason; `memory/league.example.md` is the tracked placeholder showing its shape. Anything
you write below the `<!-- hal-mary:preserve-below -->` line survives every later sync verbatim.

## 4. Claude

hal-mary shells out to the `claude` binary and uses whatever subscription that binary is logged into.

```bash
claude --version     # must be present on PATH
claude               # run once interactively to log in, if it has not been
```

On the production VM this login happens once, as the user the service runs as. The service inherits
that session; there is no key to configure.

## 5. Running it

```bash
uv sync
uv run hal-mary serve
```

The startup line prints the LAN URL. Open it on a phone on the same network, or over the VPN.
