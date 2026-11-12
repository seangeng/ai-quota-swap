"""Turn the demo harness's pages into README assets.

    uv run python scripts/demo/capture.py html        # CLI shots -> docs/shots/*.html
    uv run python scripts/demo/capture.py shots       # raw PNGs (CLI pages + served TUI)
    uv run python scripts/demo/capture.py frame       # ScreenFramed, when configured
    uv run python scripts/demo/capture.py record      # the GIFs in assets/demos (LittleDemo)
    uv run python scripts/demo/capture.py index       # docs/shots/index.html
    uv run python scripts/demo/capture.py social      # assets/social-preview.png, from the hero

``build.py`` runs html, shots, social, frame and index in order. Every qswap run
happens in a guarded ``run.py`` subprocess with its own temp home; this
process only drives Chrome, pngquant, ffmpeg and the two npx CLIs.
See scripts/demo/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import html
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import cdp  # noqa: E402
import shots  # noqa: E402
import stories  # noqa: E402

SHOTS_DIR = REPO / "docs" / "shots"
RAW_DIR = REPO / "assets" / "screenshots" / "raw"
FRAMED_DIR = REPO / "assets" / "screenshots"
DEMOS_DIR = REPO / "assets" / "demos"
RUN = HERE / "run.py"

# Browser viewport for screenshots: the 1200 px window plus room around it.
VIEWPORT = (1240, 900)
# The menu bar drawings: a menu with a submenu beside it, or the hero.
MENUBAR_VIEWPORT = (1400, 1000)
# Recordings: the window on its stage, sized for a README GIF. A dashboard
# terminal of w x h px records in a (w + 104) x (h + 140) viewport, and the
# GIF keeps the window plus 8 px of the stage around it.
GIF_WIDTH = 960
GIF_MAX_BYTES = 1_500_000
TUI_QUERY = "fontsize={font}&w={width}&h={height}&theme={theme}"
# Played pages are drawn larger than the GIF and scaled down to it, so text
# keeps its edges through the video's colour subsampling.
TYPING_ZOOM = 1.8
MENUBAR_PLAY_WIDTH = 1440  # px of drawing in the recording, before the GIF's scale-down
MENUBAR_TICKS = 3  # 86%, 88%, then the switch at 91%
MENUBAR_FRAMES_ARGS = ("--menubar", "--auto-switch", "--ticks", str(MENUBAR_TICKS))
READY = "document.body.classList.contains('-first-byte')"
LITTLEDEMO = ("npx", "--yes", "@littledemo/cli")
LITTLEDEMO_OVERCAPTURE = 3  # @littledemo/recorder 0.3.0, OVERCAPTURE_FACTOR
SCREENFRAMED = ("npx", "--yes", "@screenframed/cli")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# -- CLI pages ----------------------------------------------------------------


def page_path(name: str, theme: str) -> Path:
    return SHOTS_DIR / (f"{name}.html" if theme == "dark" else f"{name}-{theme}.html")


def render_html(only: set[str] | None = None) -> list[Path]:
    out = []
    for shot in shots.CLI_SHOTS:
        if only and shot.name not in only:
            continue
        for theme in shot.themes:
            target = page_path(shot.name, theme)
            cmd = [
                sys.executable, str(RUN), shot.scenario, "--width", str(shot.columns),
                "--theme", theme, "--html", str(target), "--", *shot.args,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(f"{shot.name} ({theme}) failed with {r.returncode}:\n{r.stderr}")
            log(f"html   {target.relative_to(REPO)}")
            out.append(target)
    # A drawing with a window behind its menu waits for that window's PNG
    # (capture_shots renders it after the dashboard shots).
    for shot in shots.MENUBAR_SHOTS:
        if (only and shot.name not in only) or shot.beside:
            continue
        out.append(render_menubar(shot))
    return out


def render_menubar(shot: shots.MenubarShot) -> Path:
    """``run.py <scenario> --menubar --html``: the menu bar drawing."""
    target = SHOTS_DIR / f"{shot.name}.html"
    cmd = [sys.executable, str(RUN), shot.scenario, "--menubar", "--theme", shot.theme,
           "--html", str(target)]
    if shot.auto_switch:
        cmd += ["--auto-switch", shot.auto_switch]
    if shot.open:
        cmd += ["--open", shot.open]
    if shot.highlight:
        cmd += ["--highlight", shot.highlight]
    if shot.beside:
        window = RAW_DIR / f"{shot.beside}.png"
        if not window.exists():
            raise SystemExit(f"{window} is missing; run `capture.py shots --only {shot.beside}` first")
        cmd += ["--beside", str(window)]
    if shot.args:
        cmd += ["--", *shot.args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{shot.name} failed with {r.returncode}:\n{r.stderr}")
    log(f"html   {target.relative_to(REPO)}")
    return target


# -- PNGs ---------------------------------------------------------------------


def compress(png: Path) -> None:
    exe = shutil.which("pngquant")
    if not exe:
        log("       (pngquant not found; PNG left uncompressed)")
        return
    subprocess.run(
        [exe, "--quality=70-95", "--speed=1", "--strip", "--force", "--output", str(png), str(png)],
        check=True,
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """``run.py <scenario> --serve`` in the background, for one capture."""

    def __init__(self, scenario: str, view: str = "dashboard", theme: str | None = None,
                 compression: float | None = None) -> None:
        self.port = free_port()
        cmd = [sys.executable, str(RUN), scenario, "--serve", "--port", str(self.port), "--view", view]
        if theme:
            cmd += ["--theme", theme]
        if compression:
            cmd += ["--compression", f"{compression:g}"]
        self.log = tempfile.NamedTemporaryFile(prefix="qswap-serve-", suffix=".log", delete=False)
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=1).read()
                break
            except OSError:
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    self.stop()
                    raise SystemExit(f"the demo server did not start; see {self.log.name}")
                time.sleep(0.2)

    def url(self, query: str) -> str:
        return f"http://127.0.0.1:{self.port}/?{query}"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def image_page(png: Path, target: Path, title: str) -> None:
    """A static page showing a TUI screenshot at 1x CSS size, so the TUI shots
    can be published (and framed) with the CLI pages: the live server cannot."""
    raw = png.read_bytes()
    w, h = struct.unpack(">II", raw[16:24])  # the IHDR chunk
    data = base64.b64encode(raw).decode()
    target.write_text(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<meta name="viewport" content="width={w // 2}">
<style>html,body{{margin:0;background:transparent}}body{{display:flex;justify-content:center}}
img{{display:block;width:{w // 2}px;height:{h // 2}px}}</style></head>
<body><img id="window" alt="{html.escape(title)}" src="data:image/png;base64,{data}"></body></html>
""", encoding="utf-8")


