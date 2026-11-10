"""Hosting the auto-switch engines, and noticing switches made elsewhere.

No ``rumps`` here, so both are tested with fakes:

- :class:`EngineSupervisor` runs one :class:`~ai_quota_swap.autoswitch.
  AutoSwitchEngine` per provider, each in its own thread, built by a factory
  the app supplies (the engine over that provider's switcher, with that
  provider's settings). The app tells it which providers it wants
  (:func:`ai_quota_swap.menubar.wanted_engines`); it starts and stops engines
  to match, remembers why one failed, and reports a thread that ended on its
  own as an :class:`EngineStoppedEvent`.
- :class:`ActiveChangeWatcher` notices an active-account change made outside
  the menu bar (``qswap switch``, Claude Code's ``/login``, ``codex login``)
  with one cheap ``stat`` a second per provider, and reads the login only
  when its file changed.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

_logger = logging.getLogger("ai-quota-swap")


@dataclass(frozen=True)
class EngineStoppedEvent:
    """Queued by an engine's thread when its ``run_loop`` ends.

    Carries the engine object, not just a flag: a restart starts the
    replacement before the old thread has finished, so the old thread's exit
    must be matched against the engine hosted *now* before anything is
    cleared (see :func:`engine_exit_applies`). ``error`` is the exception that
    ended the loop, or None when it returned on its own.
    """

    kind: ClassVar[str] = "engine-stopped"
    engine: object
    error: str | None = None

    def human(self) -> str:
        return self.error or "the engine loop ended on its own"


def engine_exit_applies(current, stopped) -> bool:
    """Whether a stopped engine is the one the menu bar currently hosts.

    False for a predecessor that finished after a restart, and for an engine
    the app already let go of through its own stop.
    """
    return current is not None and current is stopped


#: ``factory(provider, emit)`` builds a provider's engine; ``emit(event)`` is
#: the callback the engine reports through. It may raise: that is a failed start.
EngineFactory = Callable[[str, Callable[[object], None]], object]


class EngineSupervisor:
    """One engine per provider, each in a daemon thread of its own.

    ``on_event(provider, event)`` receives every event of every engine, on
    the engine's thread (the app queues them for its main thread), plus an
    :class:`EngineStoppedEvent` when a thread ends. Methods are called from
    the main thread only.
    """

    def __init__(
        self,
        factory: EngineFactory,
        on_event: Callable[[str, object], None],
        *,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        self._factory = factory
        self._on_event = on_event
        self._thread_factory = thread_factory
        self._engines: dict[str, object] = {}
        self._failures: dict[str, str] = {}
        self._threads: list[threading.Thread] = []

    # -- state -----------------------------------------------------------------

    def running(self, provider: str) -> bool:
        return provider in self._engines

    def engine(self, provider: str) -> object | None:
        return self._engines.get(provider)

    def failure(self, provider: str) -> str | None:
        """Why the provider's engine is not running although wanted."""
        return self._failures.get(provider)

    def any_running(self) -> bool:
        return bool(self._engines)

    # -- control ----------------------------------------------------------------

    def start(self, provider: str) -> str | None:
        """Start the provider's engine unless it runs; the failure text when
        it could not be built (also kept for :meth:`failure`)."""
        if provider in self._engines:
            return None
        try:
            engine = self._factory(provider, lambda ev, p=provider: self._on_event(p, ev))
        except Exception as e:  # never let a bad start crash the menu bar
            from ai_quota_swap.menubar import error_text

            text = error_text(e)
            _logger.warning("%s auto-switch engine failed to start: %s", provider, text)
            self._failures[provider] = text
            return text
        self._engines[provider] = engine
        self._failures.pop(provider, None)
        thread = self._thread_factory(
            target=self._run, args=(provider, engine), daemon=True,
            name=f"qswap-{provider}-engine",
        )
        self._threads = [t for t in self._threads if t.is_alive()]
        self._threads.append(thread)
        thread.start()
        return None

    def _run(self, provider: str, engine) -> None:
        # Engine thread. Whatever ends the loop, tell the main thread which
        # engine ended; it decides whether that is still the hosted one.
        error = None
        try:
            engine.run_loop()
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            _logger.debug("%s auto-switch engine crashed", provider, exc_info=True)
        finally:
            self._on_event(provider, EngineStoppedEvent(engine, error))

    def stop(self, provider: str) -> None:
        """Stop the provider's engine (its thread ends after its current tick)
        and forget any failure: off is not stopped."""
        engine = self._engines.pop(provider, None)
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                _logger.debug("engine stop failed", exc_info=True)
        self._failures.pop(provider, None)

    def restart(self, provider: str) -> str | None:
        """Apply changed settings to a running engine by replacing it."""
        if provider not in self._engines:
            return None
        self.stop(provider)
        return self.start(provider)

    def retry_failed(self) -> dict[str, str | None]:
        """Start every engine that failed or stopped (the health row's click)."""
        out = {}
        for provider in list(self._failures):
            self._failures.pop(provider, None)
            out[provider] = self.start(provider)
        return out

    def apply(self, wanted: Mapping[str, bool]) -> dict[str, str]:
        """Start the engines wanted and not running, stop the ones not wanted.

        A provider whose engine failed stays failed (shown, and restarted by
        a click) instead of being retried on every refresh. Returns the
        failures of the starts it attempted.
        """
        failed: dict[str, str] = {}
        for provider, want in wanted.items():
            if want:
                if provider not in self._engines and provider not in self._failures:
                    text = self.start(provider)
                    if text:
                        failed[provider] = text
            else:
                self.stop(provider)
        return failed

    def stopped(self, provider: str, event: EngineStoppedEvent) -> bool:
        """Handle a thread that ended: True when it was the hosted engine,
        which is then remembered as stopped with its reason."""
        if not engine_exit_applies(self._engines.get(provider), event.engine):
            return False
        del self._engines[provider]
        self._failures[provider] = event.human()
        return True

    def stop_all(self, *, join_timeout: float | None = None) -> None:
        """Stop every engine; with ``join_timeout``, wait for their threads."""
        for provider in list(self._engines):
            self.stop(provider)
        self._failures.clear()
        if join_timeout is not None:
            self.join(join_timeout)

    def join(self, timeout: float) -> bool:
        """Wait for every engine thread to end; True when all have."""
        for thread in list(self._threads):
            thread.join(timeout)
        self._threads = [t for t in self._threads if t.is_alive()]
        return not self._threads


