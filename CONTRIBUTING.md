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
- `reset` and `reset-after`: an account at its Fable limit holding a Fable reset, before and after spending it.
- `usage`: the fleet plus two weeks of sessions, for `qswap usage`.
- `empty`, `empty-claude`, `empty-claude-codex`: first runs.
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

A test that needs HTTP fakes the transport. Patch `urlopen` in the module under test, as `tests/test_codex_usage.py` does with `patch.object(codex_usage.urllib.request, "urlopen", fake)`, or patch the fetch function one level up (`ai_quota_swap.oauth.try_fetch_usage_for_account`, `ai_quota_swap.update_check.check_for_update`). `tests/admin_fakes.py` has a recording transport for the Admin API. `@pytest.mark.allow_network` lifts the block, but a test that needs the internet will fail in CI sooner or later, so treat it as a last resort.

A test that needs the Keychain asks for the `block_real_keychain` fixture by name. It returns the in-memory store, so the test can seed items and inspect what was written (`tests/test_api_key_accounts.py` has examples). Tests that drive the real `security` CLI are marked `no_keychain_fake`, use the `tmp_keychain` fixture in `tests/test_macos_keychain_contract.py`, and run only on GitHub Actions macOS, against a throwaway keychain.

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
6. Update the user docs your change affects, and add a line to the Unreleased section of [CHANGELOG.md](CHANGELOG.md).

## Regenerate screenshots

The README's screenshots and recordings come from the demo harness:

```bash
uv run python scripts/demo/build.py                  # every shot
uv run python scripts/demo/build.py --only list      # one shot
uv run python scripts/demo/tour.py /tmp/qswap-tour   # every dashboard screen, for review
```

Shots need a local Chrome (`CHROME_PATH` if it is not in `/Applications`), recordings need ffmpeg, and framing needs a ScreenFramed login, which `build.py` skips with a message when it is missing. If your change alters what a shot shows, regenerate that shot in the same pull request. The steps are in [scripts/demo/README.md](scripts/demo/README.md).

## Releases (maintainer)

A release is published to PyPI by `.github/workflows/publish.yml` through trusted publishing. Set the version in `pyproject.toml`, move the Unreleased notes in `CHANGELOG.md` under that version, merge, then publish a GitHub release whose tag is the version (`v0.1.0` for `0.1.0`). The workflow refuses a tag that does not match.
