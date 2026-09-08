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
uv run hal-mary doctor      # is this box able to run hal-mary at all?
uv run hal-mary espn-check  # check the ESPN cookies work
uv run hal-mary sync        # pull league state
uv run hal-mary job board_build   # research the draft board (do this before the draft)
uv run hal-mary serve       # web app + draft loop; prints the URL to open on a phone
```

See [`docs/SETUP.md`](docs/SETUP.md) for first-time setup (ESPN credentials, league ids, the
Claude login), the **runbook below** for running it as a service, [`CLAUDE.md`](CLAUDE.md) for
conventions and commands, [`docs/superpowers/specs/`](docs/superpowers/specs/) for the design of
record, and [`docs/DECISIONS.md`](docs/DECISIONS.md) for why it is built this way.

---

# Runbook

**Written for the person reading it in six months having forgotten everything.** It assumes nothing.

hal-mary runs on its own VM as a **systemd user service**. Everything below is done over SSH as
`bryan`, on that box.

## Where everything is

| What | Where |
|---|---|
| The box | `bryan@hal-mary.thehalf.io` — `192.168.1.205` |
| The checkout | `~/hal-mary` |
| The database | `~/hal-mary-data/hal.db` (plus `-wal` and `-shm` beside it) — the default when `DB_PATH` is empty |
| Backups | `~/hal-mary-data/backups/hal-<timestamp>.db`, nightly |
| Secrets | `~/hal-mary/.env` — mode 0600, gitignored, never in git |
| The unit files | `~/.config/systemd/user/hal-mary*.{service,timer}` |
| Their source | `~/hal-mary/deploy/` — edited there, copied across by `install.sh` |
| Logs | the systemd journal; there is no log file |
| The web app | `http://192.168.1.205:8080`, from a phone on the LAN |

### Always use the FQDN or the IP. Never `ssh bryan@hal-mary`.

The short name `hal-mary` has **no DNS record**. It falls through Pi-hole's wildcard
(`address=/thehalf.io/192.168.1.163`) and lands on `192.168.1.254` — the keepalived ingress VIP,
which answers as **`birdo`, the swarm manager**. It connects. It gives you a shell. It is the wrong
machine, and running install steps there would put a long-running service onto the cluster's control
plane, on a Raspberry Pi booting from an SD card.

This has already happened once during this project. Use `hal-mary.thehalf.io` or `192.168.1.205`.
The short name will start working only when someone adds a real DNS record for it.

(The polarity is the opposite of the warning in `swarm-config/docs/dev-scratch-swarm-access.md`,
where the short name is right and the FQDN wrong. Do not generalise either rule. Resolve the name
and look at the answer.)

---

## 1. First install, from nothing

### 1.1 Create the VM

A Proxmox VM, Ubuntu 24.04, x86_64. The one already built has 8 cores, 6 GB RAM and 28 GB of disk,
which is comfortable. Give it a static lease at `192.168.1.205` and an SSH key — it accepts
**publickey only**, password authentication is off, so `ssh-copy-id` cannot bootstrap it. The key
has to go on from the Proxmox console.

**Do not join it to the Docker Swarm.** It is a standalone application host. The cluster already has
three managers across three failure domains; a fourth would raise the Raft quorum from 2 to 3 while
still surviving only one loss, which is strictly worse.

### 1.2 Provision it

From dev-scratch:

```bash
ssh bryan@hal-mary.thehalf.io 'bash -s' < deploy/provision-vm.sh
```

Idempotent, safe to re-run. Installs node 22, Claude Code, `uv`, `sqlite3`, `git` and `jq`; enables
**linger**, so a user service survives logout and starts at boot; and creates `~/hal-mary-data` on
the VM's own disk.

Deliberately no Docker and no Claude Code plugins — hal-mary invokes `claude` with
`--setting-sources "" --strict-mcp-config`, so every call ignores installed plugins and MCP servers
by design.

### 1.3 Log `claude` in — the one step no script can do

```bash
ssh bryan@hal-mary.thehalf.io
claude          # follow the login flow, once
```