def capture_shots(only: set[str] | None = None) -> list[Path]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = []

    async def cli_pages() -> None:
        for shot in shots.CLI_SHOTS:
            if only and shot.name not in only:
                continue
            for theme in shot.themes:
                page = page_path(shot.name, theme)
                if not page.exists():
                    raise SystemExit(f"{page} is missing; run `capture.py html` first")
                png = RAW_DIR / (page.stem + ".png")
                await cdp.shoot(page.as_uri(), png, width=VIEWPORT[0], height=VIEWPORT[1], scale=2)
                compress(png)
                log(f"png    {png.relative_to(REPO)}  {png.stat().st_size // 1024} KB")
                out.append(png)

    asyncio.run(cli_pages())
    for shot in shots.TUI_SHOTS:
        if only and shot.name not in only:
            continue
        png = RAW_DIR / f"{shot.name}.png"
        with Server(shot.scenario, theme=shot.theme) as server:
            asyncio.run(_shoot_tui(server, shot, png))
        compress(png)
        image_page(png, SHOTS_DIR / f"{shot.name}.html", shot.caption)
        log(f"png    {png.relative_to(REPO)}  {png.stat().st_size // 1024} KB")
        out.append(png)

    async def menubar_pages() -> None:
        for shot in shots.MENUBAR_SHOTS:
            if only and shot.name not in only:
                continue
            page = render_menubar(shot) if shot.beside else SHOTS_DIR / f"{shot.name}.html"
            if not page.exists():
                raise SystemExit(f"{page} is missing; run `capture.py html` first")
            png = RAW_DIR / f"{shot.name}.png"
            await cdp.shoot(page.as_uri(), png, width=MENUBAR_VIEWPORT[0], height=MENUBAR_VIEWPORT[1],
                            scale=2, selector="#stage")
            compress(png)
            log(f"png    {png.relative_to(REPO)}  {png.stat().st_size // 1024} KB")
            out.append(png)

    asyncio.run(menubar_pages())
    for shot in shots.CHART_SHOTS:
        if only and shot.name not in only:
            continue
        out += chart_shot(shot)
    return out


def _appkit_python() -> list[str]:
    """A Python with AppKit: this one when it has it, else this project's
    with pyobjc added for the run (``uv run --with pyobjc-framework-Cocoa``)."""
    import importlib.util

    if importlib.util.find_spec("AppKit") is not None:
        return [sys.executable]
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("menubar_chart.py needs AppKit: install pyobjc-framework-Cocoa, or uv")
    return [uv, "run", "--with", "pyobjc-framework-Cocoa", "python"]


