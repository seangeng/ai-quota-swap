"""Token and cost ledger: ``qswap usage``.

Reads the session files Claude Code and Codex already write, counts each
response's tokens once into a small SQLite store under the data root, and
reports them by day, account, model, project, provider or session with an
estimated cost at API prices. No network calls; no message content is stored.

Kept import-light on purpose: the switcher imports ``ledger.activations``
on every switch, and that must not pull in SQLite.
"""
