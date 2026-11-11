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
    uv run python scripts/demo/run.py showcase --menubar --html menu.html   # ... and as a picture
    uv run python scripts/demo/build.py                        # regenerate all shots
    uv run python scripts/demo/capture.py record               # remake all recordings

`run.py <scenario> --menubar` prints the macOS menu bar's menu for the
scenario as an indented outline. Every row the menu bar shows comes from one
pure function (`ai_quota_swap.menubar.build_menu`), which the outline prints
after the same reads and ledger scan the app makes. Commands after `--` run
first, so `run.py fleet --menubar -- switch 5` prints the menu with an API key
active.

`run.py <scenario> -- <qswap arguments>` runs the real CLI entry point
(`ai_quota_swap.cli.main`) against a scenario. Separate several commands with
`++` to run them one after another on the same state, for example
`run.py reset -- reset ++ reset 1 --yes`. Add `--html <file>` to render the
output as a terminal-window page, `--theme light` for the light palette, and
`--width` to set the terminal's column count. With `--typing` as well, the
page types each command and then shows its output, one after another, on a
screen of `--height` lines (`--zoom` scales it); the quick start recording
plays that page. Whether Claude Code and Codex
are installed is the harness's choice, not this machine's: both are, unless
`--without-cli claude` (or `codex`) says otherwise, so output is the same on a
laptop with both CLIs and a CI runner with neither.

## The menu bar picture

Add `--html FILE` to `--menubar` and the harness also draws that menu: the
right-hand end of a macOS menu bar with qswap's title, and the menu open
under it. The menu bar pictures are drawings of the real menu's rows, not
photographs of the app, which needs macOS and rumps and never runs in the
sandbox. `menubar_page.py` draws each row the way `menubar_app` turns it into
a native item: a row with nothing to click is greyed (the section headings
are such rows), a checked row has a check mark, a row that needs you is
orange, and a submenu row ends in a chevron. Other apps' status icons are
neutral placeholders, and the clock shows the scenario's time. A status item
near the right of the bar has its menu pushed left to stay on screen, so
submenus open to the left, as they do on a Mac. *Start at login* is drawn
checked, the state the first `qswap menubar` leaves: the app reads it from
launchd, which the sandbox never reaches.

The cost row is drawn as the app's seven-day chart, in inline SVG: the bars
are placed by the same function the app's AppKit view uses
(`menubar_chart.layout_columns`), in the system colours' light and dark
values. A page has no script to measure text, so the chart's width and its
legend's cut names come from a table of the system font's advances, measured
in the harness's headless Chrome. `--no-usage-chart` draws the plain sentence
instead.

A drawing can also carry a notification banner, top right under the bar,
as macOS shows one. Its title and body come from the app's own
`menubar_notify.Notifier`, fed the events the hosted engines emitted; the
sender's icon is a neutral placeholder.

| Option | Does |
|---|---|
| `--theme light` | draws the light appearance; dark is the default |
| `--open "Use a reset"` | opens that submenu beside the menu and selects its row. `"Settings > Use resets automatically"` opens one inside another. A title the menu does not have stops the run with exit status 2 and lists the submenus it has |
| `--highlight TEXT` | puts the row of the innermost open menu that starts with TEXT under the pointer; a greyed row is refused |
| `--closed` | draws the bar with the menu shut, at the size of the open picture |
| `--beside PNG` | puts a 2x screenshot on the desktop as a window behind the menu; the hero uses the dashboard |
| `--size WxH` | fixes the picture's size in CSS px, so frames line up |
| `--no-usage-chart` | turns *Show usage chart* off, so the cost row is the plain sentence |
| `--auto-switch [claude\|codex\|both]` | turns the menu bar's auto-switch on and runs its engines (below); `both` when no value is given |
| `--frames DIR` | with `--auto-switch`: one picture after each of Claude Code's engine checks |
| `--ticks N` | how many checks `--frames` draws (default 4) |

The page is plain HTML and CSS, with no font, script or image from anywhere
else (the window behind the hero is embedded in the page).

`menubar_chart.py` draws the chart alone with the app's own AppKit view, off
screen into PNGs (light and dark, at 2x), from invented shapes or from a
scenario read in the sandbox: `uv run --with pyobjc-framework-Cocoa python
scripts/demo/menubar_chart.py --out DIR` (`--list` names the shapes). It
needs macOS and makes no window, menu or application object.

Without `--auto-switch` the menu's first row reads `Auto-switch off`, as it
does on a Mac where nobody turned it on. With it, the harness turns the
Auto-switch toggles on in the temporary `menubar_settings.json` and hosts the
engines as the app does: an `EngineSupervisor` starts an `AutoSwitchEngine`
for each provider in a thread of its own, with the `qswap menubar` host label,
against the canned API and inside the same guards. The menu is drawn after
each engine's first check, so `Auto-switch running for Claude Code and Codex`
is what a running engine shows. Each check then waits until the picture of
its result is drawn.