def chart_shot(shot: shots.ChartShot) -> list[Path]:
    """The cost row's chart alone, dark and light: ``menubar_chart.py``
    draws it with the app's AppKit view, off screen, for the shot's
    scenario (read in the demo sandbox). macOS only: elsewhere it says so
    and keeps the PNGs there are."""
    if sys.platform != "darwin":
        log(f"png    {shot.name}: skipped, the AppKit view draws on macOS only")
        return []
    made = []
    with tempfile.TemporaryDirectory(prefix=f"qswap-{shot.name}-") as tmp:
        cmd = [*_appkit_python(), str(HERE / "menubar_chart.py"), "--out", tmp, shot.scenario]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)
        if r.returncode != 0:
            raise SystemExit(f"{shot.name} failed with {r.returncode}:\n{r.stderr}")
        for theme, suffix in (("dark", ""), ("light", "-light")):
            png = RAW_DIR / f"{shot.name}{suffix}.png"
            shutil.copyfile(Path(tmp) / f"{shot.scenario}-{theme}.png", png)
            compress(png)
            log(f"png    {png.relative_to(REPO)}  {png.stat().st_size // 1024} KB")
            made.append(png)
    return made


async def _shoot_tui(server: Server, shot: shots.TuiShot, png: Path) -> None:
    """One dashboard shot: a terminal of exactly ``shot.columns`` x
    ``shot.rows`` cells, the shot's keys played from the first frame."""
    import tour  # noqa: PLC0415  (tour imports this module)

    async with cdp.chrome(2) as new_page:
        w, h = await tour.fit_cells(new_page, server, shot.theme, shot.columns, shot.rows,
                                    font=shot.font_size)
        page = await new_page()
        await page.setup(w + 120, h + 160)
        await page.goto(server.url(f"fontsize={shot.font_size}&w={w}&h={h}&theme={shot.theme}"))
        await page.wait_for(READY)
        await asyncio.sleep(shot.settle)
        await tour.play(page, shot.steps)
        await asyncio.sleep(0.8)
        await page.screenshot(png, "#window")
        await page.close()


# -- social preview -----------------------------------------------------------


SOCIAL_PREVIEW = REPO / "assets" / "social-preview.png"
SOCIAL_SIZE = (1280, 640)  # GitHub's social preview: 2:1
SOCIAL_MAX_BYTES = 1_000_000


async def _hero_layout(page_path: Path) -> dict:
    """Where the hero's window and menu sit on its stage, in CSS px, and
    the top of each row of the menu."""
    async with cdp.chrome(1) as new_page:
        page = await new_page()
        await page.setup(*MENUBAR_VIEWPORT)
        await page.goto(page_path.as_uri())
        await asyncio.sleep(0.3)
        layout = await page.eval("""(() => {
          const stage = document.getElementById("stage").getBoundingClientRect();
          const box = (e) => {
            const b = e.getBoundingClientRect();
            return {left: b.left - stage.left, top: b.top - stage.top,
                    right: b.right - stage.left, bottom: b.bottom - stage.top};
          };
          const menu = document.querySelector('.menu[data-level="0"]');
          return {width: stage.width, height: stage.height,
                  window: box(document.querySelector("img.window")), menu: box(menu),
                  rows: [...menu.querySelectorAll(":scope > .item")].map((e) => box(e).top)};
        })()""")
        await page.close()
    return layout


def social_crop(layout: dict) -> tuple[float, float, float, float]:
    """The part of the hero a 2:1 social preview shows, in CSS px (x, y,
    width, height): from the menu bar down, the window behind the menu whole
    across and the menu beside it, cut 2 px above the top of one of the
    menu's rows, so the edge falls between two lines of its text. Only
    wallpaper is trimmed at the sides (a little is kept at each)."""
    spare_left = max(0.0, layout["window"]["left"] - 4)
    spare_right = max(0.0, layout["width"] - layout["menu"]["right"] - 2)
    widest = layout["width"]
    narrowest = widest - spare_left - spare_right
    cuts = [top - 2 for top in layout["rows"] if narrowest <= 2 * (top - 2) <= widest]
    height = max(cuts) if cuts else widest / 2
    width = 2 * height
    trim = widest - width
    right = min(spare_right, trim / 2)
    return trim - right, 0.0, width, height


def social_preview(hero: Path = RAW_DIR / "hero.png", page: Path = SHOTS_DIR / "hero.html") -> Path:
    """``assets/social-preview.png``: the dark hero (the menu open in front
    of the dashboard) cropped to 2:1 by :func:`social_crop` and scaled to
    1280 x 640, nothing added. It is uploaded by hand in the repository's
    settings; nothing links it."""
    for path in (hero, page):
        if not path.exists():
            raise SystemExit(f"{path} is missing; run `capture.py shots --only hero` first")
    layout = asyncio.run(_hero_layout(page))
    raw = hero.read_bytes()
    px = struct.unpack(">II", raw[16:24])[0] / layout["width"]  # the PNG's px per CSS px
    x, y, w, h = (round(v * px) for v in social_crop(layout))
    w, h = 2 * (w // 2), w // 2  # exactly 2:1
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(hero), "-vf",
         f"crop={w}:{h}:{x}:{y},scale={SOCIAL_SIZE[0]}:{SOCIAL_SIZE[1]}:flags=lanczos",
         "-frames:v", "1", str(SOCIAL_PREVIEW)],
        check=True,
    )
    compress(SOCIAL_PREVIEW)
    size = SOCIAL_PREVIEW.stat().st_size
    log(f"png    {SOCIAL_PREVIEW.relative_to(REPO)}  {size // 1024} KB (from {w}x{h} of {hero.name})")
    if size > SOCIAL_MAX_BYTES:
        raise SystemExit(f"{SOCIAL_PREVIEW.name} is {size} bytes; GitHub takes 1 MB at most")
    return SOCIAL_PREVIEW


