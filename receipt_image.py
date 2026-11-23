"""The official voting receipt as an IMAGE (PNG or JPG), drawn on the SERVER with Pillow.

Same content as the on-screen receipt (templates/receipt.html), so it looks the same on every phone and laptop
and needs no internet: the bundled Inter font (static/fonts), the BotoKo logo (static/logo-512.png) and the
QR code from segno. White background, about 1080 px wide, dashed "tear-off" border.

    data = {"school": ..., "title": ..., "school_year": ..., "ballot_no": ..., "code": ..., "cast_at": ...,
            "voter_id": "V-XXX123", "picks": [{"position", "seats", "chosen": [{"name", "party"}]}],
            "verify_url": ..., "verify_base": ...}
    image_bytes = render(data, "png")      # or "jpg"

The text that is drawn is also stored in the file (PNG "Description" / JPG comment), so the receipt stays
searchable and the tests can compare it with the on-screen receipt.
"""
import io
import os
import threading

import segno
from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_LATIN = os.path.join(HERE, "static", "fonts", "inter-latin.woff2")
FONT_EXT = os.path.join(HERE, "static", "fonts", "inter-latin-ext.woff2")     # ñ is in latin; ā, ř ... here
LOGO = os.path.join(HERE, "static", "logo-512.png")

W = 1080                         # image width (px)
EDGE = 30                        # white space outside the dashed tear-off border
PAD = 70                         # border -> text
L, R = EDGE + PAD, W - EDGE - PAD
INK, MUTED, SOFT, NOVOTE = (17, 17, 17), (68, 68, 68), (85, 85, 85), (102, 102, 102)
RULE, DOTS = (119, 119, 119), (175, 175, 175)

# Characters in the "latin" Inter file (Google Fonts unicode-range); anything else uses the latin-ext file.
_LATIN = set(range(0x00, 0x100)) | set(range(0x2000, 0x2070)) | {
    0x131, 0x152, 0x153, 0x2BB, 0x2BC, 0x2C6, 0x2DA, 0x2DC, 0x304, 0x308, 0x329, 0x20AC, 0x2122, 0x2191,
    0x2193, 0x2212, 0x2215, 0xFEFF, 0xFFFD}
_local = threading.local()        # Pillow font objects are kept per thread (Waitress runs several threads)


def _font(size, weight=400, ext=False):
    cache = getattr(_local, "fonts", None)
    if cache is None:
        cache = _local.fonts = {}
    key = (size, weight, ext)
    if key not in cache:
        f = ImageFont.truetype(FONT_EXT if ext else FONT_LATIN, size)
        try:
            f.set_variation_by_axes([weight])          # Inter is a variable font: 100 (thin) .. 900 (black)
        except (OSError, AttributeError):
            pass
        cache[key] = f
    return cache[key]


def _runs(text):
    """Split text into runs that use the same font file: [[text, uses_ext], ...]."""
    out = []
    for ch in text:
        ext = ord(ch) not in _LATIN
        if out and out[-1][1] == ext:
            out[-1][0] += ch
        else:
            out.append([ch, ext])
    return out


