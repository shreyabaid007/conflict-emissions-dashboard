"""Capture dashboard pages with Playwright and stitch into an overview video with ffmpeg."""

import os
import subprocess
import time
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
OUT_DIR = "/Users/sb/Downloads/AIProject/war-emission-tracker/outputs/video_frames"
VIDEO_OUT = "/Users/sb/Downloads/AIProject/war-emission-tracker/outputs/dashboard_overview.mp4"

PAGES = [
    {"url": "/", "name": "01_dashboard", "wait": 3, "scroll_steps": 4},
    {"url": "/map", "name": "02_map", "wait": 4, "scroll_steps": 0},
    {"url": "/methodology", "name": "03_methodology", "wait": 2, "scroll_steps": 3},
    {"url": "/changelog", "name": "04_changelog", "wait": 2, "scroll_steps": 2},
]

os.makedirs(OUT_DIR, exist_ok=True)

frame_idx = 0


def capture_frame(page, name_suffix):
    global frame_idx
    path = os.path.join(OUT_DIR, f"frame_{frame_idx:04d}_{name_suffix}.png")
    page.screenshot(path=path, full_page=False)
    print(f"  captured {path}")
    frame_idx += 1
    return path


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1440, "height": 900},
        device_scale_factor=2,
        color_scheme="dark",
    )
    page = context.new_page()

    # Dismiss disclaimer modal if present
    page.goto(BASE, wait_until="load", timeout=30000)
    # Wait for Next.js hydration — look for styled header
    page.wait_for_selector("header", timeout=10000)
    time.sleep(4)
    try:
        btn = page.locator("button:has-text('I understand')")
        if btn.is_visible(timeout=3000):
            btn.click()
            time.sleep(1)
    except Exception:
        pass

    for pg in PAGES:
        url = BASE + pg["url"]
        print(f"\n--- {pg['name']}: {url} ---")
        page.goto(url, wait_until="load", timeout=30000)
        page.wait_for_selector("header", timeout=10000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        time.sleep(pg["wait"] + 2)

        # Capture top of page (duplicate frames for ~3s hold at 2fps)
        for _ in range(6):
            capture_frame(page, pg["name"] + "_top")

        # Scroll down in steps
        if pg["scroll_steps"] > 0:
            scroll_height = page.evaluate("document.body.scrollHeight")
            viewport_height = 900
            scrollable = max(0, scroll_height - viewport_height)
            step = scrollable / pg["scroll_steps"] if pg["scroll_steps"] > 0 else 0

            for s in range(1, pg["scroll_steps"] + 1):
                target = int(step * s)
                page.evaluate(f"window.scrollTo({{top: {target}, behavior: 'smooth'}})")
                time.sleep(0.8)
                # 2 frames per scroll position
                for _ in range(2):
                    capture_frame(page, f"{pg['name']}_scroll{s}")

        # Hold at bottom for a moment
        for _ in range(4):
            capture_frame(page, pg["name"] + "_bottom")

        # Transition gap (black frame via scroll back to top)
        page.evaluate("window.scrollTo({top: 0})")
        time.sleep(0.3)

    browser.close()

# Stitch frames into video with ffmpeg
print(f"\n--- Stitching {frame_idx} frames into video ---")

# Collect all frame paths in order
frames = sorted(
    [os.path.join(OUT_DIR, f) for f in os.listdir(OUT_DIR) if f.startswith("frame_") and f.endswith(".png")]
)

# Create a concat file for ffmpeg
concat_path = os.path.join(OUT_DIR, "frames.txt")
with open(concat_path, "w") as f:
    for frame in frames:
        # Each frame shown for 0.5s (2 fps)
        f.write(f"file '{frame}'\n")
        f.write("duration 0.5\n")
    # ffmpeg needs last file repeated without duration
    f.write(f"file '{frames[-1]}'\n")

cmd = [
    "ffmpeg", "-y",
    "-f", "concat", "-safe", "0",
    "-i", concat_path,
    "-vf", "scale=1440:900:flags=lanczos,format=yuv420p",
    "-c:v", "libx264",
    "-preset", "slow",
    "-crf", "18",
    "-r", "2",
    VIDEO_OUT,
]

subprocess.run(cmd, check=True)
print(f"\nVideo saved to: {VIDEO_OUT}")
print(f"Duration: ~{frame_idx * 0.5:.0f}s")