`claude` authenticates from a **subscription session in `~/.claude.json`**, not from an API key.
There is nothing to put in `.env`. The service inherits that session because it runs as this user,
which is the whole reason hal-mary is a systemd *user* unit rather than a system one, and the reason
it is not in a container.

Until this is done, hal-mary serves every page and produces **no advice at all**. `install.sh`
refuses to proceed without it. Confirm with `claude -p hello`; "Not logged in · Please run /login"
means it is not done.

### 1.4 Get the code onto the box

```bash
git clone git@github.com:gitgat/hal-mary.git ~/hal-mary
```

If the box has no GitHub deploy key: add one, use an HTTPS URL, or `rsync -a` the checkout across
from dev-scratch. Any of the three is fine — the scripts only need `~/hal-mary` to be a git checkout
with a remote it can fast-forward from.

### 1.5 Fill in `.env`

```bash
cd ~/hal-mary
python3 scripts/espn-auth.py     # asks for the cookies, verifies them, finds the team id
```

That script writes `~/hal-mary/.env` at mode 0600 and **merges** — it leaves keys it did not ask
about alone. Or copy `.env.example` and fill it in by hand; [`docs/SETUP.md`](docs/SETUP.md) says
where each value comes from.

**`DB_PATH` may be left empty.** It then defaults to `~/hal-mary-data/hal.db`, which is where it
belongs. Writing it out explicitly is still clearer:

```
DB_PATH=/home/bryan/hal-mary-data/hal.db
```

What must not happen is a *relative* value. Every configured path is resolved against the directory
holding `config.toml` — the checkout — so `DB_PATH=./hal.db` puts the database, and the `backups/`
directory that follows it, inside the one directory `git pull` rewrites, a rollback moves, and a
re-clone loses. `hal-mary doctor` has a **database location** check for exactly this, and it is
fatal once `~/hal-mary-data` exists.

The other rule is the filesystem: `~/hal-mary-data` is on the VM's **own disk**. `/var/data` on
every machine in this homelab is a TrueNAS NFS export, and **SQLite on NFS corrupts**. Doctor checks
that too and refuses an install onto a network mount.

### 1.6 Install

```bash
~/hal-mary/deploy/install.sh
```

It checks everything before it installs anything — `uv`, the checkout, a populated `.env`, a
writable data directory, and then `hal-mary doctor`, which is where "claude is not logged in" is
caught. If any of that fails, the box is left exactly as it was. Then it copies the three unit files
into `~/.config/systemd/user/`, enables linger, starts `hal-mary.service` and
`hal-mary-backup.timer`, and waits for `/healthz` to answer before claiming success.

If `doctor` refuses and you are certain it is wrong — the `claude` login check reads an undocumented
key in Claude Code's own config file, so it *can* be wrong if that format changes — then
`HAL_MARY_SKIP_DOCTOR=1 ~/hal-mary/deploy/install.sh`. The checks still run and still print; only
the refusal is turned off, and the output says so in capitals. The same variable works on
`deploy.sh`.

The units say `%h/hal-mary`. If the checkout is somewhere else, `install.sh` substitutes the real
path in as it installs; when it is the default the installed file is byte-identical to
`deploy/hal-mary.service`, so `diff` against the checkout is a meaningful check.

### 1.7 Verify

```bash
systemctl --user status hal-mary
curl -fsS http://127.0.0.1:8080/healthz               # {"status":"ok"}
systemctl --user list-timers hal-mary-backup.timer
```

Then open `http://192.168.1.205:8080` on a phone on the LAN, log in with `WEB_PASSWORD`, and press
**Sync** on the status page.

---

## 2. Is it broken, and which kind of broken?

There are two completely different failures with two different answers, and getting this backwards
costs an hour.

| Symptom | The question | Where to look |
|---|---|---|
| The page will not load at all | Is the **service** running? | `systemctl --user status hal-mary` |
| Pages load; ESPN data is stale, sync fails, a red box mentions 401 | Have the **ESPN cookies** expired? | the **`/status`** page in the app |
| Pages load; advice is generic or never arrives | Is **`claude`** working? | `/status` has a Claude row — then `journalctl` |
| Advice reads as if it has forgotten who Caroline is | Are the **paths** right? | the "Where the files are" card on `/status` |