class Paper:
    """Draws top to bottom: self.y is the top of the next line."""

    def __init__(self):
        self.img = Image.new("RGB", (W, 9000), "white")
        self.d = ImageDraw.Draw(self.img)
        self.y = EDGE + 56
        self.lines = []                                   # every text drawn, in order

    # ---- text helpers
    def width(self, text, size, weight=400, spacing=0):
        return sum(_font(size, weight, ext).getlength(t) for t, ext in _runs(text)) + spacing * max(0, len(text) - 1)

    def draw(self, x, baseline, text, size, weight=400, fill=INK, spacing=0):
        for t, ext in _runs(text):
            f = _font(size, weight, ext)
            if spacing:
                for ch in t:
                    self.d.text((x, baseline), ch, font=f, fill=fill, anchor="ls")
                    x += f.getlength(ch) + spacing
            else:
                self.d.text((x, baseline), t, font=f, fill=fill, anchor="ls")
                x += f.getlength(t)
        return x

    def wrap(self, text, size, weight=400, spacing=0, max_w=None):
        max_w = max_w or (R - L)
        words, lines, cur = text.split(), [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if self.width(trial, size, weight, spacing) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines or [""]

    def text(self, text, size, weight=400, fill=INK, align="center", spacing=0, after=0.38, record=True):
        for line in self.wrap(text, size, weight, spacing):
            w = self.width(line, size, weight, spacing)
            x = {"center": (W - w) / 2, "left": L, "right": R - w}[align]
            self.draw(x, self.y + size, line, size, weight, fill, spacing)
            self.y += size * (1 + after)
        if record:
            self.lines.append(text)

    def row(self, label, value, size=32, value_size=None, value_weight=700, value_spacing=0, note=None):
        """'Label ............ value' (label left, value right), like the dl rows on screen."""
        vs = value_size or size
        h = max(size, vs)
        self.draw(L, self.y + h, label, size, 400, MUTED)
        vw = self.width(value, vs, value_weight, value_spacing)
        self.draw(R - vw, self.y + h, value, vs, value_weight, INK, value_spacing)
        self.y += h * 1.42
        if note:
            nw = self.width(note, 24)
            self.draw(R - nw, self.y + 20, note, 24, 400, SOFT)
            self.y += 24 * 1.5
        self.lines.append(f"{label}: {value}" + (f" ({note})" if note else ""))

    # ---- lines
    def dashed(self, width=3, fill=INK, dash=14, gap=9, gap_before=18, gap_after=26):
        self.y += gap_before
        x = L
        while x < R:
            self.d.line([(x, self.y), (min(x + dash, R), self.y)], fill=fill, width=width)
            x += dash + gap
        self.y += gap_after

    def dotted(self, gap_before=12, gap_after=20):
        self.y += gap_before
        for x in range(L, R, 9):
            self.d.ellipse([x, self.y - 1.5, x + 3, self.y + 1.5], fill=DOTS)
        self.y += gap_after


def _logo(size):
    logo = Image.open(LOGO).convert("RGBA")
    return logo.resize((size, size), Image.LANCZOS)


def _qr(url, px):
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=10, border=2, dark="#111111", light="#ffffff")
    buf.seek(0)
    return Image.open(buf).convert("RGB").resize((px, px), Image.NEAREST)


