# Demo harness

This directory produces the README's screenshots and recordings from synthetic
account stores. Every capture is the real `qswap` rendering a made-up state:
invented accounts (`maya@example.com`, `Northwind Labs`), invented usage, a
fixed clock. Nothing here reads a real login, and the harness refuses to run
if it cannot prove that.

The harness runs on macOS and Linux. Its sandbox guards are POSIX-only, so it does not run on Windows.

Run everything from the repository root with the dev dependencies installed
(`uv sync`).

## Quick start

    uv run python scripts/demo/run.py fleet -- list
    uv run python scripts/demo/run.py usage -- usage --by model
    uv run python scripts/demo/run.py fleet --serve            # the dashboard at http://127.0.0.1:8765/
    uv run python scripts/demo/run.py fleet --menubar          # the menu bar's menu, as an outline
    uv run python scripts/demo/build.py                        # regenerate all shots

`run.py <scenario> --menubar` prints the macOS menu bar's menu for the
scenario as an indented outline: the menu bar cannot be drawn off macOS or
inside the sandbox, but every row it shows comes from one pure function
(`ai_quota_swap.menubar.build_menu`), which the outline prints after the same
reads and ledger scan the app makes.

`run.py <scenario> -- <qswap arguments>` runs the real CLI entry point
(`ai_quota_swap.cli.main`) against a scenario. Separate several commands with
`++` to run them one after another on the same state, for example
`run.py reset -- reset ++ reset 1 --yes`. Add `--html <file>` to render the
output as a terminal-window page, `--theme light` for the light palette, and
`--width` to set the terminal's column count. Whether Claude Code and Codex
are installed is the harness's choice, not this machine's: both are, unless
`--without-cli claude` (or `codex`) says otherwise, so output is the same on a
laptop with both CLIs and a CI runner with neither.

## Why it is safe

On macOS the tool reads credentials from the login Keychain, which a fake
`$HOME` does not hide. So the launcher does not rely on a fake home alone.
Before it imports anything that touches accounts it installs these guards
(`guards.py`), checks every one of them, and exits with status 3 if any check
fails. It never falls back to real behaviour.

1. A fresh temp home. `HOME`, `USERPROFILE`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
   `XDG_DATA_HOME` (and the other XDG variables and `TMPDIR`) point inside it.
   The rest of the inherited environment is dropped, so no token or key rides
   along, and the working directory moves into the temp home.
2. A permanent audit hook. It refuses any read or write under the real account
   stores (`~/.claude*`, `~/.codex`, `~/.ai-quota-swap`, `~/.claude-swap-backup`,
   `~/.local/share/ai-quota-swap`, the Keychain directories), any write outside
   the temp home and the declared output files, every process spawn (so
   `/usr/bin/security` can never run), loading the Security framework, and
   every connection or DNS lookup that leaves the machine. Python raises audit
   events before the operation, so a refused open never reaches the disk.
3. A socket-level block on outbound connections, loopback allowed, as a second
   layer under the hook (the same idea as the test suite's network block).
4. `ai_quota_swap.macos_keychain` replaced by an in-memory store.
5. `urllib.request.urlopen`, the one function every usage, profile, token and
   reset request in the package goes through, replaced by the canned API in
   `fakeapi.py`. The package still builds its real requests and parses the
   answers with its real code; only the transport is fake. It answers
   Anthropic's endpoints, Codex's `chatgpt.com/backend-api/wham` usage and
   reset-credit endpoints, and PyPI's project page (404, as today, since the
   package is not on PyPI). A request the fake has no answer for counts as a
   violation.

The self-test in `guards.verify` raises synthetic audit events at the hook
(a real-store read, an outside write, a `security` spawn, a connection, a DNS
lookup), so checking the guards never touches a real file or socket. A run that
crossed the sandbox anyway (for example a connection the code swallowed as
"offline") exits with status 4 and lists what happened.

