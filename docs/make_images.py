"""Regenerate the README images. Run from the repo root: python docs/make_images.py

The terminal text is copied from the format strings in cli/progress.py,
cli/app.py and cli/upload.py, so the screenshots stay faithful. If you change
what the tool prints, change it here too.
"""

from PIL import Image, ImageDraw, ImageFont

BG = (13, 17, 23)  # GitHub dark, so the images sit well in both README themes
CARD = (22, 27, 34)
BORDER = (48, 54, 61)
FG = (201, 209, 217)
BRIGHT = (230, 237, 243)
DIM = (139, 148, 158)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
YELLOW = (210, 168, 84)
BLUE = (88, 166, 255)
CYAN = (57, 197, 207)
PURPLE = (188, 140, 255)
ORANGE = (255, 138, 76)

MONO = "/System/Library/Fonts/Menlo.ttc"
SANS = "/System/Library/Fonts/Supplemental/Arial.ttf"
SANS_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
PROBE = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def span(text, colour=FG, bold=False):
    return (text, colour, bold)


def terminal(path, lines, title, width_chars=86, size=26):
    """One terminal window: chrome bar, then coloured spans, one row per line."""
    plain = ImageFont.truetype(MONO, size, index=0)
    bold = ImageFont.truetype(MONO, size, index=1)
    pad, bar, line_h = 28, 44, 38

    width = int(PROBE.textlength("M", font=plain) * width_chars) + pad * 2
    height = bar + pad + line_h * len(lines) + pad
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, width, bar], fill=CARD)
    d.line([(0, bar), (width, bar)], fill=BORDER)
    for i, colour in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([pad + i * 26 - 8, bar // 2 - 8, pad + i * 26 + 8, bar // 2 + 8], fill=colour)
    d.text(
        ((width - PROBE.textlength(title, font=plain)) / 2, bar // 2 - size // 2 - 2),
        title,
        font=plain,
        fill=DIM,
    )

    y = bar + pad
    for row in lines:
        x = pad
        for text, colour, is_bold in row:
            face = bold if is_bold else plain
            d.text((x, y), text, font=face, fill=colour)
            x += d.textlength(text, font=face)
        y += line_h

    img.save(path)
    print("%-20s %dx%d" % (path, width, height))


def scheme(path):
    """The pipeline, drawn like someone knocked it out in MS Paint.

    Flat fills, hard black outlines, no gradients or rounded corners. It sits on
    white, so it reads the same in either README theme.
    """
    big = ImageFont.truetype(SANS_BOLD, 26)
    small = ImageFont.truetype(SANS, 17)
    tag = ImageFont.truetype(SANS_BOLD, 17)

    # The classic Paint palette: pure, flat, unsubtle.
    lime, scarlet = (0, 200, 80), (255, 40, 40)
    lemon, sky, tangerine = (255, 225, 0), (0, 200, 255), (255, 140, 0)
    ink, paper = (0, 0, 0), (255, 255, 255)

    width, height = 990, 330
    img = Image.new("RGB", (width, height), paper)
    d = ImageDraw.Draw(img)

    def box(x, y, w, h, label, fill, sub=None):
        d.rectangle([x, y, x + w, y + h], fill=fill, outline=ink, width=4)
        ty = y + h / 2 - (22 if sub else 13)
        d.text((x + (w - d.textlength(label, font=big)) / 2, ty), label, font=big, fill=ink)
        if sub:
            d.text(
                (x + (w - d.textlength(sub, font=small)) / 2, ty + 30), sub, font=small, fill=ink
            )

    def arrow(x1, y1, x2, y2, label=None):
        d.line([(x1, y1), (x2 - 12, y2)], fill=ink, width=4)
        d.polygon([(x2, y2), (x2 - 16, y2 - 10), (x2 - 16, y2 + 10)], fill=ink)
        if label:
            d.text(
                ((x1 + x2) / 2 - d.textlength(label, font=tag) / 2, y1 - 32),
                label,
                font=tag,
                fill=ink,
            )

    mid, bw, bh = 165, 170, 78
    box(30, 40, 150, bh, "Spotify", lime, "tags")
    box(30, 212, 150, bh, "YouTube", scarlet, "audio")

    # Both sources meet, then one line into the tool.
    trunk = 220
    for y in (79, 251):
        d.line([(180, y), (trunk, y)], fill=ink, width=4)
    d.line([(trunk, 79), (trunk, 251)], fill=ink, width=4)
    arrow(trunk, mid, 268, mid)

    box(268, mid - bh // 2, bw, bh, "sc-scrape", lemon)
    arrow(438, mid, 496, mid)
    box(496, mid - bh // 2, bw, bh, "Songs/", sky, "tagged + art")
    # Wide gap here so the --upload label clears both boxes.
    arrow(666, mid, 772, mid, "--upload")
    box(772, mid - bh // 2, 188, bh, "SoundCloud", tangerine)

    img.save(path)
    print("%-20s %dx%d" % (path, width, height))


def main():
    # Line shapes: cli/progress.py __call__, cli/app.py _print_summary.
    terminal(
        "docs/photo1.png",
        [
            [
                span("$ ", DIM),
                span("sc-scrape ", FG, True),
                span('"https://open.spotify.com/album/<id>"', CYAN),
            ],
            [],
            [span("[ 1/10] "), span("✓ ", GREEN), span("Artist Name - Track One")],
            [span("[ 2/10] "), span("✓ ", GREEN), span("Artist Name - Track Two")],
            [
                span("[ 3/10] "),
                span("– ", YELLOW),
                span("Artist Name - Track Three "),
                span("(already downloaded)", DIM),
            ],
            [span("[ 4/10] "), span("✓ ", GREEN), span("Artist Name - Track Four")],
            [
                span("[ 5/10] "),
                span("✗ ", RED),
                span("Artist Name - Track Five "),
                span("— video unavailable", DIM),
            ],
            [span("[ 6/10] "), span("✓ ", GREEN), span("Artist Name - Track Six")],
            [span("[ 7/10] "), span("✓ ", GREEN), span("Artist Name - Track Seven")],
            [],
            [
                span("Example Album:  70%|"),
                span("█" * 21, GREEN),
                span("░" * 9, DIM),
                span("| 7/10 [00:24<00:10, 3.4s/track]"),
            ],
        ],
        "sc-scrape",
    )

    terminal(
        "docs/photo2.png",
        [
            [span("$ ", DIM), span("tree Songs", FG, True)],
            [],
            [span("Songs", BLUE)],
            [span("├── .download_history", BLUE)],
            [span("│   └── ", BLUE), span("Example Album.json")],
            [span("└── Example Album", BLUE)],
            [span("    ├── ", BLUE), span("Track One.mp3")],
            [span("    ├── ", BLUE), span("Track Two.mp3")],
            [span("    ├── ", BLUE), span("Track Three.mp3")],
            [span("    └── ", BLUE), span("cover.jpg", PURPLE)],
            [],
            [
                span("$ ", DIM),
                span("sc-scrape ", FG, True),
                span('"https://open.spotify.com/album/<id>"', CYAN),
            ],
            [
                span("Done: "),
                span("8 downloaded", GREEN),
                span(", "),
                span("1 skipped", YELLOW),
                span(", "),
                span("1 failed", RED),
            ],
            [span("Example Album: 10 tracks -> Songs/Example Album")],
            [span("Failed tracks written to: Songs/skipped.txt")],
        ],
        "Songs/",
    )

    # Formats: cli/upload.py _selectors_ok and _print_upload_summary.
    terminal(
        "docs/photo3.png",
        [
            [
                span("$ ", DIM),
                span("sc-scrape -d ", FG, True),
                span('"Songs/Example Album"', CYAN),
                span(" --upload --dry-run", FG, True),
            ],
            [],
            [
                span("[1/1] "),
                span("✓ ", GREEN),
                span("9/12 selectors found "),
                span("(3 only exist once files are attached)", DIM),
            ],
            [span("[1/1] Uploading Example Album… done "), span("— dry run", DIM)],
            [],
            [span("Dry run: "), span("1 set(s)", GREEN), span(", 0 skipped, 0 failed")],
            [span("Example Album -> dry run")],
            [],
            [span("# nothing was uploaded and no ledger was written", DIM)],
        ],
        "sc-scrape --dry-run",
    )

    scheme("docs/scheme.png")


if __name__ == "__main__":
    main()
