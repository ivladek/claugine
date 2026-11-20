"""The provider's name and logo (owner 2026-10-29 16:00: Settings → Provider).

The name ("Example Cloud" now) is shown in the portal's header, titles and footer and in every e-mail; the texts
written with "Example Cloud" get the name from the settings when it is changed. The logo is uploaded as PNG or JPEG
and kept as PNG (at most 1200 × 300 pixels) in the database.
"""

import io

DEFAULT = "Example Cloud"
MAX = (1200, 300)


def _settings():
    from tenants.models import PortalSettings

    return PortalSettings.get()


def provider(s=None):
    try:
        return ((s or _settings()).provider_name or DEFAULT).strip() or DEFAULT
    except Exception:  # no database yet (migrations, tests without tables)
        return DEFAULT


def brand(text):
    """The text with the provider's name in place of the default one."""
    name = provider()
    return text.replace(DEFAULT, name) if text and name != DEFAULT else text


def logo_version(s=None):
    s = s or _settings()
    return int(s.logo_updated_at.timestamp()) if s.logo and s.logo_updated_at else 0


def logo_path(s=None):
    """The logo's address on the portal ("" without a logo)."""
    from django.urls import reverse

    v = logo_version(s)
    return f"{reverse('brand_logo')}?v={v}" if v else ""


def to_png(upload):
    """PNG bytes of an uploaded PNG or JPEG (scaled down to MAX). Raises ValueError for anything else."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    upload.seek(0)
    try:
        img = Image.open(upload)
        fmt = img.format
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("not an image") from exc
    if fmt not in ("PNG", "JPEG", "MPO"):
        raise ValueError(fmt or "unknown")
    img = ImageOps.exif_transpose(img)
    img.thumbnail(MAX)
    out = io.BytesIO()
    img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


# --- The default logo, the browser icon and the colours (owner 2026-11-17) -----------------------------------------
# "this is default claugine logo if no any other uploaded"; "cutting icon must happened by portal itself after
# uploading logo"; "in addition to logo adopt color scheme based on uploaded logo and offer to change it or use default
# with claugine colors". Claugine's full logo and its gear (the short logo, cut by claugine-web from the full one) are
# shipped as static files (scaled copies) and shown only while no logo is uploaded. The icon and the colours of an
# uploaded logo are worked out from it when first asked for and kept in this process (keyed by the logo's version):
# nothing is stored, so a standby node (a read-only database) shows the same.

DEFAULT_LOGO = "brand/claugine-logo.png"
DEFAULT_ICON = "brand/claugine-gear.png"
CLAUGINE = "#510259"  # the purple of Claugine's logo
SCHEMES = ("claugine", "logo", "custom")
ICON_SIZES = (16, 32, 180)
_cache = {}


def _key(s=None):
    """What the icon and the colours depend on: the logo (to the microsecond) and, for a letter icon, the name."""
    s = s or _settings()
    return (s.logo_updated_at.timestamp() if s.logo and s.logo_updated_at else 0, provider(s))


def logo_src(s=None):
    """The header's logo: the uploaded one, else Claugine's."""
    from django.templatetags.static import static

    return logo_path(s) or static(DEFAULT_LOGO)


def static_file(name):
    """The path of a shipped static file (the source folder, else the collected one)."""
    import os

    from django.conf import settings
    from django.contrib.staticfiles import finders

    return finders.find(name) or os.path.join(settings.STATIC_ROOT, name)


def _hex(c):
    return "#" + "".join(f"{v:02x}" for v in c[:3])


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def background(img):
    """None when the logo is transparent at its border, else the border's median colour (a logo on a band)."""
    if img.mode == "RGBA":
        a = img.getchannel("A")
        w, h = a.size
        border = [a.crop(b).getextrema()[0] for b in ((0, 0, w, 1), (0, h - 1, w, h), (0, 0, 1, h), (w - 1, 0, w, h))]
        if min(border) < 128:
            return None
    rgb = img.convert("RGB")
    w, h = rgb.size
    edge = []
    for b in ((0, 0, w, 1), (0, h - 1, w, h), (0, 0, 1, h), (w - 1, 0, w, h)):
        edge += list(rgb.crop(b).getdata())
    return tuple(sorted(c[i] for c in edge)[len(edge) // 2] for i in range(3))


def _ink(img, bg):
    """The logo's drawing as a 0/255 mask: what is not the background."""
    from PIL import Image, ImageChops

    if bg is None:
        return img.getchannel("A").point(lambda v: 255 if v > 40 else 0)
    rgb = img.convert("RGB")
    d = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg)).convert("L")
    return d.point(lambda v: 255 if v > 30 else 0)


