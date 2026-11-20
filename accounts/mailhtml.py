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
BUTTON = (
    'style="display:inline-block;background:#0f6b55;color:#ffffff;text-decoration:none;padding:9px 16px;'
    'border-radius:8px"'
)


NAMED = re.compile(r"([^\s:,;(][^:,;<>()]*?) <([\w.+-]+@[\w-]+(?:\.[\w-]+)+)>")
TOKENS = re.compile(NAMED.pattern + "|" + URL.pattern + "|" + EMAIL.pattern)


def _inline(text):
    """Escaped text with "Name <e-mail>" as the name linked to the e-mail, addresses and e-mail addresses as links."""
    out, at = [], 0
    for m in TOKENS.finditer(text):
        out.append(escape(text[at : m.start()]))
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
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if len(lines) == 1 and URL.fullmatch(lines[0].strip()):
            u = escape(lines[0].strip())
            out.append(
                f'<p style="margin:14px 0"><a href="{u}" {BUTTON}>Open</a> '
                f'<span style="color:#6b736e;font-size:12px">{u}</span></p>'
            )
        elif (
            len(lines) > 1
            and sum(bool(FIELD.match(ln)) and not URL.match(ln.strip()) for ln in lines) >= len(lines) - 1
        ):
            items = []
            for ln in lines:
                m = FIELD.match(ln)
                items.append(
                    f"<li>{escape(m.group(1).strip())}: <strong>{_inline(m.group(2))}</strong></li>"
                    if m
                    else f"<li>{_inline(ln.strip())}</li>"
                )
            out.append('<ul style="margin:6px 0 10px 18px;padding:0">' + "".join(items) + "</ul>")
        else:
            out.append('<p style="margin:0 0 10px">' + "<br>".join(_inline(ln.strip()) for ln in lines) + "</p>")
    return "\n".join(out)


def framed(text, subject=""):
    from django.template.loader import render_to_string
    from django.utils.safestring import mark_safe

    return render_to_string("emails/_frame.html", {"content": mark_safe(body_html(text)), "subject": subject})


# Owner 2026-11-17 17:19-17:25: one template per e-mail, in HTML; the e-mail's plain-text part is made from it.
TAG = re.compile(r"(\{%.*?%\}|\{\{.*?\}\})", re.S)
LINK_VAR = re.compile(r"^\{\{\s*[\w.]*(link|url)[\w.]*\s*\}\}$")


def _esc_text(src):
    """The template source with & < > outside the template tags written as HTML; a "{{ name }} <{{ e-mail }}>" pair
    becomes the name linked to the address."""
    src = re.sub(
        r"(\{\{[^}]*\}\}) <(\{\{[^}]*\}\})>",
        lambda m: f'\0a href="mailto:{m.group(2)}" {LINK}\1{m.group(1)}\0/a\1',
        src,
    )
    out = []
    for part in TAG.split(src):
        out.append(part if TAG.fullmatch(part) else escape(part, quote=False))
    return "".join(out).replace("\0", "<").replace("\1", ">")