# -- ScreenFramed -------------------------------------------------------------


def screenframed_credentials() -> bool:
    return bool(os.environ.get("SCREENFRAMED_API_KEY")) or (Path.home() / ".screenframed.json").exists()


def frame(base_url: str | None, *, background: str, device: str | None, only: set[str] | None = None) -> int:
    """Frame each published page with ScreenFramed (its cloud renders the URL).

    Needs the shots published somewhere public (``--base-url`` or
    ``SCREENFRAMED_BASE_URL``, the URL that serves docs/shots/) and a login
    (``npx @screenframed/cli login`` writes ~/.screenframed.json, or
    ``SCREENFRAMED_API_KEY``). Without both it says so and does nothing.
    """
    base_url = base_url or os.environ.get("SCREENFRAMED_BASE_URL")
    missing = []
    if not base_url:
        missing.append("a public base URL for docs/shots (--base-url or SCREENFRAMED_BASE_URL)")
    if not screenframed_credentials():
        missing.append("a ScreenFramed login (`npx @screenframed/cli login` or SCREENFRAMED_API_KEY)")
    if missing:
        log("frame  skipped: ScreenFramed captures from its cloud and needs " + " and ".join(missing) + ".")
        return 0
    FRAMED_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for name in shots.page_names():
        if only and name.removesuffix("-light") not in only:
            continue
        url = f"{base_url.rstrip('/')}/{name}.html"
        out = FRAMED_DIR / f"{name}.png"
        cmd = [*SCREENFRAMED, "capture", url, "--background", background, "--shadow",
               "--selector", "#window", "--wait-for", "networkidle", "--output", str(out)]
        if device:
            cmd += ["--device", device]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            failures += 1
            log(f"frame  {name}: failed ({r.returncode}): {r.stderr.strip()[-300:]}")
            continue
        compress(out)
        log(f"frame  {out.relative_to(REPO)}")
    return 1 if failures else 0


# -- recordings ---------------------------------------------------------------


# 192 colours: the recordings draw with far fewer, and the smaller palette
# keeps the busiest (the dashboard's 40-row views in qswap's colours) under
# GIF_MAX_BYTES with room to spare, with no difference to see.
GIF_PALETTE = (
    "split[a][b];[a]palettegen=max_colors=192:stats_mode=full[p];"
    "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
)


# Frames that repeat the one before (a held screen) are dropped, so the GIF
# holds one frame instead of re-encoding the video's noise every 83 ms. The
# thresholds are strict: a held screen of these recordings differs by almost
# nothing, while the faintest step of a fade or a changed digit is kept.
DECIMATE = "mpdecimate=hi=128:lo=64:frac=0.001"


def gif_filters(*, width: int, crop: str | None) -> str:
    """Crop (ffmpeg w:h:x:y, the stage around the window), then scale down to
    ``width`` at most, then a palette."""
    pre = f"crop={crop}," if crop else ""
    return f"{pre}scale='min({width},iw)':-2:flags=lanczos,{GIF_PALETTE}"


def _duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                        str(path)], capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def _last_frame_at(gif: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "packet=pts_time", "-of", "csv=p=0",
                        str(gif)], capture_output=True, text=True, check=True)
    return float(r.stdout.split()[-1])


def mp4_to_gif(mp4: Path, gif: Path, *, width: int = GIF_WIDTH, fps: int = 12,
               crop: str | None = None, trim_ms: int = 0) -> None:
    """``trim_ms`` of video are cut from the start."""
    graph = f"fps={fps},{DECIMATE},{gif_filters(width=width, crop=crop)}"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{trim_ms / 1000:g}", "-i", str(mp4),
           "-vf", graph, "-fps_mode", "vfr"]
    subprocess.run([*cmd, str(gif)], check=True)
    # The last frame is held to the video's end (dropping its repeats left it
    # nothing to measure its delay by).
    hold = _duration(mp4) - trim_ms / 1000 - _last_frame_at(gif)
    if hold > 0.1:
        subprocess.run([*cmd, "-final_delay", str(round(hold * 100)), str(gif)], check=True)