def _blocks(mask, axis):
    """The inked stretches along an axis, those closer than a small gap merged (the letters of a word, the details of
    a mark)."""
    from PIL import Image

    w, h = mask.size
    n = w if axis == "x" else h
    line = mask.resize((w, 1) if axis == "x" else (1, h), Image.Resampling.BOX)
    flags = [v > 0 for v in line.getdata()]
    gap = max(4, (h if axis == "x" else w) // 100)
    out, i = [], 0
    while i < n:
        if flags[i]:
            s = i
            while i < n and flags[i]:
                i += 1
            if out and s - out[-1][1] < gap:
                out[-1][1] = i
            else:
                out.append([s, i])
        else:
            i += 1
    return out


def mark_box(img):
    """(box, background, how): the box of the logo's mark (the sign beside or above the name) or None."""
    bg = background(img)
    m = _ink(img, bg)
    w, h = m.size
    for axis in ("x", "y"):
        blocks = _blocks(m, axis)
        if len(blocks) < 2:
            continue
        s, e = blocks[0]
        box = (s, 0, e, h) if axis == "x" else (0, s, w, e)
        bb = m.crop(box).getbbox()
        if not bb:
            continue
        box = (box[0] + bb[0], box[1] + bb[1], box[0] + bb[2], box[1] + bb[3])
        bw, bh = box[2] - box[0], box[3] - box[1]
        rest = sum(e2 - s2 for s2, e2 in blocks[1:])
        if axis == "x":  # a mark stands out from the name: taller than its letters, or a wider gap after it
            heights = [b2[3] - b2[1] for b2 in (m.crop((s2, 0, e2, h)).getbbox() for s2, e2 in blocks[1:]) if b2]
            gaps = [blocks[i + 1][0] - blocks[i][1] for i in range(len(blocks) - 1)]
            later = sorted(gaps[1:]) or [0]
            taller = bool(heights) and bh >= 1.25 * max(heights)
            apart = gaps[0] >= 2 * later[len(later) // 2]
            if not (taller or apart):
                continue
        if 0.6 <= bw / bh <= 1.6 and (axis == "y" or rest > bw):
            return box, bg, ("beside" if axis == "x" else "above")
    return None, bg, ""


def _lum(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (ch(v) for v in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def colours(img, n=5):
    """The logo's distinct colours, the most present first (its background counts; transparent parts do not)."""
    from PIL import Image

    im = img.convert("RGBA")
    im.thumbnail((300, 300))
    a = im.getchannel("A")
    flat = Image.new("RGB", im.size, (255, 255, 255))
    flat.paste(im.convert("RGB"), mask=a.point(lambda v: 255 if v > 128 else 0))
    q = flat.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    hist = q.histogram(mask=a.point(lambda v: 255 if v > 128 else 0))  # the transparent parts are not the logo's
    out = []
    for _n, i in sorted(((c, i) for i, c in enumerate(hist[: len(pal) // 3]) if c), reverse=True):
        c = tuple(pal[i * 3 : i * 3 + 3])
        if all(sum(abs(x - y) for x, y in zip(c, o, strict=True)) > 60 for o in out):
            out.append(c)
    return [_hex(c) for c in out[:n]]


def main_colour(cols):
    """The colour the portal takes from a logo: its most present colour that is not near white (a white name on a
    band of colour takes the band); a logo of white only gives Claugine's."""
    import colorsys

    for h in cols:
        r, g, b = (v / 255 for v in _rgb(h))
        _h, light, _s = colorsys.rgb_to_hls(r, g, b)
        if light < 0.9:
            return h
    return CLAUGINE


def analyse(s=None):
    """{bg, box, how, colours, main, transparent} of the uploaded logo, or None without one."""
    from PIL import Image

    s = s or _settings()
    if not s.logo:
        return None
    v = _key(s)[0]
    hit = _cache.get("logo")
    if hit and hit[0] == v:
        return hit[1]
    img = Image.open(io.BytesIO(bytes(s.logo)))
    img.load()
    box, bg, how = mark_box(img)
    cols = colours(img)
    out = {"bg": bg, "box": box, "how": how, "colours": cols, "main": main_colour(cols), "transparent": bg is None}
    _cache["logo"] = (v, out)
    return out


def _font(size):
    from PIL import ImageFont

    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def _square(img, info, name):
    """The square icon (256 px): the logo's mark on its background, or the provider's first letter on the logo's main
    colour when the logo has no separate mark."""
    from PIL import Image, ImageDraw

    s = 256
    if info["box"]:
        mark = img.convert("RGBA").crop(info["box"])
        mark.thumbnail((int(s / 1.18), int(s / 1.18)), Image.Resampling.LANCZOS)
        bg = info["bg"]
        out = Image.new("RGBA", (s, s), tuple(bg) + (255,) if bg else (0, 0, 0, 0))
        out.alpha_composite(mark, ((s - mark.width) // 2, (s - mark.height) // 2))
        if bg:  # rounded, as phones show it
            r = Image.new("L", (s, s), 0)
            ImageDraw.Draw(r).rounded_rectangle([0, 0, s - 1, s - 1], radius=s // 6, fill=255)
            out.putalpha(r)
        return out
    main = _rgb(info["main"])
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=s // 6, fill=main + (255,))
    letter = (name[:1] or "C").upper()
    f = _font(170)
    b = d.textbbox((0, 0), letter, font=f)
    ink = (255, 255, 255) if contrast(main, (255, 255, 255)) >= 3 else (23, 32, 29)
    d.text(((s - (b[2] - b[0])) / 2 - b[0], (s - (b[3] - b[1])) / 2 - b[1]), letter, font=f, fill=ink)
    return out


def icon_png(size):
    """PNG bytes of the browser icon at a size: cut from the uploaded logo, else Claugine's gear."""
    from PIL import Image

    s = _settings()
    v = _key(s)
    key = ("icon", size)
    hit = _cache.get(key)
    if hit and hit[0] == v:
        return hit[1]
    info = analyse(s)
    if info:
        img = Image.open(io.BytesIO(bytes(s.logo)))
        img.load()
        sq = _square(img, info, provider(s))
    else:
        sq = Image.open(static_file(DEFAULT_ICON)).convert("RGBA")
    out = io.BytesIO()
    sq.resize((size, size), Image.Resampling.LANCZOS).save(out, "PNG", optimize=True)
    data = out.getvalue()
    _cache[key] = (v, data)
    return data


def icon_path(size, s=None):
    import zlib

    from django.urls import reverse

    stamp, name = _key(s)
    return f"{reverse('brand_icon', args=[size])}?v={int(stamp)}-{zlib.crc32(name.encode()) % 10000}"


def scheme(s=None):
    """(the scheme in force, its main colour). No choice made: the uploaded logo's colours, else Claugine's (owner
    2026-11-17: an installation that already has its own logo takes its colours from it)."""
    try:
        s = s or _settings()
    except Exception:
        return "claugine", CLAUGINE
    name = s.colour_scheme or ("logo" if s.logo else "claugine")
    if name == "custom" and s.colour_custom:
        return name, s.colour_custom
    if name == "logo":
        info = analyse(s)
        if info:
            return name, info["main"]
    return "claugine", CLAUGINE


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))


def palette(main):
    """The portal's accent colours for light and dark pages from one colour: dark enough for white text and for links
    on white, light enough for links on the dark page."""
    m = _rgb(main)
    white, dark_surface, light_ink, dark_ink = (255, 255, 255), (26, 32, 29), (23, 32, 29), (12, 21, 18)
    light = m
    for _ in range(40):
        if contrast(light, white) >= 4.5:
            break
        light = _mix(light, (0, 0, 0), 0.08)
    import colorsys

    h, lig, sat = colorsys.rgb_to_hls(*(v / 255 for v in m))
    sat = min(sat, 0.6)  # not glaring on a dark page
    dark = m
    while contrast(dark, dark_surface) < 5 and lig < 0.95:  # lighter, the same hue
        lig = min(lig + 0.03, 0.95)
        dark = tuple(round(v * 255) for v in colorsys.hls_to_rgb(h, lig, sat))
    return {
        "light": {
            "accent": _hex(light),
            "accent_ink": "#ffffff" if contrast(light, white) >= 4.5 else _hex(light_ink),
            "accent_soft": _hex(_mix(white, light, 0.11)),
        },
        "dark": {
            "accent": _hex(dark),
            "accent_ink": _hex(dark_ink) if contrast(dark, dark_ink) >= 4.5 else "#ffffff",
            "accent_soft": _hex(_mix(dark_surface, dark, 0.22)),
        },
    }


def css(s=None):
    """The :root variables of the scheme in force (light, then dark)."""
    p = palette(scheme(s)[1])

    def block(d):
        return " ".join(f"--{k.replace('_', '-')}: {v};" for k, v in d.items())

    return f":root {{ {block(p['light'])} }}\n@media (prefers-color-scheme: dark) {{ :root {{ {block(p['dark'])} }} }}"


def logo_inverts(s=None):
    """A transparent uploaded logo is inverted on dark pages (dark ink); one with its own background is not."""
    try:
        info = analyse(s)
    except Exception:
        return False
    return bool(info and info["transparent"])
