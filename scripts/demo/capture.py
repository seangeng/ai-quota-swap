"""Turn the demo harness's pages into README assets.

    uv run python scripts/demo/capture.py html        # CLI shots -> docs/shots/*.html
    uv run python scripts/demo/capture.py shots       # raw PNGs (CLI pages + served TUI)
    uv run python scripts/demo/capture.py frame       # ScreenFramed, when configured
    uv run python scripts/demo/capture.py record      # dashboard recordings (LittleDemo)
    uv run python scripts/demo/capture.py index       # docs/shots/index.html

``build.py`` runs html, shots, frame and index in order. Every qswap run
happens in a guarded ``run.py`` subprocess with its own temp home; this
process only drives Chrome, pngquant, ffmpeg and the two npx CLIs.
See scripts/demo/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
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
# Recordings: the window on its stage, sized for a README GIF.
RECORD_VIEWPORT = (1280, 760)
GIF_CROP = "1216:700:32:30"  # the window plus a little of the stage, from RECORD_VIEWPORT
TUI_QUERY = "fontsize={font}&w=1176&h={height}&theme={theme}"
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
    return out


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
        with Server(shot.scenario, shot.view, shot.theme) as server:
            url = server.url(TUI_QUERY.format(font=shot.font_size, height=shot.height, theme=shot.theme))
            asyncio.run(cdp.shoot(url, png, width=VIEWPORT[0], height=VIEWPORT[1], scale=2,
                                  ready=READY, settle=4.0))
        compress(png)
        image_page(png, SHOTS_DIR / f"{shot.name}.html", shot.caption)
        log(f"png    {png.relative_to(REPO)}  {png.stat().st_size // 1024} KB")
        out.append(png)
    return out


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


GIF_PALETTE = (
    "split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];"
    "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
)


def mp4_to_gif(mp4: Path, gif: Path, *, width: int = 960, fps: int = 12, crop: str | None = None) -> None:
    """``crop`` (ffmpeg w:h:x:y) trims the stage around the window first."""
    pre = f"crop={crop}," if crop else ""
    graph = f"{pre}fps={fps},scale={width}:-1:flags=lanczos,{GIF_PALETTE}"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4), "-vf", graph, str(gif)], check=True)


def frames_to_gif(frames: list[tuple[float, Path]], gif: Path, *, width: int = 960) -> None:
    """Variable-timing frames (from ``cdp.record_frames``) into a palette GIF."""
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "frames.txt"
        lines = []
        for i, (t, path) in enumerate(frames):
            nxt = frames[i + 1][0] if i + 1 < len(frames) else t + 1.5
            lines.append(f"file '{path}'\nduration {max(0.04, nxt - t):.3f}")
        lines.append(f"file '{frames[-1][1]}'")
        listing.write_text("\n".join(lines) + "\n")
        graph = f"scale={width}:-1:flags=lanczos,{GIF_PALETTE}"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-vf", graph, "-r", "12", str(gif)],
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


def record(name: str, *, mode: str, upload: bool) -> dict:
    """Record one storyboard. ``mode`` is ``littledemo`` (its local recorder
    drives Chrome against the loopback server) or ``local`` (frames through
    the DevTools protocol, then ffmpeg). Returns a result record."""
    story = stories.STORIES[name]
    DEMOS_DIR.mkdir(parents=True, exist_ok=True)
    gif = DEMOS_DIR / f"{name}.gif"
    result: dict = {"name": name, "scenario": story.scenario, "mode": mode}
    query = TUI_QUERY.format(font=18, height=620, theme="dark") + "&stage=1"
    # LittleDemo's local recorder over-captures: it runs the page at a third of
    # real speed and speeds the video up 3x. The engine's clock lives in the
    # server, so slow it by the same factor to keep the storyboard's pacing.
    import scenarios as _scenarios  # noqa: PLC0415

    auto = _scenarios.get(story.scenario).auto
    compression = auto.compression / LITTLEDEMO_OVERCAPTURE if (auto and mode == "littledemo") else None
    with tempfile.TemporaryDirectory(prefix=f"qswap-rec-{name}-") as tmp, \
            Server(story.scenario, story.view, compression=compression) as server:
        url = server.url(query)
        if mode == "littledemo":
            steps = stories.to_littledemo(story, Path(tmp) / "steps.json", viewport=RECORD_VIEWPORT)
            mp4 = Path(tmp) / f"{name}.mp4"
            cmd = [*LITTLEDEMO, "record", url, "--steps", str(steps), "--out", str(mp4),
                   "--viewport", f"{RECORD_VIEWPORT[0]}x{RECORD_VIEWPORT[1]}", "--mockup", "none",
                   "--no-zoom", "--json"]
            log(f"record {name}: {' '.join(cmd)}")
            r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "LITTLEDEMO_NO_TELEMETRY": "1"})
            result["record"] = {"exit": r.returncode, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()[-2000:]}
            for line in r.stderr.strip().splitlines()[-12:]:
                log(f"       | {line}")
            if r.returncode != 0 or not mp4.exists():
                raise SystemExit(f"littledemo record failed ({r.returncode}):\n{r.stderr[-2000:]}")
            mp4_to_gif(mp4, gif, crop=GIF_CROP)
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
                r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "LITTLEDEMO_NO_TELEMETRY": "1"})
                if r.returncode != 0:
                    raise SystemExit(f"littledemo upload failed ({r.returncode}):\n{r.stderr[-2000:]}")
                result["upload"] = json.loads(r.stdout)
        else:
            frames = asyncio.run(cdp.record_frames(
                url, story.steps, Path(tmp) / "frames",
                width=RECORD_VIEWPORT[0], height=RECORD_VIEWPORT[1], fps=8,
            ))
            frames_to_gif(frames, gif)
    result["gif"] = str(gif.relative_to(REPO))
    result["gifBytes"] = gif.stat().st_size
    log(f"gif    {gif.relative_to(REPO)}  {gif.stat().st_size // 1024} KB")
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
        existing[r["name"]] = {**existing.get(r["name"], {}), **r}
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


# -- index --------------------------------------------------------------------


def write_index() -> Path:
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
        rows.append(
            f"<tr><td><code>qswap {'watch' if shot.view == 'watch' else 'tui'}</code></td>"
            f"<td>{html.escape(shot.caption)}</td><td><a href=\"{shot.name}.html\">{shot.theme}</a></td>"
            f"<td><a href=\"../../assets/screenshots/raw/{shot.name}.png\">PNG</a></td></tr>"
        )
    target = SHOTS_DIR / "index.html"
    target.write_text(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>qswap demo shots</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         max-width: 960px; margin: 40px auto; padding: 0 20px; color: #242422; background: #fbfaf7; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }} p {{ color: #6d6c68; margin-top: 0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td, th {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #e2dfd8; vertical-align: top; }}
  code {{ font: 13px "SF Mono", Menlo, Consolas, monospace; white-space: nowrap; }}
  a {{ color: #954c2a; }}
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
    elif args.cmd == "upload":
        save_recordings([upload(n) for n in args.names])
    elif args.cmd == "record":
        save_recordings([record(n, mode=args.mode, upload=args.upload) for n in args.names])
    return 0


if __name__ == "__main__":
    sys.exit(main())