**`systemctl` answers "is the process up". `/status` answers everything else.** That page is built
for exactly this: every problem it can detect goes in a red box at the top — missing `.env` keys, an
ESPN 401, a missing `claude`, a missing memory directory, a sync that failed — and it lists every
resolved path with whether it exists.

That is also why the service **boots even when something is wrong**. A unit that refused to start
because the memory directory had gone missing would be down at 2am with nobody watching, and the one
page that would have told you why is the page that no longer loads. So it starts, it serves, and it
says on `/status` what is wrong. The refusing is done by `hal-mary doctor` at install and deploy
time, when a human is actually looking.

### Reading logs

```bash
journalctl --user -u hal-mary -f                      # follow, live
journalctl --user -u hal-mary -n 200 --no-pager       # the last 200 lines
journalctl --user -u hal-mary --since "1 hour ago"
journalctl --user -u hal-mary -p err                  # errors only
journalctl --user -u hal-mary-backup --since yesterday
```

There is no log file. One place to look, rotated by systemd.

`--user` matters. Without it you are asking about a *system* unit that does not exist, and you get
an empty result rather than an error — which reads exactly like "the service has never logged
anything".

### Restarting

```bash
systemctl --user restart hal-mary
systemctl --user stop hal-mary
systemctl --user start hal-mary
```

If it refuses to start and `status` says something like `start request repeated too quickly`:

```bash
systemctl --user reset-failed hal-mary
systemctl --user start hal-mary
```

### `HAL_MARY_ENV points at ... which is not a file`

The unit names `~/hal-mary/.env` explicitly, and the config loader refuses to start when a path it
was *told* to use is not there — a typo'd path that silently loaded no secrets is the failure that
check exists to prevent. So this is one of the few things that does stop the service outright, and
it means the `.env` file has been moved or deleted. Restore it (`python3 scripts/espn-auth.py`
rebuilds it) and restart.

### `claude: command not found` in the journal

This is the most likely way hal-mary ends up running and useless. A systemd user unit does **not**
inherit your interactive shell's `PATH`, and both `uv` and `claude` are installed per-user
(`~/.local/bin`, `~/.npm-global/bin`). The unit sets `Environment=PATH=` explicitly for that reason.

Check what the running service actually has:

```bash
systemctl --user show hal-mary -p Environment
```

If `.npm-global/bin` is missing from it, the installed unit is out of date with
`~/hal-mary/deploy/hal-mary.service`. Re-run `install.sh` (or fix it by hand and
`systemctl --user daemon-reload && systemctl --user restart hal-mary`).

---

## 3. Rotating the ESPN cookies

**This is the maintenance event of the season.** ESPN's cookies are session credentials with no
refresh flow. They expire after a few weeks, or whenever that ESPN account signs out everywhere.
When they go, syncing stops and everything downstream quietly gets worse.

**Symptom:** `/status` shows a red box mentioning 401 or authentication, the ESPN row is not OK, and
the sync ages stop moving.

### The steps

1. **Confirm it is the cookies and not the service.**

   ```bash
   ssh bryan@hal-mary.thehalf.io
   cd ~/hal-mary
   uv run hal-mary espn-check
   ```

   Nonzero with a message about 401 or authentication means the cookies. A connection error means
   something else, and `journalctl` is the next stop.

2. **Get fresh values from a browser.** On a desktop, signed in to `fantasy.espn.com` **as the
   account that is actually in the league** — Caroline's. A cookie from an account that cannot see
   the league returns 401 no matter how well-formed it is.

   - Open the league page.
   - `F12` → **Application** (Chrome) or **Storage** (Firefox) → **Cookies** →
     `https://fantasy.espn.com`.
   - Copy **`espn_s2`**. It is a few hundred characters and contains `%` escapes. Copy it exactly,
     including those.
   - Copy **`SWID`**. It looks like `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`. **Keep the braces.**
     Stripping them is the single most common cause of a 401 here.