def render(data, fmt="png"):
    """The receipt image as bytes (fmt = "png" or "jpg")."""
    p = Paper()
    # 1. header: logo, school, "Student Council Election", event, school year
    size = 170
    p.img.paste(_logo(size), ((W - size) // 2, int(p.y)), _logo(size))
    p.y += size + 14
    bw = p.width("Boto", 40, 800) + p.width("Ko", 40, 800)
    x = p.draw((W - bw) / 2, p.y + 40, "Boto", 40, 800, INK)
    p.draw(x, p.y + 40, "Ko", 40, 800, (74, 52, 204))                # "Ko" in the brand violet, like the header
    p.y += 40 * 1.55
    p.lines.append("BotoKo")
    if data.get("school"):
        p.text(data["school"].upper(), 36, 800, spacing=1, after=0.3)
    p.text("STUDENT COUNCIL ELECTION", 27, 700, MUTED, spacing=3, after=0.45)
    p.text(data["title"], 44, 700, after=0.3)
    p.text(f"School Year {data['school_year']}", 29, 400, MUTED, after=0.2)
    # 2. title between dashed lines
    p.dashed(width=4, gap_before=26, gap_after=26)
    p.text("OFFICIAL VOTING RECEIPT", 40, 800, spacing=6, after=0.15)
    p.dashed(width=4, gap_before=16, gap_after=30)
    # 3. receipt details
    p.row("Receipt / Ballot No.", data["ballot_no"])
    p.row("Verification code", data["code"], value_size=40, value_weight=800, value_spacing=6)
    p.row("Date & time voted", data["cast_at"], note="Philippine time (Asia/Manila)")
    p.row("Voter ID", data["voter_id"])
    # 4. votes cast
    p.dashed(width=2, fill=RULE, gap_before=10, gap_after=30)
    p.text("VOTES CAST", 32, 800, align="left", spacing=4, after=0.5)
    picks = data["picks"]
    for i, pick in enumerate(picks):
        label = pick["position"].upper() + (f"  (VOTE UP TO {pick['seats']})" if pick["seats"] > 1 else "")
        p.text(label, 25, 600, SOFT, align="left", spacing=1.5, after=0.45, record=False)
        if pick["chosen"]:
            names = []
            for c in pick["chosen"]:
                party = f" ({c['party']})" if c.get("party") else ""
                nw = p.width(c["name"], 38, 800)
                if party and nw + p.width(party, 29) <= R - L:
                    p.draw(L, p.y + 38, c["name"], 38, 800, INK)
                    p.draw(L + nw, p.y + 38, party, 29, 400, MUTED)
                    p.y += 38 * 1.38
                else:
                    p.text(c["name"], 38, 800, align="left", after=0.3, record=False)
                    if party:
                        p.text(party.strip(), 29, 400, MUTED, align="left", after=0.35, record=False)
                names.append(c["name"] + party)
            p.lines.append(f"{pick['position']}" + (f" (vote up to {pick['seats']})" if pick["seats"] > 1 else "")
                           + ": " + "; ".join(names))
        else:
            p.text("NO VOTE", 34, 700, NOVOTE, align="left", spacing=5, after=0.35, record=False)
            p.lines.append(f"{pick['position']}" + (f" (vote up to {pick['seats']})" if pick["seats"] > 1 else "")
                           + ": NO VOTE")
        if i < len(picks) - 1:
            p.dotted()
    # 5. totals
    voted = sum(1 for x in picks if x["chosen"])
    p.dashed(width=2, fill=RULE, gap_before=20, gap_after=28)
    p.row("Positions voted", str(voted))
    p.row("Positions skipped", str(len(picks) - voted))
    p.row("Total positions", str(len(picks)))
    # 6. QR code (left) + how to verify (right)
    p.dashed(width=2, fill=RULE, gap_before=14, gap_after=30)
    qr_px = 270
    top = int(p.y)
    p.img.paste(_qr(data["verify_url"], qr_px), (L, top))
    tx, tw = L + qr_px + 34, R - (L + qr_px + 34)
    blocks = [("Scan to verify, or open", 27, 400, MUTED), (data["verify_base"], 27, 700, INK),
              ("and enter the verification code.", 27, 400, MUTED)]
    lines = [(ln, s, wt, col) for t, s, wt, col in blocks for ln in p.wrap(t, s, wt, max_w=tw)]
    text_h = len(lines) * 27 * 1.4
    ty = top + max(0, (qr_px - text_h) / 2)
    for ln, s, wt, col in lines:
        p.draw(tx, ty + s, ln, s, wt, col)
        ty += s * 1.4
    p.lines.append("Scan to verify, or open " + data["verify_base"] + " and enter the verification code.")
    p.y = top + qr_px + 26
    # 7. footer
    p.dashed(width=2, fill=RULE, gap_before=8, gap_after=34)
    p.text("Thank you for voting.", 34, 700, after=0.35)
    p.text("Keep this receipt for verification.", 30, 400, MUTED, after=0.2)
    bottom = int(p.y + 46)
    # dashed tear-off border around the receipt
    d, x0, x1, y0, y1 = p.d, EDGE, W - EDGE, EDGE, bottom
    for x in range(x0, x1, 26):
        d.line([(x, y0), (min(x + 15, x1), y0)], fill=(140, 140, 140), width=3)
        d.line([(x, y1), (min(x + 15, x1), y1)], fill=(140, 140, 140), width=3)
    for y in range(y0, y1, 26):
        d.line([(x0, y), (x0, min(y + 15, y1))], fill=(140, 140, 140), width=3)
        d.line([(x1, y), (x1, min(y + 15, y1))], fill=(140, 140, 140), width=3)
    img = p.img.crop((0, 0, W, bottom + EDGE))
    description = "\n".join(p.lines)
    out = io.BytesIO()
    if fmt == "jpg":
        img.save(out, "JPEG", quality=92, optimize=True, dpi=(216, 216), comment=description.encode("utf-8"))
    else:
        info = PngImagePlugin.PngInfo()
        info.add_itxt("Title", "BotoKo official voting receipt")
        info.add_itxt("Description", description)
        img.save(out, "PNG", pnginfo=info, dpi=(216, 216), compress_level=6)
    return out.getvalue()
