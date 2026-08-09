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
    """The pipeline diagram: two sources, one chain, an optional upload."""
    title_f = ImageFont.truetype(MONO, 30, index=1)
    body_f = ImageFont.truetype(MONO, 21)
    tag_f = ImageFont.truetype(MONO, 19, index=1)
    padx, gap, card_h, margin, merge = 34, 132, 156, 60, 62

    sources = [
        ("Spotify", ["public page:", "titles, artists, cover"], GREEN),
        ("YouTube", ["the audio stream", "(yt-dlp)"], RED),
    ]
    nodes = [
        ("sc-scrape", ["match, download,", "convert, tag"], BLUE),
        ("Songs/<Album>/", ["tagged audio", "+ cover.jpg"], PURPLE),
        ("SoundCloud", ["one set"], ORANGE),
    ]

    def box_w(title, subs):
        widest = max(
            [PROBE.textlength(title, font=title_f)]
            + [PROBE.textlength(s, font=body_f) for s in subs]
        )
        return int(widest) + padx * 2

    src_w = max(box_w(t, s) for t, s, _ in sources)
    widths = [box_w(t, s) for t, s, _ in nodes]
    chain_x = margin + src_w + merge + gap
    width = chain_x + sum(widths) + gap * (len(widths) - 1) + margin
    height = 580

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    def card(x, y, w, h, title, subs, accent):
        d.rounded_rectangle([x, y, x + w, y + h], radius=14, fill=CARD, outline=BORDER, width=2)
        d.rounded_rectangle([x, y + 2, x + 6, y + h - 2], radius=3, fill=accent)
        d.text(
            (x + (w - PROBE.textlength(title, font=title_f)) / 2, y + h / 2 - 34),
            title,
            font=title_f,
            fill=BRIGHT,
        )
        for i, line in enumerate(subs):
            d.text(
                (x + (w - PROBE.textlength(line, font=body_f)) / 2, y + h / 2 + 4 + i * 26),
                line,
                font=body_f,
                fill=DIM,
            )

    def arrow(x1, x2, y, label=None, colour=DIM):
        d.line([(x1, y), (x2 - 10, y)], fill=colour, width=3)
        d.polygon([(x2, y), (x2 - 13, y - 8), (x2 - 13, y + 8)], fill=colour)
        if label:
            d.text(
                ((x1 + x2) / 2 - PROBE.textlength(label, font=tag_f) / 2, y - 36),
                label,
                font=tag_f,
                fill=colour,
            )

    mid, spread = 250, 132
    top_y, bot_y = mid - spread, mid + spread
    card(margin, top_y - card_h // 2, src_w, card_h, *sources[0])
    card(margin, bot_y - card_h // 2, src_w, card_h, *sources[1])

    # Both sources join one trunk before the pipeline.
    trunk = margin + src_w + merge
    for y in (top_y, bot_y):
        d.line([(margin + src_w, y), (trunk, y)], fill=DIM, width=3)
    d.line([(trunk, top_y), (trunk, bot_y)], fill=DIM, width=3)
    arrow(trunk, chain_x, mid)

    x = chain_x
    for i, (title, subs, accent) in enumerate(nodes):
        card(x, mid - card_h // 2, widths[i], card_h, title, subs, accent)
        x += widths[i]
        if i < len(nodes) - 1:
            label, colour = ("--upload", ORANGE) if i == 1 else (None, DIM)
            arrow(x, x + gap, mid, label, colour)
            x += gap

    note = "Spotify supplies metadata only. Every byte of audio comes from YouTube."
    d.text(
        ((width - PROBE.textlength(note, font=body_f)) / 2, height - 58),
        note,
        font=body_f,
        fill=DIM,
    )
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