3. **Put them on the box.** The helper is the safe way: it verifies against ESPN *before* writing,
   merges into the existing `.env` rather than replacing it, hides the input, and re-writes at mode
   0600.

   ```bash
   cd ~/hal-mary
   python3 scripts/espn-auth.py
   ```

   It writes nothing if ESPN rejects the values, so a bad paste costs nothing.

   By hand instead: edit `~/hal-mary/.env`, replace `ESPN_S2=` and `SWID=`, change nothing else.
   Never `git add` it — it is gitignored, and these are live session credentials.

4. **Check they took.**

   ```bash
   uv run hal-mary espn-check      # must exit 0
   ```

5. **Restart the service so it picks up the new `.env`.** The file is read once at startup; editing
   it changes nothing until then. **This step is the one people forget**, and its symptom is
   "I fixed the cookies and the status page still says 401".

   ```bash
   systemctl --user restart hal-mary
   ```

6. **Confirm from the app.** Open `/status`, press **Sync**, and watch the sync ages go to "just
   now" and the red box disappear.

**Never paste these values into a commit, an issue, a chat log, or a test fixture.** Holding them is
equivalent to being logged in as that ESPN account.

**During a live draft, do not stop to do this.** The draft page takes picks by hand, and the advisor
runs against a board that was built before the draft, so a dead cookie costs the automatic pick
feed and nothing else. Cookies are a between-drafts job.

---

## 4. Deploying a change

```bash
ssh bryan@hal-mary.thehalf.io
~/hal-mary/deploy/deploy.sh
```

It fast-forwards, syncs dependencies, runs `hal-mary doctor`, applies migrations, runs the **full
test suite**, restarts the service, and then polls `/healthz` until it answers. It aborts, having
changed nothing that matters, if:

- the working tree is dirty or has untracked files — it names them;
- the branch has diverged from the remote — it will only `--ff-only`, never merge or rebase, because
  a merge commit created by a script on a production box is a commit nobody will ever review;
- `doctor` finds something fatal;
- **the test suite is red** — nothing is restarted and the service keeps running the old code;
- the service does not answer `/healthz` after the restart.

It prints the previous commit at the top and again at the end. That is the SHA to roll back to.

Two things worth knowing:

- **`memory/league.md` is generated and gitignored.** A checkout that still tracks it receives a
  deletion on pull, and git refuses when the local copy differs. That refusal is correct — the file
  holds real leaguemates' names — but it looks like a broken deploy. Fix with
  `git rm --cached memory/league.md`, then re-run; `hal-mary sync` rewrites the file.
- To deploy to a box `doctor` says is not ready — for instance, to ship the fix that makes it ready
  — `HAL_MARY_SKIP_DOCTOR=1 ~/hal-mary/deploy/deploy.sh`. The checks still run and print; only the
  refusal is turned off.
- `deploy.sh` **re-executes itself once, immediately after the pull**, and prints `re-reading` when
  it does. That is not a bug: a script rewritten underneath a running bash can resume at a stale
  byte offset and skip everything after it while still exiting 0. Today's git replaces a changed
  file with a new inode rather than truncating the old one, so it does not actually bite — the
  re-exec is what makes that an implementation detail of git rather than a load-bearing assumption.

### Rolling back

```bash
cd ~/hal-mary
git log --oneline -10               # or use the SHA deploy.sh printed
git checkout <sha>
uv sync
systemctl --user restart hal-mary
curl -fsS http://127.0.0.1:8080/healthz
```

Deliberately by hand rather than a `rollback.sh`. A rollback happens when something is already
wrong, and a script that ran migrations backwards would be the second thing wrong.

**Migrations are forward-only.** Rolling the code back does not roll the schema back, so a rollback
across a migration only works because the schema changes have all been additive. They have been so
far; check the new migration before assuming it for the next one.

The checkout is on a detached HEAD afterwards. `git checkout main` once the real fix is in.

---

## 5. Backups, and restoring from one

A timer runs `hal-mary backup` nightly at 04:17 UTC, with up to ten minutes of jitter, keeping the
newest **14** snapshots — `backup.keep` in `config.toml`.

```bash
systemctl --user list-timers hal-mary-backup.timer
ls -lh ~/hal-mary-data/backups/
journalctl --user -u hal-mary-backup --since "2 days ago"
systemctl --user start hal-mary-backup.service        # take one right now
```