For the menu bar recording, `--frames DIR` writes three files after each check:
`frame-NN.html` (open, or as `--open` and `--closed` say), `frame-NN-closed.html`
(with the banner of the notification the app would post for that check, if
any) and `frame-NN.txt`, the outline, followed by a `notification:` line for
each notification. On a scenario with time compression the checks come one
scenario minute apart, a few real seconds each:

    uv run python scripts/demo/run.py autoswitch --menubar --auto-switch \
        --frames frames --open "Switch history"

The titles go `⇄ personal · 86% · 38%`, `⇄ personal · 88% · 38%`, then
`⇄ work · 12% · 21%` once the engine switches at 91%. That check's closed
frame carries the notification `Switched #1 personal → #2 work · 5h reached
91%`, and from then on Switch history lists
`#1 personal → #2 work · 2026-11-10 14:22`. Screenshot each
page's `#stage` element at 2x, as `capture.py` does, to get the frames.

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
pins it), `AutoSwitchEngine._next_delay` (time compression, below), the
command the key line names (`installed.command`, pinned to a stand-in
`~/.local/bin/qswap` in the temp home that is never run), the check of that
line (`claude_settings._run_check`, which runs `qswap key-helper N --check`
in the demo process instead of through the shell), the machine-wide managed
settings file doctor reads (none), each menu bar engine's `tick` under
`--menubar --auto-switch` (wrapped on that engine object to wait for the
picture of its check), and the
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
- `last-resort`: the `showcase` fleet with every subscription at 100% of its
  5-hour window and no usage resets, and `autoswitch.includeApiKeyAccounts`
  on, so `auto --once` moves to the API key (which has its $40 budget and
  the showcase's spend) and `status`, `list` and `--menubar` after it show a
  key auto-switch entered.
- `empty`, `empty-claude`, `empty-claude-codex`: the first run. Nothing is
  managed and there is no `settings.json`; the variants add a live Claude Code
  login, then a live Codex login as well.
- `quickstart`: the quick start's second step. `#1 personal` is saved, and
  Claude Code is logged in to `maya@northwind.example.com`, which qswap does
  not manage yet (a scenario's `live` login with managed accounts is a
  `/login` since the last `qswap add`). `qswap add --alias work` saves it as
  `#2 work`, the active account, so the recording switches back with
  `qswap switch 1`.
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

Menu bar shots (`MENUBAR_SHOTS`) render to `docs/shots/<name>.html` with
`run.py --menubar --html` and are captured after the dashboard shots, because
`hero` and `hero-light` put `dashboard.png` and `dashboard-light.png` behind
the menu. `menubar-settings` (Settings open) and `menubar-auto-resets`
(Settings > Use resets automatically) are used only by `docs/menubar.md`, one
submenu level each so the menus stay readable at their natural size.
ScreenFramed does not frame these: they come with their own desktop.

The README and the docs use the PNGs in `assets/screenshots/raw/`. The README
links them by absolute `raw.githubusercontent.com` URL, so they render on PyPI
too; after regenerating, check that every name the README and `docs/` use
still exists.

## Capturing

`capture.py` does each step; `build.py` runs them in order.

- `capture.py html` renders the CLI pages and the menu bar pages (the heroes
  wait for `shots`, which renders them once the dashboard PNGs exist).
- `capture.py shots` takes raw screenshots with a local headless Chrome at 2x
  into `assets/screenshots/raw/`: CLI pages, then dashboard shots, then menu
  bar drawings. It compresses them with pngquant when it is installed. Set `CHROME_PATH` if Chrome is not at
  `/Applications/Google Chrome.app`.
- `capture.py frame` frames each page with the ScreenFramed CLI into
  `assets/screenshots/`. ScreenFramed renders the URL in its cloud, so it needs
  `docs/shots/` published at a public URL (`--base-url` or
  `SCREENFRAMED_BASE_URL`) and a login (`npx @screenframed/cli login`, which
  writes `~/.screenframed.json`, or `SCREENFRAMED_API_KEY`). Without both it
  prints what is missing and does nothing. `--background` and `--device` pick
  the frame (defaults: `midnight`, no device).
- `capture.py record [names]` makes the recordings in `stories.py` (all of
  them by default) with LittleDemo's local recorder
  (`npx @littledemo/cli record`), which drives a local Chrome against a
  loopback server, then writes a GIF to `assets/demos/` with ffmpeg. The MP4
  stays local. `--mode local` skips LittleDemo and grabs frames through the
  DevTools protocol instead. See Recordings, below.
- `capture.py upload [names]` hosts recordings already in `assets/demos/` on
  LittleDemo (it creates an `ai-quota-swap` project the first time) and records
  the share, embed, GIF and thumbnail URLs in `assets/demos/recordings.json`.
  Recording and hosting are separate so nothing is published unseen; a new
  recording drops the record of a copy hosted earlier, which shows the old
  one.
- `capture.py index` writes `docs/shots/index.html`.

`build.py` runs html, shots, frame and index; add `--record local` or
`--record littledemo --upload` to remake the recordings too, and `--only` to
limit it to some shots.

## Recordings

Each GIF in `assets/demos/` is 960 px wide at most, under 1.5 MB and about
13 to 17 seconds. `capture.py record NAME` remakes one; `recordings.json`
records how each was made.

| GIF | Scenario | Shows | Storyboard |
|---|---|---|---|
| `menubar.gif` | `autoswitch` | The menu open on `#1 personal` at 86%; the bar magnified while the title's 5-hour figure climbs to 88%, then changes to `work` when the menu bar's own engine switches at 91%, with the app's notification (`Switched #1 personal → #2 work · 5h reached 91%`) under the bar; then the menu open on that check, on Switch history (`#1 personal → #2 work · 2026-11-10 14:22`) | a played page (`player_page.py`) |
| `quickstart.gif` | `quickstart` | `qswap add --alias work`, `qswap list`, `qswap switch 1`, `qswap status`, typed and answered with their real output | a played page (`run.py --typing`) |
| `dashboard.gif` | `showcase` | The dashboard's main view, then `w` (watch), `g` (the auto view, in dry run) and `u` (usage), each closed with `esc` | keys into the served dashboard, 100x40 cells |
| `autoswitch.gif` | `autoswitch` | The auto view goes live (the Go live dialog held long enough to read); `#1 personal` climbs from 86% and the engine switches to `#2 work` at 92% | keys into the served dashboard |
| `reset.gif` | `reset` | The auto view goes live, spends the Fable reset on `#1 personal` and Fable drops from 100% to 0% | keys into the served dashboard |

Two kinds of storyboard drive the recorder:

- Keys into the dashboard served by `run.py --serve` (`Story.steps`). A story
  can size the terminal in cells (`cells`, fitted as `tour.py` fits them) and
  cut the first moments of the video (`trim_ms`), while the dashboard still
  draws its footer. The dashboard's clock runs in real time while LittleDemo
  plays the page at a third of real speed, so a storyboard that ran much past
  a minute of real time would show readings aging into "as of 3m ago".
- A page the harness writes and plays (`Story.page`), saved as
  `docs/shots/recording-<name>.html`. It shows its first frame until a key
  press, then follows a timeline computed in Python and embedded in the page,
  so the same scenario state gives the same page. `typing` is
  `run.py <scenario> --typing` with the story's commands: the output is the
  commands' own, rendered as `render.py` renders the CLI shots, and only the
  typing is animated. It opens on its last screen (a good first frame) and
  ends there, so the GIF loops without a jump. `menubar` is `player_page.py` over the
  drawings `run.py --menubar --auto-switch --frames` makes: three guarded runs
  of the same deterministic scenario, one to find the widest drawing (Switch
  history open) and two to draw every frame at that size. Each shot is a
  whole `menubar_page` drawing, embedded unchanged (`<iframe srcdoc>`); the
  player adds cross-fades and can show a drawing magnified on the bar's
  right-hand end (one scale for every magnified shot, the largest that keeps
  each title and banner in view), and adds no row or title of its own.

The recorder adds time after each step: 500 ms after typed text and 1.3 s
after a pressed key (its key overlay, then its delay), which the storyboards
count. The stage page and the played pages hide LittleDemo's pointer and key
overlays. To make the GIF, ffmpeg crops a served dashboard to its window plus
8 px of the stage, scales to 960 px, drops frames that repeat the one before
(`mpdecimate` with strict thresholds, so every step of a fade is kept) and
holds the last frame to the video's end. Played pages are drawn about 1.5
times larger than the GIF and scaled down, which keeps text sharp through the
video's colour subsampling.

To check a recording, take one frame a second and look at each one:

    mkdir -p frames && ffmpeg -i assets/demos/menubar.gif -vf fps=1 frames/s%02d.png

Text should read at 960 px, each state should stay long enough to read, and
no frame should be blank or half drawn.

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
first). The recording page hides LittleDemo's pointer and key overlays, as
there is nothing to point at and the key overlay would show `esc` but not the
letters typed. A played page starts on any key, which the storyboard presses
once.
