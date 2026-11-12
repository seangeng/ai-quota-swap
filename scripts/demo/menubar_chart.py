"""Draw the menu bar's usage chart off screen into PNGs, from made-up data.

    uv run --with pyobjc-framework-Cocoa python scripts/demo/menubar_chart.py --out DIR
    uv run --with pyobjc-framework-Cocoa python scripts/demo/menubar_chart.py --out DIR usage spike

Each TARGET is a named shape (``--list`` shows them; all of them by
default) or a scenario with ledger days (``usage``, ``showcase``). It writes
``<target>-light.png`` and ``<target>-dark.png`` into DIR at 2x.

A shape is a week of invented accounts and figures, built here. A scenario
is seeded and read in a child process inside the demo sandbox (``guards.py``,
as ``run.py`` installs it): the ledger scan and the reads the menu bar makes,
then ``build_menu``; only the chart's description comes back, as JSON.

The picture is the real view (``ai_quota_swap.menubar_chart_view``) drawn
into a bitmap, for the light and the dark appearance, on the window
background colour. Nothing is shown on screen: before AppKit measures or
draws anything, the process takes the Prohibited activation policy (no Dock
icon, no menu bar, never active, no window can show), and it makes no
window, status item or menu.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # before any import that could write a .pyc

import argparse  # noqa: E402
import dataclasses  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
from datetime import date, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

#: The week every shape covers: the scenarios' clock, 2026-11-10, is today.
TODAY = date(2026, 11, 10)


# -- shapes: invented accounts and figures ------------------------------------------------


def _shape_accounts():
    from ai_quota_swap.menubar_chart import CostAccount

    def claude(n, name, email):
        return CostAccount("claude", f"{email}|", n, f"#{n} {name}")

    def codex(n, name, email):
        return CostAccount("codex", f"{email}|", n, f"#{n} {name}")

    return {
        "personal": claude(1, "personal", "maya@example.com"),
        "work": claude(2, "work", "maya@northwind.example.com"),
        "night": claude(3, "night-shift", "ops@northwind.example.com"),
        "side": claude(4, "side", "maya.side@example.com"),
        "ci": claude(5, "ci", "api-key-5@token.local"),
        "codex1": codex(1, "maya@example.com", "maya@example.com"),
        "codex2": codex(2, "work", "maya@northwind.example.com"),
        "long1": claude(1, "maya.rodriguez-castellanos@example.com", "maya.rodriguez-castellanos@example.com"),
        "long2": claude(2, "northwind-labs-platform-team", "platform@northwind.example.com"),
        "long3": claude(3, "weekend-side-project-account", "weekend@example.com"),
        "prefix1": claude(1, "northwind-platform-production", "platform@northwind.example.com"),
        "prefix2": claude(2, "northwind-platform-staging", "staging@northwind.example.com"),
        "stranger": CostAccount(
            "claude", "jordan.lee.contractor@example.org|", None,
            "jordan.lee.contractor@example.org (not managed)",
        ),
        "unattributed": CostAccount(None, "", None, "unattributed"),
    }


#: Per shape: one row per account, its cost on each of the seven days
#: (oldest first, today last), and the menu width it is drawn at (a menu
#: is as wide as its widest row).
SHAPES: dict[str, tuple[int, dict[str, tuple[float, ...]]]] = {
    "one-account": (320, {
        "personal": (18.40, 22.10, 9.75, 31.20, 26.80, 14.05, 12.40),
    }),
    "seven-accounts": (460, {
        "personal": (14.2, 18.9, 6.1, 22.4, 19.8, 11.0, 9.3),
        "work": (9.8, 12.1, 0.0, 15.6, 13.2, 8.4, 7.7),
        "night": (4.1, 0.0, 3.2, 6.8, 5.5, 2.9, 1.8),
        "side": (0.0, 2.4, 1.1, 0.0, 3.6, 0.0, 1.2),
        "ci": (1.9, 2.2, 0.0, 2.5, 2.1, 1.7, 0.6),
        "codex1": (3.4, 4.8, 1.2, 5.1, 6.0, 2.2, 3.9),
        "codex2": (0.0, 1.1, 0.0, 2.0, 0.0, 0.8, 0.0),
    }),
    "empty-days": (360, {
        "personal": (21.30, 0.0, 0.0, 17.60, 0.0, 24.90, 8.20),
        "work": (6.10, 0.0, 0.0, 9.40, 0.0, 3.30, 0.0),
    }),
    "spike": (360, {
        "personal": (4.10, 3.60, 41.80, 5.20, 3.90, 4.40, 2.70),
        "work": (1.20, 0.90, 9.60, 1.10, 1.40, 0.80, 0.50),
    }),
    "long-names": (480, {
        "long1": (12.0, 15.5, 9.0, 18.2, 11.4, 7.9, 6.3),
        "long2": (8.1, 6.4, 7.7, 9.9, 5.2, 4.8, 3.1),
        "long3": (0.0, 3.2, 0.0, 0.0, 6.6, 2.1, 0.0),
        "stranger": (1.4, 0.0, 2.2, 0.9, 0.0, 1.6, 0.4),
        "unattributed": (0.6, 0.3, 0.0, 0.8, 0.2, 0.0, 0.1),
    }),
    "single-day": (320, {
        "personal": (0.0, 0.0, 14.30, 0.0, 0.0, 0.0, 0.0),
        "work": (0.0, 0.0, 3.80, 0.0, 0.0, 0.0, 0.0),
    }),
    "today-only": (320, {
        "personal": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 6.85),
        "work": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 2.10),
    }),
    "shared-prefix": (340, {
        "prefix1": (16.2, 12.8, 19.4, 4.1, 3.3, 15.0, 11.6),
        "prefix2": (6.4, 9.9, 5.2, 0.0, 1.8, 7.7, 4.9),
    }),
    "codex-top-four": (460, {
        "codex2": (12.4, 15.1, 9.8, 18.3, 16.0, 11.2, 8.7),
        "personal": (9.2, 11.4, 4.0, 13.9, 12.6, 7.3, 6.1),
        "codex1": (6.8, 7.5, 3.9, 9.4, 8.8, 5.1, 4.4),
        "ci": (4.0, 5.2, 2.1, 6.3, 5.5, 3.9, 2.8),
        "work": (1.9, 2.4, 0.0, 3.1, 2.2, 1.4, 1.0),
        "night": (0.8, 0.0, 1.1, 1.5, 0.9, 0.6, 0.4),
        "side": (0.0, 0.6, 0.4, 0.0, 0.9, 0.0, 0.3),
    }),
    # Both providers with a #1 and a #2 in the top four: each Codex entry
    # says "Codex" at every cut, so no two entries read the same.
    "twin-numbers": (360, {
        "personal": (14.2, 18.9, 6.1, 22.4, 19.8, 11.0, 9.3),
        "codex1": (9.8, 12.1, 0.0, 15.6, 13.2, 8.4, 7.7),
        "codex2": (4.1, 0.0, 3.2, 6.8, 5.5, 2.9, 4.3),
        "work": (0.0, 5.4, 4.1, 0.0, 6.6, 3.0, 4.6),
        "night": (1.9, 2.2, 0.0, 2.5, 2.1, 1.7, 0.6),
    }),
}


def shape_chart(name: str):
    """The chart for a shape, built as the menu builds it from a read. A
    shape with a Codex account stands for a menu that shows both
    providers."""
    from ai_quota_swap.menubar_chart import CostDay, CostSummary, build_chart

    _width, rows = SHAPES[name]
    accounts = _shape_accounts()
    days = []
    for i in range(7):
        costs = tuple((accounts[k], v[i]) for k, v in rows.items() if v[i] > 0)
        days.append(CostDay(TODAY - timedelta(days=6 - i), costs))
    today = sum(c for _a, c in days[-1].costs)
    week = sum(c for d in days for _a, c in d.costs)
    both = any(accounts[k].provider == "codex" for k in rows)
    return build_chart(CostSummary(today=round(today, 6), week=round(week, 6), days=tuple(days)), both=both)


# -- scenarios: read in the sandbox --------------------------------------------------------


def chart_to_json(chart) -> dict:
    data = dataclasses.asdict(chart)
    for column in data["columns"]:
        column["day"] = column["day"].isoformat()
    return data


def chart_from_json(data: dict):
    from ai_quota_swap.menubar_chart import Column, LegendEntry, Segment, UsageChart

    return UsageChart(
        caption=data["caption"],
        columns=tuple(
            Column(
                date.fromisoformat(c["day"]), c["label"],
                tuple(Segment(**s) for s in c["segments"]), c["today"],
            )
            for c in data["columns"]
        ),
        legend=tuple(LegendEntry(**e) for e in data["legend"]),
    )


def describe_in_sandbox(scenario: str) -> int:
    """Child process: seed ``scenario`` in the demo sandbox, scan its
    ledger and read it as the menu bar does, build the menu, and print the
    cost row's chart as JSON."""
    sys.path.insert(0, str(HERE))
    import guards  # imports nothing from ai_quota_swap

    try:
        sb = guards.install()
    except Exception as e:  # noqa: BLE001
        print(f"menubar_chart.py: the demo sandbox could not be installed: {e}", file=sys.stderr)
        return 3
    import time

    import time_machine

    import fakeapi
    import scenarios

    s = scenarios.get(scenario)
    if s.ledger is None:
        print(f"menubar_chart.py: scenario {scenario!r} has no ledger days", file=sys.stderr)
        return 2
    time_machine.travel(s.epoch, tick=True).start()
    guards.install_fake_api(fakeapi.FakeAnthropic(scenarios.LiveState(s, clock=time.time),
                                                  on_unknown=sb.record_violation))
    try:
        guards.verify(sb, clock_epoch=s.epoch)
    except guards.DemoGuardError as e:
        print(f"menubar_chart.py: refusing to start.\n{e}", file=sys.stderr)
        return 3

    import seed

    switcher = seed.seed(s)
    from ai_quota_swap import menubar, menubar_data
    from ai_quota_swap.snapshot_source import SnapshotSource

    root = switcher.backup_dir
    menubar_data.scan_ledger(root, registry=switcher._get_sequence_data())
    now = time.time()
    inp = menubar.MenuInput(
        claude=menubar_data.claude_state(
            switcher, SnapshotSource(switcher), full=False, store_only=False, now=now,
        ),
        codex=menubar_data.CodexReader(root).state(full=False, store_only=False, now=now),
        settings=menubar.MenuBarSettings(),
        now=now,
        cost=menubar_data.read_cost(root),
    )
    menu = menubar.build_menu(inp)
    section = menu.section("cost")
    chart = section.rows[0].chart if section is not None else None
    if chart is None:
        print(f"menubar_chart.py: scenario {scenario!r} has no chart", file=sys.stderr)
        return 2
    # The other top-level rows, which set the menu's width.
    rows = [
        {"text": r.text, "submenu": bool(r.children), "checked": r.checked}
        for r in menu.rows() if not r.separator and r.chart is None
    ]
    print(json.dumps({"chart": chart_to_json(chart), "rows": rows}))
    if sb.violations:
        print("menubar_chart.py: the run crossed the sandbox:", file=sys.stderr)
        for v in sb.violations:
            print(f"  - {v}", file=sys.stderr)
        return 4
    return 0


