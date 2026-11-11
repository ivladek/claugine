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


def provider():
    try:
        return (_settings().provider_name or DEFAULT).strip() or DEFAULT
    except Exception:  # no database yet (migrations, tests without tables)
        return DEFAULT


def brand(text):
    """The text with the provider's name in place of the default one."""
    name = provider()
    return text.replace(DEFAULT, name) if text and name != DEFAULT else text


def logo_version():
    s = _settings()
    return int(s.logo_updated_at.timestamp()) if s.logo and s.logo_updated_at else 0


def logo_path():
    """The logo's address on the portal ("" without a logo)."""
    from django.urls import reverse
    v = logo_version()
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