def frames_to_gif(frames: list[tuple[float, Path]], gif: Path, *, width: int = GIF_WIDTH,
                  crop: str | None = None) -> None:
    """Variable-timing frames (from ``cdp.record_frames``) into a palette GIF."""
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "frames.txt"
        lines = []
        for i, (t, path) in enumerate(frames):
            nxt = frames[i + 1][0] if i + 1 < len(frames) else t + 1.5
            lines.append(f"file '{path}'\nduration {max(0.04, nxt - t):.3f}")
        lines.append(f"file '{frames[-1][1]}'")
        listing.write_text("\n".join(lines) + "\n")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-vf", gif_filters(width=width, crop=crop), "-r", "12", str(gif)],
            check=True,
        )


def littledemo_project() -> str:
    """The maintainer's LittleDemo project for these recordings (made once)."""
    wanted = os.environ.get("LITTLEDEMO_PROJECT_ID")
    if wanted:
        return wanted
    r = subprocess.run([*LITTLEDEMO, "projects", "list", "--json"], capture_output=True, text=True, check=True)
    for p in json.loads(r.stdout):
        if p.get("name") == "ai-quota-swap":
            return p["id"]
    r = subprocess.run(
        [*LITTLEDEMO, "projects", "create", "--name", "ai-quota-swap",
         "--url", "https://github.com/seangeng/ai-quota-swap", "--json"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout)["id"]


class StaticServer:
    """A directory on 127.0.0.1, for the recorder to load a played page from."""

    def __init__(self, root: Path) -> None:
        import functools
        import http.server
        import threading

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

        handler = functools.partial(Quiet, directory=str(root))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{name}"

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


async def _element_size(path: Path, selector: str) -> tuple[int, int]:
    """The CSS px size of ``selector`` on a local page."""
    import math

    async with cdp.chrome(1) as new_page:
        page = await new_page()
        await page.setup(1800, 1400)
        await page.goto(path.as_uri())
        await asyncio.sleep(0.3)
        r = await page.rect(selector)
        await page.close()
    return math.ceil(r["width"]), math.ceil(r["height"])


def page_duration(page: Path) -> int:
    """A played page's length in ms: its timeline's last event."""
    import re

    m = re.search(r'<script type="application/json" id="timeline">(.*?)</script>',
                  page.read_text(encoding="utf-8"), re.S)
    if not m:
        raise SystemExit(f"{page} has no timeline; it is not a played page")
    return int(json.loads(m.group(1))[-1]["at"])


def _run_demo(args: list[str]) -> str:
    r = subprocess.run([sys.executable, str(RUN), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"run.py {' '.join(args)} failed with {r.returncode}:\n{r.stderr}")
    return r.stdout


def build_typing_page(story: stories.Story, target: Path) -> Path:
    """``run.py --typing``: the story's commands typed and answered, in order."""
    _run_demo([story.scenario, "--width", "80", "--height", "22", "--typing",
               "--zoom", f"{TYPING_ZOOM:g}", "--title", "qswap", "--html", str(target),
               "--", *story.args])
    return target


def build_menubar_page(story: stories.Story, target: Path) -> Path:
    """The menu bar after each of the engine's checks, played in order.

    Three guarded runs of the same deterministic scenario: one to find the
    widest drawing (Switch history open), then the frames at that size with
    the menu as it opens and with Switch history open. The shots: the menu
    open on the first check (the GIF's first frame), the bar alone while the
    title climbs, the bar with the app's notification of the switch, then the
    menu open on the same check with Switch history showing the switch.
    """
    import player_page

    with tempfile.TemporaryDirectory(prefix="qswap-menubar-frames-") as tmp:
        plain, history = Path(tmp) / "plain", Path(tmp) / "history"
        _run_demo([story.scenario, *MENUBAR_FRAMES_ARGS, "--open", "Switch history",
                   "--frames", str(history)])
        sizes = [asyncio.run(_element_size(history / f"frame-{i:02d}.html", "#stage")) for i in range(MENUBAR_TICKS)]
        w, h = max(s[0] for s in sizes) + 4, max(s[1] for s in sizes) + 4
        size = f"{w}x{h}"
        _run_demo([story.scenario, *MENUBAR_FRAMES_ARGS, "--size", size, "--frames", str(plain)])
        _run_demo([story.scenario, *MENUBAR_FRAMES_ARGS, "--size", size, "--open", "Switch history",
                   "--frames", str(history)])

        def drawn(path: Path) -> str:
            return path.read_text(encoding="utf-8")

        shots = [
            # The menu open on the first check: #1 personal active, 5h 86%,
            # the cost row the week's chart.
            # Short: the recorder shows this frame for about a second before
            # its key starts the play.
            player_page.Shot(drawn(plain / "frame-00.html"), hold_ms=1000),
            # The menu shut, the bar magnified: the title climbs a minute at a
            # time. Fades are short, as the Mac's are (a title changes at
            # once), and each full-frame step of a fade costs a GIF 130 to
            # 180 KB (the menu with its chart is taller).
            player_page.Shot(drawn(plain / "frame-00-closed.html"), hold_ms=1500, fade_ms=120, zoom=True),
            player_page.Shot(drawn(plain / "frame-01-closed.html"), hold_ms=1500, fade_ms=120, zoom=True),
            # The engine switches at 91%: the title changes, and the banner is
            # the notification the app posts, which says why.
            player_page.Shot(drawn(plain / "frame-02-closed.html"), hold_ms=3400, fade_ms=200, zoom=True),
            # The menu opens on the same check: #2 work checked, #1 personal
            # orange at 91%, the week's cost chart, and the switch in Switch
            # history.
            player_page.Shot(drawn(history / "frame-02.html"), hold_ms=4400, fade_ms=160),
        ]
    target.write_text(player_page.page(shots, width=w, height=h, scale=MENUBAR_PLAY_WIDTH / w),
                      encoding="utf-8")
    return target


PAGE_BUILDERS = {"typing": build_typing_page, "menubar": build_menubar_page}


def recording_page(story: stories.Story) -> Path:
    """Write a played story's page to docs/shots/recording-<name>.html."""
    target = SHOTS_DIR / f"recording-{story.name}.html"
    PAGE_BUILDERS[story.page](story, target)
    log(f"html   {target.relative_to(REPO)}")
    return target


async def _tui_layout(server: Server, story: stories.Story) -> tuple[int, int]:
    """The terminal's px size: the story's cells fitted as tour.py fits them."""
    import tour  # noqa: PLC0415  (tour imports this module)

    async with cdp.chrome(1) as new_page:
        return await tour.fit_cells(new_page, server, "dark", *story.cells, font=story.font)


def even(size: tuple[int, int]) -> tuple[int, int]:
    """A video size H.264 accepts: both sides even, rounded up."""
    return size[0] + size[0] % 2, size[1] + size[1] % 2


def record(name: str, *, mode: str, upload: bool) -> dict:
    """Record one storyboard. ``mode`` is ``littledemo`` (its local recorder
    drives Chrome against a loopback server) or ``local`` (frames through the
    DevTools protocol, then ffmpeg). Returns a result record."""
    story = stories.STORIES[name]
    DEMOS_DIR.mkdir(parents=True, exist_ok=True)
    gif = DEMOS_DIR / f"{name}.gif"
    result: dict = {"name": name, "scenario": story.scenario, "mode": mode}
    with contextlib.ExitStack() as stack:
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=f"qswap-rec-{name}-")))
        if story.page:
            # A page the harness wrote: it holds its first frame until a key
            # starts it, and records at its own size (nothing to crop).
            page = recording_page(story)
            url = stack.enter_context(StaticServer(page.parent)).url(page.name)
            viewport = even(asyncio.run(_element_size(page, "#frame")))
            key_ms = stories.LITTLEDEMO_KEY_MS if mode == "littledemo" else 0
            steps = stories.page_steps(page_duration(page), key_ms=key_ms)
            crop = None
            result["page"] = str(page.relative_to(REPO))
        else:
            # LittleDemo's local recorder over-captures: it runs the page at a
            # third of real speed and speeds the video up 3x. The engine's
            # clock lives in the server, so slow it by the same factor to keep
            # the storyboard's pacing.
            import scenarios as _scenarios  # noqa: PLC0415

            auto = _scenarios.get(story.scenario).auto
            compression = auto.compression / LITTLEDEMO_OVERCAPTURE if (auto and mode == "littledemo") else None
            server = stack.enter_context(Server(story.scenario, story.view, compression=compression))
            w, h = asyncio.run(_tui_layout(server, story)) if story.cells else story.term
            url = server.url(TUI_QUERY.format(font=story.font, width=w, height=h, theme="dark") + "&stage=1")
            viewport = even((w + 104, h + 140))
            steps = story.steps
            crop = f"{w + 40}:{h + 80}:32:30"
        if mode == "littledemo":
            config = stories.to_littledemo(story, tmp / "steps.json", viewport=viewport, steps=steps)
            mp4 = tmp / f"{name}.mp4"
            cmd = [*LITTLEDEMO, "record", url, "--steps", str(config), "--out", str(mp4),
                   "--viewport", f"{viewport[0]}x{viewport[1]}", "--mockup", "none",
                   "--no-zoom", "--json"]
            log(f"record {name}: {' '.join(cmd)}")
            env = {**os.environ, "LITTLEDEMO_NO_TELEMETRY": "1", "LITTLEDEMO_CAPTURE_QUALITY": "92"}
            r = subprocess.run(cmd, capture_output=True, text=True, env=env)
            result["record"] = {"exit": r.returncode, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()[-2000:]}
            for line in r.stderr.strip().splitlines()[-12:]:
                log(f"       | {line}")
            if r.returncode != 0 or not mp4.exists():
                raise SystemExit(f"littledemo record failed ({r.returncode}):\n{r.stderr[-2000:]}")
            mp4_to_gif(mp4, gif, crop=crop, trim_ms=story.trim_ms)
            kept = DEMOS_DIR / f"{name}.mp4"
            shutil.copyfile(mp4, kept)
            result["video"] = str(kept.relative_to(REPO))
            thumb = mp4.with_suffix(".png")
            if upload:
                project = littledemo_project()
                cmd = [*LITTLEDEMO, "demos", "upload", str(mp4), "--project", project,
                       "--prompt", story.prompt, "--title", story.title, "--gif", str(gif), "--json"]
                if thumb.exists():
                    cmd += ["--thumbnail", str(thumb)]
                r = subprocess.run(cmd, capture_output=True, text=True, env=env)
                if r.returncode != 0:
                    raise SystemExit(f"littledemo upload failed ({r.returncode}):\n{r.stderr[-2000:]}")
                result["upload"] = json.loads(r.stdout)
            result["recorder"] = ("@littledemo/cli record (local Chrome, loopback "
                                  + ("page" if story.page else "dashboard") + "), GIF re-encoded with ffmpeg")
        else:
            frames = asyncio.run(cdp.record_frames(
                url, steps, tmp / "frames", width=viewport[0], height=viewport[1], fps=8,
                ready=f"!!document.querySelector({json.dumps(story.ready)})",
            ))
            cut = story.trim_ms / 1000
            kept = [(t - cut, png) for t, png in frames if t >= cut] or frames[-1:]
            frames_to_gif(kept, gif, crop=crop)
            result["recorder"] = "DevTools protocol frame grabs (cdp.record_frames), GIF made with ffmpeg"
    result["gif"] = str(gif.relative_to(REPO))
    result["gifBytes"] = gif.stat().st_size
    log(f"gif    {gif.relative_to(REPO)}  {gif.stat().st_size // 1024} KB")
    if gif.stat().st_size > GIF_MAX_BYTES:
        log(f"       over {GIF_MAX_BYTES // 1000} KB: the README budget for one GIF")
    return result


