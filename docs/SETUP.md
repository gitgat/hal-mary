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
| `MCP_TOKEN` | a bearer token for the `/mcp` endpoint, which is how Claude Cowork and Claude Desktop reach hal-mary. **Optional, and deliberately not `WEB_PASSWORD`** — two doors, two keys. `openssl rand -hex 32`. Leave it unset and `/mcp` answers 503 to everything; absent never means open. See [`COWORK.md`](COWORK.md) and the MCP section of the runbook. |
| `DB_PATH` | where the database lives. Leave it empty and it defaults to `~/hal-mary-data/hal.db` — on local disk and outside the checkout, which is what you want. A *relative* value is resolved against the directory holding `config.toml`, **not** the working directory, which puts the database inside the checkout; `hal-mary doctor` reports that. |

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
uv run hal-mary doctor         # everything above, in one place, before anything runs
uv run hal-mary espn-check     # exits nonzero if the cookies do not work
uv run hal-mary sync           # pulls the league and prints a summary
```

`doctor` is the fastest way to find out what is still missing: the `.env` keys, `claude` on the PATH
and logged in, the prompts and memory directories, and whether the database directory is writable
and on local disk rather than an NFS mount. It touches no network and spawns nothing. It is also
what `deploy/install.sh` and `deploy/deploy.sh` gate on — `serve` itself always starts and reports
problems on the status page instead, so that a box with something missing is still serving the page
that explains what.

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

## 6. Draft night

The draft order is the one thing hal-mary cannot know before the draft opens. This league's
`draftSettings.orderType` is `DRAFT_START`, so ESPN draws the order at the moment the draft begins
and everything before that is a placeholder.

hal-mary handles this itself: on the first poll that sees a real pick, the draft loop reads ESPN's
own draft board, stores the order it finds, and the draft page, the advice card and the loop all use
it from then on.

**But it can only read that board once a real pick has landed**, and there is a window before then —
between the draft opening and pick 1 — in which every number on the page is still the pre-draft
placeholder. That window is the dangerous one. If ESPN draws Caroline **first overall**, the
placeholder puts her opening pick five away, past the advisor's window, so no advice card is written
at all while the page says four picks out. The draft page says the numbers are provisional until the
first pick lands, and this is what to do about it:

**Do this every draft, not only when something looks wrong:**

1. Tap **The draft has started** on the draft page **once, the moment the draft room opens.** It
   sits in the note that says the pick numbers are provisional, so there is nothing to go and find.
   It syncs — re-reading `pickOrder`, which ESPN has drawn for real by then, closing the window
   before pick 1 — and it puts the loop on its draft-night five-second cadence straight away instead
   of at the end of the current five-minute check. Then it tells you what it found.
2. Then eyeball "on the clock" on the draft page against ESPN's draft room for two picks.

That button is an **override, not the mechanism**. Forgetting it costs at most one five-minute
interval: the loop switches to draft-night cadence on its own from the first pick ESPN reports, and
the draft page says which cadence it is on ("watching ESPN every 5 seconds" / "checking ESPN every 5
minutes"). What the button buys is the gap before pick 1, which is the window described above.

**If the loop is not running** — no ESPN credentials, a loop that failed to start (the page says so
in a band across the top), or picks being entered by hand — nothing ever reads the drawn board, so
step 1 is the *only* thing correcting the order and step 2 is the only thing checking it. The draft
page keeps saying the pick numbers are provisional for as long as that is true, including all the
way through a hand-entered draft: the note clears when hal-mary has read the order ESPN drew, not
when the first pick lands.

Do the eyeballing even if the numbers look plausible. A wrong draft order is a *silent* failure: the
page and the advice card compute her position the same way from the same list, so they agree with
each other while both being wrong, and no banner fires. A human comparing two screens is the only
thing that catches it.

## 7. Running it as a service

Everything above is for a developer's checkout. For the production VM — creating it, installing the
systemd unit, reading logs, rotating the ESPN cookies mid-season, backups and rollback — see the
**runbook** in the second half of [`README.md`](../README.md).