class ActiveChangeWatcher:
    """Notices which providers' active account changed outside the menu bar.

    ``paths[provider]()`` is the file that names the live login (Claude
    Code's config, Codex's ``auth.json``); ``read_active[provider]()`` reads
    the live login's email from it. Each :meth:`poll` stats every file and
    reads only those whose modification time moved (Claude Code rewrites its
    config often for unrelated reasons, so a changed file alone is not a
    switch); a provider is reported when the login now differs from the one
    the menu shows.
    """

    def __init__(
        self,
        paths: Mapping[str, Callable[[], Path | None]],
        read_active: Mapping[str, Callable[[], str | None]],
        *,
        stat: Callable[[Path], os.stat_result] = os.stat,
    ) -> None:
        self._paths = dict(paths)
        self._read = dict(read_active)
        self._stat = stat
        self._mtimes: dict[str, float] = {}

    def poll(self, shown: Mapping[str, str | None]) -> list[str]:
        """Providers whose live login is not the one ``shown`` (provider →
        the active account's email in the menu)."""
        changed: list[str] = []
        for provider, path_of in self._paths.items():
            try:
                path = path_of()
                if path is None:
                    continue
                mtime = self._stat(path).st_mtime
            except (OSError, ValueError):
                continue
            except Exception:
                _logger.debug("active-change stat failed", exc_info=True)
                continue
            if self._mtimes.get(provider) == mtime:
                continue
            self._mtimes[provider] = mtime
            try:
                active = self._read[provider]()
            except Exception:
                _logger.debug("active-change read failed", exc_info=True)
                continue
            if active and active != shown.get(provider):
                changed.append(provider)
        return changed