def upload(name: str) -> dict:
    """Host an existing recording (assets/demos/<name>.mp4 + .gif) on LittleDemo.

    Separate from ``record`` so a recording is looked at before it is
    published, and publishing never costs another recording."""
    story = stories.STORIES[name]
    mp4, gif = DEMOS_DIR / f"{name}.mp4", DEMOS_DIR / f"{name}.gif"
    if not mp4.exists():
        raise SystemExit(f"{mp4} is missing; run `capture.py record {name}` first")
    with tempfile.TemporaryDirectory(prefix=f"qswap-up-{name}-") as tmp:
        thumb = Path(tmp) / f"{name}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-1.5", "-i", str(mp4),
                        "-frames:v", "1", str(thumb)], check=True)
        cmd = [*LITTLEDEMO, "demos", "upload", str(mp4), "--project", littledemo_project(),
               "--prompt", story.prompt, "--title", story.title, "--thumbnail", str(thumb), "--json"]
        if gif.exists():
            cmd += ["--gif", str(gif)]
        r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "LITTLEDEMO_NO_TELEMETRY": "1"})
    if r.returncode != 0:
        raise SystemExit(f"littledemo upload failed ({r.returncode}):\n{r.stderr[-2000:]}")
    hosted = json.loads(r.stdout)
    log(f"upload {name}: {hosted.get('shareUrl')}")
    out = {"name": name, "scenario": story.scenario, "upload": hosted}
    if gif.exists():
        out["gifBytes"] = gif.stat().st_size
    return out


