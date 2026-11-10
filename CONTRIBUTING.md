# Contributing

## Testing

Install the project and its development dependencies with `uv sync`, then run the suite with `uv run pytest`. It runs in parallel by default.

The project supports Python 3.12 and later, and CI runs 3.12 on Linux, macOS and Windows. Run the lowest supported version as well before you push, since uv fetches it if you don't have it:

```bash
uv run --python 3.12 pytest
```

The suite never touches your real accounts or the network. Every test gets a temporary home and Codex home, the macOS Keychain is replaced with an in-memory fake, outbound connections are blocked, and a guard fails any test that tries to write to your real account store. The few tests that need a real Keychain run only on GitHub Actions macOS, against a throwaway keychain.

If a test asserts something that only holds on POSIX (file modes, `flock`, `/proc`), keep the assertion there and give Windows an equivalent check where one exists. Otherwise skip it on Windows with a reason that says what does not apply.