`Persistent=true` on the timer means a night the box was switched off is caught up when it comes
back rather than silently skipped.

**Why this is not `cp`.** The database runs in WAL mode and the service writes to it while the
backup runs. In WAL mode a committed row can live entirely in `hal.db-wal` with nothing of it in
`hal.db`, so a copy of the main file alone opens cleanly, passes an integrity check, and has
silently lost the newest notes — which are the ones the season is made of. `hal-mary backup` uses
SQLite's online backup API and produces one self-contained file with no sidecars.

**What is actually at risk.** Everything else regenerates: ESPN state resyncs in seconds, the draft
board rebuilds from a job. The **notes** — the season's accumulated research, written by every job
and read into every prompt — exist nowhere else.

### Restoring

```bash
systemctl --user stop hal-mary                                 # 1. stop the writers
cd ~/hal-mary-data
mv hal.db hal.db.broken && rm -f hal.db-wal hal.db-shm         # 2. the bad one aside, and its
                                                               #    sidecars with it
cp backups/hal-20261103T041700Z.db hal.db                      # 3. the snapshot you want
sqlite3 hal.db "PRAGMA integrity_check; SELECT count(*) FROM notes;"
systemctl --user start hal-mary                                # 4.
```

Step 2 matters: leaving a stale `hal.db-wal` beside a restored `hal.db` is how a restore produces a
database that is neither the backup nor the original.

### Copying a backup off the box

Nothing does this automatically today. The snapshots live on the same VM as the database they
insure, which covers "the database got corrupted" and not "the VM died". If that matters, add a
periodic `rsync` **pulled from another machine** — and note that `/var/data` is fine as a *destination*
for copies. It is only running the live database from there that corrupts.

---

## 6. What is and is not running

`hal-mary serve` is the whole service. It runs:

- the **web app** — status, team, league, and the draft page;
- the **draft loop**, on its own thread, when a draft is live. If it cannot start — no ESPN
  credentials, for instance — that is logged as a warning and the app serves anyway, because picks
  can be entered by hand and a page that renders beats a page that does not.

**There is no scheduler yet.** Nothing recurring runs on its own: no nightly news sweep, no Tuesday
waiver scan. The in-season jobs exist as prompts and as `hal-mary job <name>`, and are run by hand:

```bash
cd ~/hal-mary
uv run hal-mary job board_build     # the day before a draft; takes minutes
uv run hal-mary sync
```

When the scheduler lands it runs inside the same `serve` process, and this unit does not change.

### The MCP endpoint and a Cloudflare tunnel

Not built yet. When it lands it adds an `MCP_TOKEN` to `.env`, and it is the one surface that would
be exposed through a Cloudflare tunnel so that Claude elsewhere can reach hal-mary's memory.

**The dashboard stays LAN-only.** It is protected by a single shared password and it renders live
ESPN session state; it does not go on the public internet. If the MCP endpoint is tunnelled, the
tunnel maps the MCP path *only*, and `MCP_TOKEN` is what authenticates it.

Configuring that tunnel is a human job — `cloudflared` on the box plus a DNS route — and nothing in
this repo does it or should.

---

## 7. Command reference

Run from `~/hal-mary` on the box.

```bash
uv run hal-mary doctor        # can this box run hal-mary? nonzero on a fatal problem
uv run hal-mary espn-check    # are the ESPN cookies still good? nonzero when they are not
uv run hal-mary sync          # pull league state and draft picks from ESPN
uv run hal-mary migrate       # apply pending schema migrations
uv run hal-mary backup        # snapshot the database now, and prune old ones
uv run hal-mary job <name>    # run one job on demand (board_build)
uv run hal-mary serve         # what the service runs
```

`hal-mary doctor` is the fastest answer to "is this box set up correctly": it checks the `.env`
keys, `claude` on `PATH` and logged in, the prompts and memory directories, and whether the database
directory is writable and on local disk rather than NFS. It touches no network and spawns nothing,
so it is safe to run at any time, including during a draft.

## Status

Under active construction against a draft deadline. This repository is entirely AI-authored.
