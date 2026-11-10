"""A small Chrome DevTools Protocol client: screenshots and frame grabs.

Drives a local headless Chrome (``CHROME_PATH``, default the macOS app
bundle) over its debugging websocket, using aiohttp (a dependency of
textual-serve, already in the dev group). Enough for this harness: set the
viewport and device scale, load a page, wait for it to settle, screenshot an
element with a transparent page background, send keys, grab frames.

This process never imports ``ai_quota_swap``; it only looks at pages the
guarded launcher produced (files, or the loopback TUI server).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp

DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

CHROME_FLAGS = (
    "--headless=new",
    "--remote-debugging-port=0",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--disable-features=Translate,OptimizationHints,MediaRouter",
    "--no-pings",
    "--metrics-recording-only",
    "--hide-scrollbars",
    "--mute-audio",
    "--force-color-profile=srgb",
    "--font-render-hinting=none",
)


def chrome_path() -> str:
    path = os.environ.get("CHROME_PATH") or DEFAULT_CHROME
    if not (Path(path).exists() or shutil.which(path)):
        raise SystemExit(f"Chrome not found at {path!r}; set CHROME_PATH")
    return path


class Page:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: list[tuple[str, dict]] = []
        self._waiters: list[tuple[str, asyncio.Future]] = []
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if "id" in data:
                fut = self._pending.pop(data["id"], None)
                if fut is not None and not fut.done():
                    if "error" in data:
                        fut.set_exception(RuntimeError(f"CDP error: {data['error']}"))
                    else:
                        fut.set_result(data.get("result", {}))
            elif "method" in data:
                for name, fut in list(self._waiters):
                    if name == data["method"] and not fut.done():
                        fut.set_result(data.get("params", {}))
                        self._waiters.remove((name, fut))

    async def send(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self.ws.send_str(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        return await asyncio.wait_for(fut, timeout)

    def expect(self, method: str) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((method, fut))
        return fut

    async def eval(self, expression: str):
        r = await self.send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        if "exceptionDetails" in r:
            raise RuntimeError(f"page script failed: {r['exceptionDetails']}")
        return r.get("result", {}).get("value")

    async def setup(self, width: int, height: int, transparent: bool = True) -> None:
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        await self.send("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 0, "mobile": False,
        })
        # Every page acts as the focused one, as a terminal being typed in
        # is: the dashboard redraws its footer keys only while focused.
        await self.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        if transparent:
            await self.send("Emulation.setDefaultBackgroundColorOverride", {
                "color": {"r": 0, "g": 0, "b": 0, "a": 0},
            })

    async def goto(self, url: str, timeout: float = 30) -> None:
        loaded = self.expect("Page.loadEventFired")
        await self.send("Page.navigate", {"url": url})
        await asyncio.wait_for(loaded, timeout)
        await self.eval("document.fonts.ready.then(() => true)")

    async def wait_for(self, expression: str, timeout: float = 20, poll: float = 0.1) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.eval(expression):
                return
            await asyncio.sleep(poll)
        raise TimeoutError(f"page never satisfied: {expression}")

    async def rect(self, selector: str) -> dict:
        r = await self.eval(
            f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
            " if (!e) return null; const b = e.getBoundingClientRect();"
            " return {x: b.left + scrollX, y: b.top + scrollY, width: b.width, height: b.height}; })()"
        )
        if not r:
            raise RuntimeError(f"no element matches {selector!r}")
        return r

    async def screenshot(self, out: Path, selector: str | None = None, pad: int = 0) -> Path:
        params: dict = {"format": "png", "captureBeyondViewport": True}
        if selector:
            r = await self.rect(selector)
            params["clip"] = {
                "x": max(0, r["x"] - pad), "y": max(0, r["y"] - pad),
                "width": r["width"] + 2 * pad, "height": r["height"] + 2 * pad, "scale": 1,
            }
        data = await self.send("Page.captureScreenshot", params, timeout=60)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(data["data"]))
        return out

    async def press(self, key: str) -> None:
        """Type one key: a printable character, or Enter / Escape / Tab / ArrowDown..."""
        named = {
            "Enter": ("\r", 13), "Escape": ("\x1b", 27), "Tab": ("\t", 9),
            "ArrowDown": ("", 40), "ArrowUp": ("", 38), "ArrowLeft": ("", 37), "ArrowRight": ("", 39),
        }
        if key in named:
            text, code = named[key]
            base = {"key": key, "code": key, "windowsVirtualKeyCode": code}
            await self.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", **base})
            if text:
                await self.send("Input.dispatchKeyEvent", {"type": "char", "text": text, **base})
            await self.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})
        else:
            await self.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key, "text": key})
            await self.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})

    async def type_text(self, text: str) -> None:
        """Insert text the way an IME would; xterm.js forwards it to the app."""
        for ch in text:
            await self.send("Input.insertText", {"text": ch})

    async def close(self) -> None:
        self._reader.cancel()
        await self.ws.close()


@asynccontextmanager
async def chrome(scale: float = 1):
    """Start a throwaway headless Chrome; yield a factory for new pages.

    ``scale`` is the real device scale factor (``--force-device-scale-factor``).
    It is set on the browser rather than emulated per page: xterm.js's WebGL
    renderer in the TUI page draws at twice its size under an emulated scale.
    """
    profile = tempfile.mkdtemp(prefix="qswap-demo-chrome-")
    proc = subprocess.Popen(
        [chrome_path(), *CHROME_FLAGS, f"--force-device-scale-factor={scale:g}",
         f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    session = aiohttp.ClientSession()
    try:
        port_file = Path(profile) / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        while not port_file.exists() or not port_file.read_text().strip():
            if time.monotonic() > deadline or proc.poll() is not None:
                raise RuntimeError("Chrome did not start (no DevToolsActivePort)")
            await asyncio.sleep(0.05)
        port = int(port_file.read_text().split()[0])

        async def new_page() -> Page:
            async with session.put(f"http://127.0.0.1:{port}/json/new?about:blank") as r:
                target = await r.json()
            ws = await session.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0)
            return Page(ws)

        yield new_page
    finally:
        await session.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


async def shoot(url: str, out: Path, *, width: int = 1240, height: int = 900, scale: float = 2,
                selector: str = "#window", ready: str | None = None, settle: float = 0.3,
                pad: int = 0) -> Path:
    async with chrome(scale) as new_page:
        page = await new_page()
        await page.setup(width, height)
        await page.goto(url)
        if ready:
            await page.wait_for(ready)
        await asyncio.sleep(settle)
        await page.screenshot(out, selector, pad=pad)
        await page.close()
    return out


async def record_frames(url: str, steps, out_dir: Path, *, width: int, height: int,
                        scale: float = 1, fps: float = 8,
                        ready: str = "document.body.classList.contains('-first-byte')"):
    """Play a storyboard against ``url`` while grabbing frames.

    Returns ``[(seconds since start, frame path), ...]``. The local fallback
    for recordings: ``capture.py`` turns the frames into a GIF with ffmpeg.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[float, Path]] = []
    async with chrome(scale) as new_page:
        page = await new_page()
        await page.setup(width, height, transparent=False)
        await page.goto(url)
        await page.wait_for(ready)
        await asyncio.sleep(0.5)
        done = asyncio.Event()
        t0 = time.monotonic()

        async def grab() -> None:
            i = 0
            while not done.is_set():
                started = time.monotonic()
                data = await page.send("Page.captureScreenshot", {"format": "png"})
                path = out_dir / f"frame_{i:05d}.png"
                path.write_bytes(base64.b64decode(data["data"]))
                frames.append((started - t0, path))
                i += 1
                await asyncio.sleep(max(0.0, 1 / fps - (time.monotonic() - started)))

        grabber = asyncio.create_task(grab())
        for kind, value in steps:
            if kind == "pause":
                await asyncio.sleep(int(value) / 1000)
            elif kind == "type":
                await page.type_text(str(value))
            elif kind == "key":
                await page.press(str(value))
        done.set()
        await grabber
        await page.close()
    return frames