# -- spending one usage reset by hand ---------------------------------------------


def _start_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True, name="menubar-reset").start()


class ResetRunner:
    """Runs a confirmed *Use a reset* off the main thread.

    ``spend(switcher, number, grant_id, codex=…)`` is
    :func:`ai_quota_swap.manual_reset.spend`, the sequence ``qswap reset
    ACCOUNT --grant ID --yes`` runs (fresh re-read, pending record, claim,
    final record); tests pass a fake. ``report(notification)`` receives the
    result from the worker thread, in the auto log's words (``Used a Fable
    reset on #1 personal · …``, ``Did not use …: why``); the app queues it
    for the main thread. ``start(work)`` runs ``work`` (a daemon thread);
    ``lock`` serializes with the app's other slow actions.

    One claim per account at a time: while one is in flight, :meth:`busy`
    names the account (its rows read "in progress") and :meth:`run` refuses
    a second. Nothing the worker raises escapes it.
    """

    def __init__(
        self,
        spend: Callable[..., object],
        report: Callable[[object], None],
        *,
        start: Callable[[Callable[[], None]], None] | None = None,
        lock: threading.Lock | None = None,
    ) -> None:
        self._spend = spend
        self._report = report
        self._start = start or _start_thread
        self._lock = lock
        self._busy: set[tuple[str, str]] = set()
        self._guard = threading.Lock()

    def busy(self) -> frozenset[tuple[str, str]]:
        """``(provider, number)`` of every account with a claim in flight."""
        with self._guard:
            return frozenset(self._busy)

    def run(self, switcher, provider: str, number: str, grant_id: str, *, who: str, what: str) -> bool:
        """Spend ``grant_id`` on account ``number`` in the background. False
        (and nothing started) while that account's last claim is in flight."""
        key = (provider, str(number))
        with self._guard:
            if key in self._busy:
                return False
            self._busy.add(key)

        def work() -> None:
            text = self._outcome(switcher, provider, str(number), grant_id, who=who, what=what)
            with self._guard:
                self._busy.discard(key)
            try:
                from ai_quota_swap.menubar_notify import Notification

                self._report(Notification(text))
            except Exception:
                _logger.error("menu bar: could not report a usage reset's result", exc_info=True)

        try:
            self._start(work)
        except Exception:
            with self._guard:
                self._busy.discard(key)
            raise
        return True

    def _outcome(self, switcher, provider: str, number: str, grant_id: str, *, who: str, what: str) -> str:
        """The spend, and its result as one sentence. Never raises."""
        from ai_quota_swap import manual_reset
        from ai_quota_swap.exceptions import ProviderError

        try:
            if self._lock is not None:
                with self._lock:
                    spent = self._spend(switcher, number, grant_id, codex=provider == "codex")
            else:
                spent = self._spend(switcher, number, grant_id, codex=provider == "codex")
            if spent is None:
                return manual_reset.not_used_text(what, who, "kept")
            return manual_reset.outcome_text(spent, who)
        except ProviderError as e:
            # The command line's refusal ("that reset cannot be used right
            # now (…)", "… needs re-login; nothing was spent."), on one line.
            message = " ".join(str(e).split())
            names = [who]
            head, _, rest = who.partition(" ")
            if head in ("Claude", "Codex") and rest:
                names.append(rest)  # the command line names it without the provider
            for name in names:
                message = message.removeprefix(f"{name}: ").removeprefix(f"{name} ")
            return manual_reset.not_used_text(what, who, message)
        except Exception as e:
            _logger.error("menu bar: usage reset on %s #%s failed", provider, number, exc_info=True)
            return manual_reset.not_used_text(what, who, "error", f"{type(e).__name__}: {e}")