def scenario_chart(name: str):
    """The scenario's chart, and how wide its menu is."""
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--describe", name],
        capture_output=True, text=True, cwd=REPO, timeout=300,
    )
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or f"menubar_chart.py: {name} failed ({proc.returncode})")
    data = json.loads(proc.stdout)
    return chart_from_json(data["chart"]), menu_width(data["rows"])


#: What NSMenu adds to a title, measured off screen with ``NSMenu.size`` on
#: macOS 26: padding, the check-mark column once a row has a check mark,
#: and a submenu's arrow.
MENU_PADDING, CHECK_COLUMN, SUBMENU_ARROW = 32.0, 14.0, 31.0


def appkit():
    """AppKit, once this process is barred from the screen: the Prohibited
    activation policy, set before anything is measured or drawn."""
    import AppKit

    app = AppKit.NSApplication.sharedApplication()
    if app.activationPolicy() != AppKit.NSApplicationActivationPolicyProhibited:
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)
    return AppKit


def menu_width(rows: list[dict]) -> float:
    """The width NSMenu gives these rows: its widest title and what it adds."""
    AppKit = appkit()

    font = AppKit.NSFont.menuFontOfSize_(0)

    def width(row: dict) -> float:
        text = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            row["text"], {AppKit.NSFontAttributeName: font},
        )
        return float(text.size().width) + (SUBMENU_ARROW if row["submenu"] else 0.0)

    marks = CHECK_COLUMN if any(r["checked"] for r in rows) else 0.0
    return max((width(r) for r in rows), default=0.0) + MENU_PADDING + marks


