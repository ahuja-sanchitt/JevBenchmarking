"""Record a real race on the live site for social posts: MP4 + GIF in 16:9 and 9:16.

    .venv/Scripts/python scripts/record_demo.py [url] [preset] [feature]

Each run is a genuine race against the real APIs (costs a few tenths of a cent). Frames come from Chrome's
screencast at full device resolution, each with its real timestamp, so playback is real time: nothing is
sped up, cut or reordered. Output goes to media/ (gitignored).
"""
from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
import sys
import time
from pathlib import Path

import imageio_ffmpeg
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "media"
URL = sys.argv[1] if len(sys.argv) > 1 else "https://jev-support-systems.onrender.com/"
PRESET = sys.argv[2] if len(sys.argv) > 2 else "r1"
FEATURE = sys.argv[3] if len(sys.argv) > 3 else "recat"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# name -> (CSS viewport, device scale factor, output size, gif width)
FORMATS = {
    "landscape": ((1280, 720), 1.5, (1920, 1080), 960),
    "vertical": ((432, 768), 2.5, (1080, 1920), 540),
}


async def smooth_scroll_to(page, selector: str, offset: int = 16, settle: float = 1.0) -> None:
    await page.evaluate(
        """([sel, off]) => {
            const el = document.querySelector(sel);
            if (el) window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - off, behavior: 'smooth' });
        }""",
        [selector, offset],
    )
    await page.wait_for_timeout(int(settle * 1000))


class Screencast:
    """Chrome DevTools screencast: frames at physical resolution, each with a capture timestamp."""

    def __init__(self, session, size):
        self.session, self.size, self.frames = session, size, []

    async def start(self):
        self.session.on("Page.screencastFrame", self._on_frame)
        await self.session.send("Page.startScreencast", {
            "format": "jpeg", "quality": 92, "maxWidth": self.size[0], "maxHeight": self.size[1], "everyNthFrame": 1,
        })

    def _on_frame(self, ev):
        self.frames.append((ev["metadata"]["timestamp"], base64.b64decode(ev["data"])))
        asyncio.ensure_future(self.session.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]}))

    async def stop(self):
        await self.session.send("Page.stopScreencast")


async def record(pw, name: str) -> Path:
    (vw, vh), dsf, size, _ = FORMATS[name]
    browser = await pw.chromium.launch()
    ctx = await browser.new_context(viewport={"width": vw, "height": vh}, device_scale_factor=dsf, color_scheme="dark")
    page = await ctx.new_page()
    await page.goto(URL + (f"#{FEATURE}" if FEATURE == "prioritiser" else ""), wait_until="networkidle")
    await page.wait_for_selector(f"#preset option[value='{PRESET}']", state="attached")
    # The prompt-cache popup would cover the answers; the card's "Cache hit" tag still shows it.
    await page.add_style_tag(content=".toasts { display: none !important; }")
    await page.wait_for_timeout(800)

    cast = Screencast(await ctx.new_cdp_session(page), size)
    await cast.start()
    await page.wait_for_timeout(1500)  # a moment on the headline

    await smooth_scroll_to(page, ".input-panel", settle=1.2)
    await page.select_option("#preset", PRESET)
    await page.wait_for_timeout(1200)
    await smooth_scroll_to(page, ".input-panel", offset=8, settle=0.4)

    t0 = time.perf_counter()
    await page.click("#run-btn")
    if name == "vertical":  # on a phone the cards sit below the input: follow the race
        await smooth_scroll_to(page, "#run-title", settle=0.2)
    await page.wait_for_selector("#verdict-row:not([hidden])", timeout=90_000)
    print(f"  {name}: race finished in {time.perf_counter() - t0:.1f}s")
    await page.wait_for_timeout(1800)  # let both results be read

    if name == "vertical":
        await smooth_scroll_to(page, "#card-jev", settle=3.0)
        await smooth_scroll_to(page, "#verdict", offset=24, settle=3.5)
    else:
        await smooth_scroll_to(page, "#run-title", settle=2.5)
        await smooth_scroll_to(page, "#verdict-row", offset=60, settle=3.5)
    await cast.stop()
    await ctx.close()
    await browser.close()

    # Frames only arrive when the screen changes; hold each until the next one's timestamp (real time).
    tmp = OUT / f"_frames_{name}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    frames = cast.frames
    lines = []
    for i, (ts, data) in enumerate(frames):
        f = tmp / f"f{i:05d}.jpg"
        f.write_bytes(data)
        dur = (frames[i + 1][0] - ts) if i + 1 < len(frames) else 1.0
        lines += [f"file '{f.name}'", f"duration {max(dur, 0.001):.4f}"]
    lines.append(f"file '{tmp / f'f{len(frames) - 1:05d}.jpg'}'".replace(str(tmp) + "\\", "").replace(str(tmp) + "/", ""))
    (tmp / "list.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  {name}: {len(frames)} frames over {frames[-1][0] - frames[0][0]:.1f}s")
    return tmp


def convert(tmp: Path, name: str) -> tuple[Path, Path]:
    (_, _), _, (w, h), gif_w = FORMATS[name]
    mp4 = OUT / f"jev-race-{FEATURE}-{name}.mp4"
    gif = OUT / f"jev-race-{FEATURE}-{name}.gif"
    # Pad to the exact target size in the page background colour (frames can be a pixel or two short).
    vf = f"fps=30,scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0a0a0a,format=yuv420p"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"),
                    "-vf", vf, "-c:v", "libx264", "-crf", "18", "-preset", "slow", "-movflags", "+faststart", str(mp4)], check=True)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(mp4), "-vf",
                    f"fps=12,scale={gif_w}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer",
                    str(gif)], check=True)
    return mp4, gif


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    async with async_playwright() as pw:
        for name in FORMATS:
            tmp = await record(pw, name)
            mp4, gif = convert(tmp, name)
            shutil.rmtree(tmp, ignore_errors=True)
            mb = lambda p: p.stat().st_size / 1e6
            print(f"  -> {mp4.relative_to(ROOT)} ({mb(mp4):.1f} MB), {gif.relative_to(ROOT)} ({mb(gif):.1f} MB)")


if __name__ == "__main__":
    asyncio.run(main())
