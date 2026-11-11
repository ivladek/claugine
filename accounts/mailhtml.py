"""Owner 2026-10-29: every e-mail the portal sends is HTML (with the plain text kept beside it). An e-mail with its own
HTML template uses it; the others are drawn from their text in the same frame (templates/emails/_base.html):
paragraphs, "Label: value" lines as a list, a paragraph that is only an address as a button, addresses and e-mail
addresses as links that show themselves (a link-rewriting filter changes only the target)."""
import re
from html import escape

URL = re.compile(r"(https?://[^\s<>\"']+)")
EMAIL = re.compile(r"(?<![\w.@/])([\w.+-]+@[\w-]+(?:\.[\w-]+)+)")
FIELD = re.compile(r"^\s*([^:]{1,40}):\s+(.+)$")
LINK = 'style="color:#0f6b55"'
BUTTON = ('style="display:inline-block;background:#0f6b55;color:#ffffff;text-decoration:none;padding:9px 16px;'
          'border-radius:8px"')


NAMED = re.compile(r"([^\s:,;(][^:,;<>()]*?) <([\w.+-]+@[\w-]+(?:\.[\w-]+)+)>")
TOKENS = re.compile(NAMED.pattern + "|" + URL.pattern + "|" + EMAIL.pattern)


def _inline(text):
    """Escaped text with "Name <e-mail>" as the name linked to the e-mail, addresses and e-mail addresses as links."""
    out, at = [], 0
    for m in TOKENS.finditer(text):
        out.append(escape(text[at:m.start()]))
        name, mail, url, bare = m.group(1), m.group(2), m.group(3), m.group(4)
        if mail:
            out.append(f'<a href="mailto:{escape(mail)}" {LINK}>{escape(name.strip())}</a>')
        elif url:
            out.append(f'<a href="{escape(url)}" {LINK}>{escape(url)}</a>')
        else:
            out.append(f'<a href="mailto:{escape(bare)}" {LINK}>{escape(bare)}</a>')
        at = m.end()
    out.append(escape(text[at:]))
    return "".join(out)


def body_html(text):
    out = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        if len(lines) == 1 and URL.fullmatch(lines[0].strip()):
            u = escape(lines[0].strip())
            out.append(f'<p style="margin:14px 0"><a href="{u}" {BUTTON}>Open</a> '
                       f'<span style="color:#6b736e;font-size:12px">{u}</span></p>')
        elif len(lines) > 1 and sum(bool(FIELD.match(l)) and not URL.match(l.strip()) for l in lines) >= len(lines) - 1:
            items = []
            for l in lines:
                m = FIELD.match(l)
                items.append(f"<li>{escape(m.group(1).strip())}: <strong>{_inline(m.group(2))}</strong></li>" if m
                             else f"<li>{_inline(l.strip())}</li>")
            out.append('<ul style="margin:6px 0 10px 18px;padding:0">' + "".join(items) + "</ul>")
        else:
            out.append('<p style="margin:0 0 10px">' + "<br>".join(_inline(l.strip()) for l in lines) + "</p>")
    return "\n".join(out)


def framed(text, subject=""):
    from django.template.loader import render_to_string
    from django.utils.safestring import mark_safe
    return render_to_string("emails/_frame.html", {"content": mark_safe(body_html(text)), "subject": subject})