# -- drawing --------------------------------------------------------------------------------


#: Space above and below the view, as a menu leaves around its items.
MARGIN = 5.0


def render(chart, out: Path, *, width: float, dark: bool, scale: float = 2.0) -> Path:
    """Draw ``chart`` with the real view into a ``scale``x PNG at ``out``."""
    AppKit = appkit()

    from ai_quota_swap import menubar_chart_view

    backdrop_cls = _backdrop_class()
    view = menubar_chart_view.make_view(chart)
    # As wide as the menu: never narrower than the view and what NSMenu adds
    # to it beside a checked row (the view draws as in a menu with check
    # marks).
    w = max(float(width), menubar_chart_view.menu_width(chart))
    h = float(view.frame().size.height) + 2 * MARGIN
    backdrop = backdrop_cls.alloc().initWithFrame_(((0, 0), (w, h)))
    view.setFrame_(((0, MARGIN), (w, view.frame().size.height)))
    backdrop.addSubview_(view)
    name = AppKit.NSAppearanceNameDarkAqua if dark else AppKit.NSAppearanceNameAqua
    backdrop.setAppearance_(AppKit.NSAppearance.appearanceNamed_(name))
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, int(round(w * scale)), int(round(h * scale)), 8, 4, True, False,
        AppKit.NSDeviceRGBColorSpace, 0, 0,
    )
    rep.setSize_((w, h))
    backdrop.cacheDisplayInRect_toBitmapImageRep_(backdrop.bounds(), rep)
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    out.parent.mkdir(parents=True, exist_ok=True)
    png.writeToFile_atomically_(str(out), True)
    return out


