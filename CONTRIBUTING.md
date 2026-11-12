# Contributing

This page takes you from a fresh clone to a pull request. You need [uv](https://docs.astral.sh/uv/) and git; uv fetches the Python versions it needs.

## Set up

```bash
git clone https://github.com/seangeng/ai-quota-swap.git
cd ai-quota-swap
uv sync
```

`uv sync` creates `.venv` with the Python that `.python-version` names (3.14) and installs the project with its development tools.

## Run the tool safely

Do not point a development build at your own accounts. `uv run qswap` uses your real home: it reads and rewrites your saved logins and the macOS Keychain, refreshes saved tokens, can spend a usage reset (which cannot be undone), and runs its data migrations on your data directory. A bug in your branch does all of that to real accounts.

Use the demo harness instead. It runs the real CLI and dashboard against invented accounts in a temporary home, with the Keychain, the network and process spawns blocked, and it refuses to start if it cannot prove those guards are in place:

```bash
uv run python scripts/demo/run.py fleet -- list
uv run python scripts/demo/run.py fleet -- switch 3 ++ list    # ++ runs several commands on one state
uv run python scripts/demo/run.py usage -- usage --by model
uv run python scripts/demo/run.py fleet --serve                # the dashboard at http://127.0.0.1:8765/
```

The scenarios are data in `scripts/demo/scenarios.py`:

- `fleet`: four Claude accounts on different plans, an API-key account and two Codex accounts.
- `autoswitch`: the active account climbs past its line and the engine switches.
- `reset` and `reset-after`: an account at its Fable limit holding a Fable reset and a weekly reset, before and after spending the Fable one.
- `usage`: the fleet plus two weeks of sessions, for `qswap usage`.
- `showcase`: the README's hero: the fleet with a budget on the API-key account and two weeks of sessions already in the ledger.
- `last-resort`: every subscription at its limit and auto-switch allowed to use the API-key account, which has a budget: `auto --once` moves to the key.
- `empty`, `empty-claude`, `empty-claude-codex`: first runs.
- `quickstart`: `#1 personal` saved and Claude Code logged in to a second account, for the quick start recording (`qswap add --alias work`, then `qswap switch 1`).
- `trouble`: failure states, such as a dead login, a failing endpoint and a stale reading.

The harness runs on macOS and Linux. [scripts/demo/README.md](scripts/demo/README.md) explains the guards and how to add a scenario.

## Run the tests

```bash
uv run pytest
```

The suite runs in parallel, one worker per core. CI also runs it on Python 3.12, the lowest supported version. Run that before you push:

```bash
uv run --isolated --python 3.12 pytest
```

`--isolated` builds a throwaway environment from the lockfile, so `.venv` stays on 3.14. Without it, `uv run --python 3.12` replaces `.venv` with a 3.12 one, and the next plain `uv run` replaces it again.

If a test asserts something that only holds on POSIX (file modes, `flock`, `/proc`), keep the assertion there and give Windows an equivalent check where one exists. Otherwise skip it on Windows with a reason that says what does not apply.

## What the test suite guarantees

The suite never touches your real accounts or the network. `tests/conftest.py` sets this up for every test, without any action on your part:

- `HOME`, `USERPROFILE` and `CODEX_HOME` point at temporary directories.
- An audit hook refuses writes to your real account stores (Claude Code's login and config files, Codex's `auth.json`, the qswap data directory), even from a thread that outlives its test.
- `ai_quota_swap.macos_keychain` is replaced with an in-memory store.
- Outbound connections fail, and a test that attempts one fails, even when the code under test swallows the error. Loopback stays open.
- Codex processes are faked: no test sees your running Codex or runs `codex`.
- A fake home does not isolate launchd, which keeps one set of services per user: a `launchctl bootout` from a test would stop your real menu bar. So no test reaches launchctl: `ai_quota_swap.launch_agent._launchctl` and doctor's `launchctl print` answer "not loaded", a call that would change launchd's state fails the test, and an audit hook refuses to start `launchctl` at all.

These guards live in the pytest process. A child process the suite starts (`python -m ai_quota_swap ...`, as `tests/test_cli.py` runs it) inherits the temporary `HOME` and `CODEX_HOME`, and finds the suite's stand-in `launchctl` first on `PATH`. The stand-in answers a read "not loaded" and refuses any other call, and the test that started the child fails on a refusal. Nothing else in the list reaches a child:

- It has the real Keychain, the network and the real `/Applications`, where `qswap menubar --uninstall-service` and `qswap purge` delete the menu bar's app.
- Doctor runs `/bin/launchctl print` by its full path, so in a child it reads your real launchd. It only reads.
- A child given a `PATH` of its own (`os.defpath`, say) finds the real `launchctl`.

So a child runs qswap in an empty home of its own (`_subprocess_env` in `tests/test_cli.py`), and never installs, removes or starts the menu bar service.

A test that needs HTTP fakes the transport. Patch `urlopen` in the module under test, as `tests/test_codex_usage.py` does with `patch.object(codex_usage.urllib.request, "urlopen", fake)`, or patch the fetch function one level up (`ai_quota_swap.oauth.try_fetch_usage_for_account`, `ai_quota_swap.update_check.check_for_update`). `tests/admin_fakes.py` has a recording transport for the Admin API. `@pytest.mark.allow_network` lifts the block, but a test that needs the internet will fail in CI sooner or later, so treat it as a last resort.

A test that needs the Keychain asks for the `block_real_keychain` fixture by name. It returns the in-memory store, so the test can seed items and inspect what was written (`tests/test_api_key_accounts.py` has examples). Tests that drive the real `security` CLI are marked `no_keychain_fake`, use the `tmp_keychain` fixture in `tests/test_macos_keychain_contract.py`, and run only on GitHub Actions macOS, against a throwaway keychain.

## The live switch, end to end

The suite fakes Claude Code, so it cannot show that a real Claude Code follows the key line a switch to an API key writes. That is checked by hand: qswap's own switch, run against the real `claude` binary and the real API, in a throwaway home, with a made-up key. It was last run on Claude Code 2.1.322; what it found is in [docs/design/api-key-sessions.md](docs/design/api-key-sessions.md#part-2-use-a-key-without-logging-anyone-out). Run it again when a change touches the line, the helper or the switch, or when Claude Code changes how it reads settings.

What it shows, with print-mode sessions (`claude -p`) and the throwaway home's user settings as the only settings layer:

- a session started while qswap's line is in sends the key the line prints;
- one long-lived process (`--input-format stream-json`) follows `qswap switch 5` and the switch back, about one second after each write;
- `qswap run 5` gives that one process the key and writes no settings;
- a line for an account that cannot give a key fails new sessions with "Your apiKeyHelper script is failing", while a session already holding the key keeps sending it;
- `qswap key-helper` prints exactly the key, or one line on standard error.

It does not cover an interactive session, a claude.ai login underneath (the throwaway home has none), Remote Control or claude.ai connectors.

How it stays away from your accounts:

- `HOME` and `CLAUDE_CONFIG_DIR` point into a fresh `mktemp -d` directory, removed afterwards. Every process starts from `env -i` with a short list of variables: no `ANTHROPIC_*` variable and no `CLAUDE_CODE_OAUTH_TOKEN`.
- The key is made up (`sk-ant-api03-E2E-FAKE-KEY-...`). The API answers every request with a 401, so nothing is billed. Claude Code retries a rejected key for about four minutes per prompt, so allow five minutes for each check that sends one.
- qswap runs in-process, under a driver that replaces the Keychain with an in-memory store and, like the demo harness's guards, refuses opens of the real account stores (`~/.claude*`, `~/.codex`, `~/.ai-quota-swap`, the Keychain directory), writes outside the throwaway directory, spawns of `security` and `launchctl`, and any connection qswap tries other than loopback.
- `claude` is started by absolute path, only by the check itself.

## Lint

```bash
uv run ruff check
```

CI runs the same command. The rule selection, and the rules left out until the code passes them, are in `pyproject.toml`. Formatting is not enforced, so do not run `ruff format` over the tree: it would rewrite most files and bury your change.

## Where things are

[docs/architecture.md](docs/architecture.md) maps the modules and says where to look for the most common first changes. The decisions behind the larger features are in [docs/design/](docs/design/).

## Propose a change

1. Branch from `main` and open a pull request against it. The template asks what changed, how you tested it, and for screenshots when output changes.
2. CI must be green: the suite on Linux (Python 3.12 and 3.14), macOS and Windows, and the lint job.
3. Anything that changes behaviour gets a design doc in `docs/design/`, or an update to an existing one, before or with the code. The existing ones show the format.
4. JSON output is a contract that only grows. Add fields; never rename, remove or retype one. Document new fields in [docs/json-output.md](docs/json-output.md).
5. Text output follows [docs/design/ux.md](docs/design/ux.md): one word per thing, 80 columns, the next command to run on every dead end.
6. Update the user docs your change affects, and add a line under `## Unreleased` at the top of [CHANGELOG.md](CHANGELOG.md), starting that section if the last release left none. A new setting also needs its meaning and an example value in `scripts/settings_doc.py`; run it to regenerate [docs/settings.md](docs/settings.md). `tests/test_docs.py` fails while the page is stale or a link in any Markdown file is broken, and `tests/test_docs_examples.py` while a page quotes an API-key message the code no longer prints.

## Regenerate screenshots

The screenshots and recordings in the README and `docs/` come from the demo harness:

```bash
uv run python scripts/demo/build.py                  # every shot
uv run python scripts/demo/build.py --only list      # one shot
uv run python scripts/demo/tour.py /tmp/qswap-tour   # every dashboard screen, for review
```

Shots need a local Chrome (`CHROME_PATH` if it is not in `/Applications`), recordings need ffmpeg (and a LittleDemo login, except with `--record local`), and framing needs a ScreenFramed login, which `build.py` skips with a message when it is missing. The README and the docs use the unframed PNGs in `assets/screenshots/raw/`. If your change alters what a shot shows, regenerate that shot in the same pull request. The steps are in [scripts/demo/README.md](scripts/demo/README.md).

## Releases (maintainer)

A release is published to PyPI by `.github/workflows/publish.yml` through trusted publishing. 0.1.0 was the first. For each release:

1. Set the version in `pyproject.toml`, then run `uv lock` and commit `uv.lock` with it: the lock file carries the version too, and CI's `uv sync --locked` fails while the two differ. Rename `## Unreleased` in `CHANGELOG.md` to that version, with nothing above it but the title.
2. Build the package and install the wheel into a scratch environment. `qswap --version` should print the new version with no commit in brackets, since the wheel is not a git install:

   ```bash
   scratch=$(mktemp -d)
   uv build --out-dir "$scratch/dist"
   uv venv "$scratch/venv"
   uv pip install --python "$scratch/venv/bin/python" "$scratch"/dist/*.whl
   mkdir "$scratch/home"
   HOME="$scratch/home" "$scratch/venv/bin/qswap" --version
   ```

3. Merge, then publish a GitHub release whose tag is the version (`v0.1.0` for `0.1.0`). The workflow refuses a tag that does not match the version, and a tree whose `CHANGELOG.md` has no section for it. Name the repository in every `gh` command of the release, for example `gh release create v0.1.0 --target main -R seangeng/ai-quota-swap`: in a clone that also has the original project as a remote named `upstream` and no default repository set, `gh` run from a script picks that one, and at a terminal it stops and asks for `gh repo set-default`.
4. Watch the run (`gh run watch -R seangeng/ai-quota-swap`), then check that `https://pypi.org/pypi/ai-quota-swap/json` answers with the new version.

The run has two jobs: `build` checks the tag and builds the package, and `publish` uploads it. If either fails before anything was uploaded, re-run the failed job from the Actions page when the tree was not at fault; when it needs a change, delete the release and its tag, fix main, and create the release again. A version that did upload can never be replaced: publish the next patch version, then yank the bad one on PyPI.