def save_recordings(results: list[dict]) -> None:
    path = DEMOS_DIR / "recordings.json"
    existing = json.loads(path.read_text()) if path.exists() else {}
    for r in results:
        r.pop("record", None)  # the raw recorder log stays out of the repo
        before = existing.get(r["name"], {})
        if "gif" in r and "upload" not in r:
            # A new recording: a copy hosted earlier shows the old one.
            before.pop("upload", None)
        existing[r["name"]] = {**before, **r}
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


# -- index --------------------------------------------------------------------


def write_index() -> Path:
    from ai_quota_swap.tui import theme as c  # qswap's light palette, as the light shots use it

    rows = []
    for shot in shots.CLI_SHOTS:
        links = " · ".join(
            f'<a href="{page_path(shot.name, t).name}">{t}</a>' for t in shot.themes
        )
        pngs = " · ".join(
            f'<a href="../../assets/screenshots/raw/{page_path(shot.name, t).stem}.png">{t} PNG</a>'
            for t in shot.themes
        )
        rows.append(
            f"<tr><td><code>{html.escape('qswap ' + ' '.join(shot.args).replace(' ++ ', ' ; qswap '))}</code></td>"
            f"<td>{html.escape(shot.caption)}</td><td>{links}</td><td>{pngs}</td></tr>"
        )
    for shot in shots.TUI_SHOTS:
        keys = " ".join(str(v) for k, v in shot.steps if k in ("type", "key"))
        rows.append(
            f"<tr><td><code>qswap{(' then ' + html.escape(keys)) if keys else ''}</code></td>"
            f"<td>{html.escape(shot.caption)}</td><td><a href=\"{shot.name}.html\">{shot.theme}</a></td>"
            f"<td><a href=\"../../assets/screenshots/raw/{shot.name}.png\">PNG</a></td></tr>"
        )
    for shot in shots.MENUBAR_SHOTS:
        how = "qswap menubar" + (f" after qswap {' '.join(shot.args)}" if shot.args else "")
        if shot.open:
            how += f", {shot.open} open"
        rows.append(
            f"<tr><td><code>{html.escape(how)}</code></td>"
            f"<td>{html.escape(shot.caption)} (a drawing of the menu's rows)</td>"
            f"<td><a href=\"{shot.name}.html\">{shot.theme}</a></td>"
            f"<td><a href=\"../../assets/screenshots/raw/{shot.name}.png\">PNG</a></td></tr>"
        )
    for shot in shots.CHART_SHOTS:
        pngs = " · ".join(
            f'<a href="../../assets/screenshots/raw/{shot.name}{suffix}.png">{theme} PNG</a>'
            for theme, suffix in (("dark", ""), ("light", "-light"))
        )
        rows.append(
            f"<tr><td><code>menubar_chart.py {html.escape(shot.scenario)}</code></td>"
            f"<td>{html.escape(shot.caption)} (the app's own AppKit view, drawn off screen)</td>"
            f"<td></td><td>{pngs}</td></tr>"
        )
    target = SHOTS_DIR / "index.html"
    target.write_text(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>qswap demo shots</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         max-width: 960px; margin: 40px auto; padding: 0 20px; color: {c.FOREGROUND_LIGHT};
         background: {c.BACKGROUND_LIGHT}; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }} p {{ color: {c.MUTED_LIGHT}; margin-top: 0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td, th {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid {c.PANEL_LIGHT};
            vertical-align: top; }}
  code {{ font: 13px "SF Mono", Menlo, Consolas, monospace; white-space: nowrap; }}
  a {{ color: {c.ACCENT_LIGHT}; }}
</style></head>
<body>
<h1>qswap demo shots</h1>
<p>Rendered by scripts/demo from synthetic scenarios: invented accounts, invented numbers.
Each page is the tool's own output at a fixed clock.</p>
<table><tr><th>Command</th><th>Shows</th><th>Page</th><th>Raw capture</th></tr>
{chr(10).join(rows)}
</table>
</body></html>
""", encoding="utf-8")
    log(f"index  {target.relative_to(REPO)}")
    return target


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="capture.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("html", "shots", "index"):
        s = sub.add_parser(name)
        s.add_argument("--only", help="comma-separated shot names")
    sub.add_parser("social", help="assets/social-preview.png, from the dark hero")
    f = sub.add_parser("frame")
    f.add_argument("--base-url", help="public URL serving docs/shots/ (or SCREENFRAMED_BASE_URL)")
    f.add_argument("--background", default=os.environ.get("SCREENFRAMED_BACKGROUND", "midnight"))
    f.add_argument("--device", default=os.environ.get("SCREENFRAMED_DEVICE"))
    f.add_argument("--only", help="comma-separated shot names")
    r = sub.add_parser("record")
    r.add_argument("names", nargs="*", default=list(stories.STORIES), help="storyboards (default: all)")
    r.add_argument("--mode", choices=("littledemo", "local"), default="littledemo")
    r.add_argument("--upload", action="store_true", help="host the LittleDemo recording (uses the account)")
    u = sub.add_parser("upload", help="host recordings already in assets/demos on LittleDemo")
    u.add_argument("names", nargs="*", default=list(stories.STORIES))
    args = p.parse_args(argv)
    only = set(args.only.split(",")) if getattr(args, "only", None) else None
    if args.cmd == "html":
        render_html(only)
    elif args.cmd == "shots":
        capture_shots(only)
    elif args.cmd == "frame":
        return frame(args.base_url, background=args.background, device=args.device, only=only)
    elif args.cmd == "index":
        write_index()
    elif args.cmd == "social":
        social_preview()
    elif args.cmd == "upload":
        save_recordings([upload(n) for n in args.names])
    elif args.cmd == "record":
        save_recordings([record(n, mode=args.mode, upload=args.upload) for n in args.names])
    return 0


if __name__ == "__main__":
    sys.exit(main())