_BACKDROP = None


def _backdrop_class():
    """A view that fills itself with the window background colour, looked up
    while drawing so it follows the appearance. Defined once."""
    global _BACKDROP
    if _BACKDROP is None:
        AppKit = appkit()

        class QswapChartBackdrop(AppKit.NSView):
            def isFlipped(self):
                return True

            def drawRect_(self, rect):
                AppKit.NSColor.windowBackgroundColor().setFill()
                AppKit.NSBezierPath.fillRect_(self.bounds())

        _BACKDROP = QswapChartBackdrop
    return _BACKDROP


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--describe"] and len(argv) == 2:
        return describe_in_sandbox(argv[1])
    p = argparse.ArgumentParser(prog="menubar_chart.py", description=__doc__.split("\n\n")[0])
    p.add_argument("targets", nargs="*", help="shapes or scenarios (default: every shape)")
    p.add_argument("--out", type=Path, help="directory for the PNGs")
    p.add_argument("--width", type=float, help="menu width in points (default: each shape's own)")
    p.add_argument("--list", action="store_true", help="list the shapes and exit")
    args = p.parse_args(argv)
    if args.list:
        for name, (width, rows) in SHAPES.items():
            print(f"{name:<16} {len(rows)} accounts, {width:g} pt wide")
        return 0
    if args.out is None:
        p.error("--out DIR is required")
    for target in args.targets or list(SHAPES):
        if target in SHAPES:
            chart, width = shape_chart(target), SHAPES[target][0]
        else:
            chart, width = scenario_chart(target)
        if args.width:
            width = args.width
        for theme in ("light", "dark"):
            path = render(chart, args.out / f"{target}-{theme}.png", width=width, dark=theme == "dark")
            print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
