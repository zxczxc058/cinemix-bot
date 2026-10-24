"""Translate readable Markdown to Telegram HTML, validating markup before sending."""

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
        "a", "code", "pre", "blockquote", "tg-spoiler", "tg-emoji", "span"}


class TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in TAGS:
            raise ValueError("Unsupported HTML tag.")
        attrs = dict(attrs)
        if tag == "a":
            if urlparse(attrs.get("href", "")).scheme not in {"http", "https", "tg", "mailto"}:
                raise ValueError("Unsafe HTML link.")
        elif tag == "span" and attrs.get("class") != "tg-spoiler":
            raise ValueError("Unsupported span.")
        allowed = {"href"} if tag == "a" else {"class"} if tag in {"span", "code"} else {"emoji-id"} if tag == "tg-emoji" else {"expandable"} if tag == "blockquote" else set()
        if set(attrs) - allowed:
            raise ValueError("Unsupported HTML attributes.")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError("Unbalanced HTML.")

    def handle_startendtag(self, tag, attrs):
        raise ValueError("Unsupported self-closing tag.")


def format_text(text):
    if re.search(r"</?(?:b|strong|i|em|u|ins|s|strike|del|a|code|pre|blockquote|tg-spoiler|tg-emoji|span)(?:\s|>)", text, re.I):
        try:
            parser = TelegramHTML()
            parser.feed(text)
            parser.close()
            if parser.stack:
                raise ValueError("Unclosed HTML.")
            return text, "HTML"
        except ValueError:
            return text, None
    protected = []

    def hold(value):
        protected.append(value)
        return f"\x00{len(protected) - 1}\x00"

    original = text
    text = re.sub(r"\\([_*`\[\]|])", lambda m: hold(html.escape(m[1])), text)
    text = re.sub(r"```(?:[^\n`]*\n)?([\s\S]*?)```", lambda m: hold("<pre>" + html.escape(m[1]) + "</pre>"), text)
    text = re.sub(r"`([^`\n]+)`", lambda m: hold("<code>" + html.escape(m[1]) + "</code>"), text)
    text = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
        lambda m: hold(f'<a href="{html.escape(m[2], quote=True)}">{html.escape(m[1])}</a>'),
        text,
    )
    text = re.sub(r"https?://[^\s<>()]+", lambda m: hold(html.escape(m[0].replace("\\_", "_"))), text)
    if text.count("*") % 2 or text.count("_") % 2 or text.count("`") or text.count("|") % 2 or text.count("[") != text.count("]") or re.search(r"\[[^\]]*\]\(", text):
        return original, None
    text = html.escape(text)
    for pattern, tag in (
        (r"\*\*([^*\n]+)\*\*", "b"), (r"\*([^*\n]+)\*", "b"),
        (r"_([^_\n]+)_", "i"), (r"\|\|([^|\n]+)\|\|", "tg-spoiler"),
    ):
        text = re.sub(pattern, lambda m: f"<{tag}>{m[1]}</{tag}>", text)
    for index, value in reversed(list(enumerate(protected))):
        text = text.replace(f"\x00{index}\x00", value)
    if text != html.escape(original):
        return text, "HTML"
    return original, None


def prepare(text, mode=None):
    if mode == "HTML":
        try:
            parser = TelegramHTML()
            parser.feed(text)
            parser.close()
            if parser.stack:
                raise ValueError("Unclosed HTML.")
            return text, "HTML"
        except ValueError:
            return html.escape(text), "HTML"
    rendered, detected = format_text(text)
    # Escaped HTML is equivalent to plain text, including malformed markup.
    return (rendered, "HTML") if detected else (html.escape(text), "HTML")