`tests/test_demo_harness.py` holds the evidence: a guarded run of `list`,
`reset`, `usage` and `status` with the audit log on opens no file outside its
temp home, the repository and the interpreter's own install, writes nothing
outside its temp home, spawns nothing and makes no network call; each guard
refuses what it should; and the launcher refuses to start when a guard is off.

The demo processes patch these in their own memory only, and nothing on
disk: `cli._use_native_tls` (it would load the Security framework, which the
sandbox refuses), `poll_policy.JITTER_FRAC` (pinned to zero as the test suite
pins it), `AutoSwitchEngine._next_delay` (time compression, below), and the
four functions in `ai_quota_swap.codex.processes` that reach processes
(listing Codex processes with `ps`, finding and restarting Codex's background
server, running `codex login`). The sandbox refuses those spawns, so the demo
replaces them as `tests/conftest.py` does: no Codex process is running, no
background server exists, and `codex login` writes the scenario's
`codex_login` record into the temporary home it is handed (a scenario without
one answers "codex is not on PATH").

## Scenarios

`scenarios.py` holds them as data. Each has a fixed clock (2026-11-10 14:20
UTC), accounts with plans, usage windows, usage-reset grants, aliases and
disabled flags, settings, and optionally a session history.

- `fleet`: four Claude accounts on Max 20x, Team, Max 5x and Pro (one near its
  limit, two holding usage resets, one disabled), an API-key account, and two
  Codex accounts (the live one at a moderate weekly figure, a saved workspace
  account holding a reset credit). Every account was read two minutes before
  the clock, so `auto --once` compares real readings.
- `autoswitch`: the active account climbs from 86% to past its 90% line and
  the engine switches to the account with the most headroom.
- `reset`: the active account is at 100% on its Fable weekly window and holds
  a Fable reset and a weekly reset; `reset-after` is the same state once the
  Fable reset is spent.
- `usage`: the `fleet` accounts plus two weeks of Claude Code and Codex
  sessions across six models and five projects, with both providers'
  activation logs, for `qswap usage` and its `--by` views.
- `showcase`: the README's hero. The `fleet` accounts, with the API-key
  account on a $40 monthly budget, and two weeks of sessions in which that
  key was the active login on weekday mornings and afternoons. The history is
  read into the ledger while seeding (as an earlier `qswap usage` would have),
  so `list` shows the key's spend against its budget and the dashboard's cost
  line and usage screen have numbers.
- `empty`, `empty-claude`, `empty-claude-codex`: the first run. Nothing is
  managed and there is no `settings.json`; the variants add a live Claude Code
  login, then a live Codex login as well.
- `trouble`: failure states. Claude: the active account at 100% with no
  resets, a dead saved login (expired token, refused refresh), a usage
  endpoint answering 503, an account quarantined by the auto-switch engine,
  and a reading three hours old whose refreshes time out. Codex: the live
  account at 100% weekly, a saved one whose token expired.

`seed.py` writes a scenario into the temp home with the switchers' own
writers, the way the test suite's `EngineHarness` does, so the store on disk
is what a real `qswap add` (or `qswap codex add`) leaves. To add a scenario,
build a `Scenario` and register it in `SCENARIOS`. To add a provider, give it
an account type with a `provider` field, a seeder in `seed.SEEDERS`, and
routes in the canned API; scenario accounts are a flat list, so a mixed fleet
is one tuple.

The auto-switch engine sleeps a minute between ticks. Demo runs compress that:
each sleep moves the scenario clock forward instead of waiting (`AutoRun` sets
the factor, 20x by default), so the log keeps true timestamps and a recording
shows several ticks in seconds.

## Shots and pages

