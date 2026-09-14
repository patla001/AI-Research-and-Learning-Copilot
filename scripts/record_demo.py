"""
Record docs/demo.gif: one learner's path through the console.

    pip install playwright pillow          # dev-only, not in requirements.txt
    python scripts/record_demo.py http://localhost:8000

Drives the installed Google Chrome (Playwright's channel="chrome", so no browser
download) through the real app, screenshots each step, and assembles a captioned
GIF. Every request carries X-Forwarded-Email for a fresh demo user - the header
Databricks Apps sets in production - so the recording starts from an empty
library no matter what else is in the database.

The app it points at needs ANTHROPIC_API_KEY for the copilot step and ideally
OPENALEX_API_KEY so saved papers pull in full text.
"""

from __future__ import annotations

import io
import os
import re
import sys
import time

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import expect, sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "demo.gif")

VIEWPORT = {"width": 1440, "height": 900}
GIF_WIDTH = 1200
CAPTION_HEIGHT = 58
GOAL = "Understand retrieval augmented generation"
PAPERS_TO_SAVE = 5

frames: list[tuple[Image.Image, int, str]] = []


def font(size: int):
    for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/System/Library/Fonts/Helvetica.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def capture(page, caption: str, ms: int = 2200) -> None:
    shot = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    frames.append((shot, ms, caption))
    print(f"  frame {len(frames):2d}: {caption}")


def build_gif() -> None:
    title_font = font(24)
    rendered = []
    for shot, ms, caption in frames:
        scale = GIF_WIDTH / shot.width
        body = shot.resize((GIF_WIDTH, round(shot.height * scale)), Image.LANCZOS)
        canvas = Image.new("RGB", (GIF_WIDTH, body.height + CAPTION_HEIGHT), "#1c2a23")
        canvas.paste(body, (0, CAPTION_HEIGHT))
        draw = ImageDraw.Draw(canvas)
        draw.text((24, CAPTION_HEIGHT // 2), caption, fill="#f7f9f5", font=title_font, anchor="lm")
        rendered.append((canvas.quantize(colors=128, method=Image.Quantize.MEDIANCUT), ms))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    first, *rest = rendered
    first[0].save(OUT, save_all=True, append_images=[f for f, _ in rest],
                  duration=[ms for _, ms in rendered], loop=0, optimize=True, disposal=2)
    print(f"wrote {OUT} ({os.path.getsize(OUT) / 1024 / 1024:.1f} MB, {len(rendered)} frames)")


def main(base: str) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
        context.set_extra_http_headers({"X-Forwarded-Email": f"demo-{int(time.time())}@example.edu"})
        page = context.new_page()
        page.set_default_timeout(60_000)

        # 1. goal
        page.goto(base)
        goal_input = page.get_by_placeholder("What do you want to learn?")
        expect(goal_input).to_be_visible()
        capture(page, "Start with a learning goal", 1800)
        goal_input.fill(GOAL)
        page.get_by_placeholder("Why, or what you already know (optional)").fill(
            "How RAG systems retrieve evidence, stay grounded, and get evaluated")
        page.get_by_label("Your level").select_option("beginner")
        capture(page, "Say what you want to learn and your level", 2200)
        page.get_by_role("button", name="Create goal").click()

        # 2. discover
        expect(page.get_by_role("button", name="Search", exact=True)).to_be_visible()
        page.get_by_role("button", name="Search", exact=True).click()
        expect(page.locator(".paper-row").first).to_be_visible()
        page.wait_for_timeout(500)
        capture(page, "Search OpenAlex for papers that match the goal", 2800)

        # 3. save papers (import: metadata, authors, open-access full text, vectors)
        for i in range(PAPERS_TO_SAVE):
            page.get_by_role("button", name="Save", exact=True).first.click()
            expect(page.get_by_role("button", name="Saved", exact=True)).to_have_count(i + 1, timeout=240_000)
            if i == 1:
                capture(page, "Save papers: stored in Lakebase and embedded with pgvector", 2200)

        page.get_by_role("tab", name=re.compile(r"^Saved")).click()
        expect(page.locator(".paper-row").first).to_be_visible()
        capture(page, "Saved papers collect under the goal", 2200)

        # 4. reading plan
        page.get_by_role("tab", name=re.compile(r"^Reading plan")).click()
        page.get_by_role("button", name="Build plan", exact=True).click()
        expect(page.locator(".stop").first).to_be_visible()
        page.wait_for_timeout(400)
        capture(page, "A reading plan ordered by citations, stage, and relevance", 3400)

        page.locator(".stop").first.get_by_role("button", name="Done", exact=True).click()
        expect(page.locator(".next-up-label")).to_contain_text("1 of")
        page.wait_for_timeout(400)
        capture(page, "Track progress and the next paper updates", 2600)

        # 5. a paper
        page.locator(".stop").nth(1).locator(".stop-title").click()
        expect(page.locator(".sheet h2")).to_be_visible()
        page.wait_for_timeout(300)
        capture(page, "Open a paper: abstract, progress, and your notes", 2400)
        page.keyboard.press("Escape")

        # 6. copilot
        chip = page.get_by_role("button", name="Compare the papers in my collection")
        if chip.count() == 0:
            print("copilot is disabled on this app (no ANTHROPIC_API_KEY) - skipping that step")
        else:
            chip.click()
            expect(page.locator(".thinking")).to_be_visible()
            capture(page, "Ask the copilot: it retrieves evidence across your papers", 1800)
            expect(page.locator(".turn-assistant").first).to_be_visible(timeout=300_000)
            page.wait_for_timeout(600)
            capture(page, "Answers cite the papers they come from", 3600)
            cite = page.locator(".cite").first
            if cite.count():
                cite.hover()
                page.wait_for_timeout(300)
                capture(page, "Every citation is checked against what was retrieved", 3600)

        browser.close()

    build_gif()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"))