def html_from_text_template(src):
    """An HTML template (in the e-mail frame) from a text template: its paragraphs, "Label: value" lines as a list and
    a paragraph that is only an address as a button - the frame the text e-mails were drawn in until now."""
    body = src.replace("{% autoescape off %}", "").replace("{% endautoescape %}", "")
    body = re.sub(r"\{%\s*load\s+i18n\s*%\}", "", body)
    blocks = []
    for block in re.split(r"\n\s*\n", body.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if len(lines) == 1 and LINK_VAR.match(lines[0]):
            v = lines[0]
            blocks.append(
                f'<p style="margin:14px 0"><a class="btn" href="{v}" {BUTTON}>Open</a> '
                f'<span style="color:#6b736e;font-size:12px">{v}</span></p>'
            )
        elif len(lines) > 1 and sum(bool(FIELD.match(ln)) for ln in lines) >= len(lines) - 1:
            items = []
            for ln in lines:
                m = FIELD.match(ln)
                items.append(
                    f"<li>{_esc_text(m.group(1).strip())}: <strong>{_esc_text(m.group(2))}</strong></li>"
                    if m
                    else f"<li>{_esc_text(ln)}</li>"
                )
            blocks.append('<ul style="margin:6px 0 10px 18px;padding:0">\n' + "\n".join(items) + "\n</ul>")
        else:
            blocks.append('<p style="margin:0 0 10px">' + "<br>\n".join(_esc_text(ln) for ln in lines) + "</p>")
    return (
        '{% extends "emails/_base.html" %}{% load i18n %}{% block body %}\n' + "\n".join(blocks) + "\n{% endblock %}\n"
    )


def linkify(html_text):
    """Addresses and e-mail addresses in the text of a rendered e-mail (not in its tags or links) as links."""
    out, in_a = [], 0
    for part in re.split(r"(<[^>]+>)", html_text):
        if part.startswith("<"):
            low = part[:3].lower()
            in_a += 1 if low == "<a " or part.lower() == "<a>" else (-1 if part.lower().startswith("</a") else 0)
            out.append(part)
        elif in_a or not part.strip():
            out.append(part)
        else:
            part = URL.sub(lambda m: f'<a href="{m.group(1)}" {LINK}>{m.group(1)}</a>', part)
            out.append(
                re.sub(
                    r"(<a [^>]*>.*?</a>)|" + EMAIL.pattern,
                    lambda m: m.group(1) or f'<a href="mailto:{m.group(2)}" {LINK}>{m.group(2)}</a>',
                    part,
                )
            )
    return "".join(out)


BLOCK = {"p", "div", "ul", "ol", "li", "table", "tr", "h1", "h2", "h3", "h4", "blockquote", "pre"}
VOID = {"br", "img", "hr", "meta", "link", "input"}


def _tree(html_text):
    from html.parser import HTMLParser

    root = {"tag": "", "attrs": {}, "kids": []}

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = [root]

        def handle_starttag(self, tag, attrs):
            node = {"tag": tag, "attrs": dict(attrs), "kids": []}
            self.stack[-1]["kids"].append(node)
            if tag not in VOID:
                self.stack.append(node)

        def handle_endtag(self, tag):
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i]["tag"] == tag:
                    del self.stack[i:]
                    break

        def handle_data(self, data):
            self.stack[-1]["kids"].append(data)

    p = P()
    p.feed(html_text or "")
    p.close()
    return root


def _text(node):
    """The node as text: blocks apart by a blank line, <br> a line break."""
    if isinstance(node, str):
        return re.sub(r"\s+", " ", node)
    tag, a = node["tag"], node["attrs"]
    if tag in ("head", "style", "script", "title"):
        return ""
    if tag == "br":
        return "\n"
    if tag == "img":
        return a.get("alt") or ""
    if tag == "table":
        rows = []
        for tr in _rows(node):
            cells = [_text(td).strip() for td in tr["kids"] if isinstance(td, dict) and td["tag"] in ("td", "th")]
            cells = [c for c in cells if c]
            if any("\0" in c for c in cells):  # a layout table: its cells are blocks
                rows.append("\0".join(cells))
            else:  # a data table: a row on a line
                rows.append(" | ".join(re.sub(r"\s*\n\s*", " ", c) for c in cells))
        return "\0" + "\n".join(r for r in rows if r) + "\0"
    inner = "".join(_text(k) for k in node["kids"])
    if tag == "a":
        href, t = a.get("href") or "", inner.strip()
        if "btn" in (a.get("class") or "").split():
            return ""  # the button: the address beside it says it
        target = href[7:] if href.startswith("mailto:") else href
        return t if (not target or target in t) else (f"{t} ({target})" if t else target)
    if tag == "li":
        return "\n- " + inner.strip() + "\n"
    if tag in ("div", "tr"):
        return "\n" + inner.strip() + "\n"
    if tag in BLOCK:
        return "\0" + inner.strip() + "\0"
    return inner


def _rows(table):
    out = []
    for k in table["kids"]:
        if isinstance(k, dict):
            if k["tag"] == "tr":
                out.append(k)
            elif k["tag"] in ("thead", "tbody", "tfoot"):
                out.extend(_rows(k))
    return out


def text_of(html_text):
    """The plain-text part of an e-mail made from its HTML (owner 2026-11-17: "portal can convert html to text
    byself"): paragraphs apart, a list as "- " lines, a data table as rows of cells ("a | b"), a link as its text with
    the address after it (unless the text is the address), the button left out (the address beside it says it)."""
    text = _text(_tree(html_text))  # \0: a break between blocks (a blank line), \n: a line break
    text = re.sub(r"[ \t]*[\n\0][\s\0]*", lambda m: "\n\n" if "\0" in m.group(0) else "\n", text)
    return "\n".join(ln.strip() for ln in text.split("\n")).strip() + "\n"