`shots.py` lists the shots. CLI shots render to `docs/shots/<name>.html` and
`<name>-light.html`: a terminal window as wide as the shot's column count (80
for most) at one font size, the tool's own colours, no external fonts or
scripts. TUI shots come from the dashboard served by textual-serve on
127.0.0.1 (`run.py <scenario> --serve`), in a page that matches the CLI windows
(`web/app_index.html`); each is sized in cells (columns x rows, fitted the way
`tour.py` fits them) and may play keys from the first frame (`u` for the usage
screen, `g` for the auto view). Each browser session starts its own fully
guarded app process. Their PNGs are also wrapped in static pages
(`docs/shots/dashboard.html`, `watch.html`, ...) so the whole set can be
published from `docs/shots/` with `index.html` as the entry.

The README and the docs use the PNGs in `assets/screenshots/raw/`. The README
links them by absolute `raw.githubusercontent.com` URL, so they render on PyPI
too; after regenerating, check that every name the README and `docs/` use
still exists.

## Capturing

`capture.py` does each step; `build.py` runs them in order.

- `capture.py html` renders the CLI pages.
- `capture.py shots` takes raw screenshots with a local headless Chrome at 2x
  into `assets/screenshots/raw/` and compresses them with pngquant when it is
  installed. Set `CHROME_PATH` if Chrome is not at
  `/Applications/Google Chrome.app`.
- `capture.py frame` frames each page with the ScreenFramed CLI into
  `assets/screenshots/`. ScreenFramed renders the URL in its cloud, so it needs
  `docs/shots/` published at a public URL (`--base-url` or
  `SCREENFRAMED_BASE_URL`) and a login (`npx @screenframed/cli login`, which
  writes `~/.screenframed.json`, or `SCREENFRAMED_API_KEY`). Without both it
  prints what is missing and does nothing. `--background` and `--device` pick
  the frame (defaults: `midnight`, no device).
- `capture.py record [autoswitch reset]` records the dashboard storyboards in
  `stories.py` with LittleDemo's local recorder (`npx @littledemo/cli record`),
  which drives a local Chrome against the loopback server, then writes a GIF
  (960 px, under 1.5 MB) to `assets/demos/` with ffmpeg. The MP4 stays local.
  `--mode local` skips LittleDemo and grabs frames through the DevTools
  protocol instead. LittleDemo's recorder runs the page at a third of real
  speed and speeds the video up, so the harness slows the server-side clock
  by the same factor to keep the storyboard's pacing.
- `capture.py upload [names]` hosts recordings already in `assets/demos/` on
  LittleDemo (it creates an `ai-quota-swap` project the first time) and records
  the share, embed, GIF and thumbnail URLs in `assets/demos/recordings.json`.
  Recording and hosting are separate so nothing is published unseen.
- `capture.py index` writes `docs/shots/index.html`.

`build.py` runs html, shots, frame and index; add `--record local` or
`--record littledemo --upload` to remake the recordings too, and `--only` to
limit it to some shots.

## Review tour

`tour.py OUT_DIR` screenshots every dashboard screen and modal it knows
(`tour.py --list` prints them: main and the cost line, watch, the auto view
for each provider, dry run and live, the usage screen, the switch picker and
its re-login refusal, the add, disable and remove menus and their
confirmations, the Codex login, the theme menu, the first-run and failure
states) in dark and light, at exact terminal sizes in cells (`--sizes
100x30,80x24`, the default; a stop may name its own, as the wide auto view
does). It is a review aid, not part of `build.py`: each stop is a scenario
plus a key sequence from the dashboard's first frame, every stop gets a fresh
guarded app process, and files are named `<stop>--<theme>--<cols>x<rows>.png`.
`--only` and `--skip-existing` limit a run. Pages act as focused (the
dashboard redraws its footer keys only while its terminal is focused).

## Keys in recordings

The storyboards type into the TUI through xterm.js. The served page keeps
xterm's input textarea focused, and the LittleDemo steps insert plain text
(no selector, since the recorder would try to click the off-screen textarea
first). The recording page hides LittleDemo's pointer overlay, as there is
nothing to point at.
