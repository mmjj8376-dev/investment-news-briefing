"""
Converts output/briefing.md (whichever briefing ended up there -- Copilot's
or the rule-based fallback) into output/briefing.html so links are actual
clickable <a> tags. Some mail clients (including corporate webmail) don't
auto-linkify bare URLs in a plain-text email body, which left every "원문
링크: https://..." line unclickable -- this runs after the briefing is
final and before the email step, so the email can carry both a plain-text
body and this HTML version.
"""
import html
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SRC = BASE_DIR / "output" / "briefing.md"
DEST = BASE_DIR / "output" / "briefing.html"

URL_PATTERN = re.compile(r"(https?://[^\s<]+)")


def linkify(escaped_text):
    return URL_PATTERN.sub(r'<a href="\1">\1</a>', escaped_text)


def render_line(line):
    stripped = line.strip()
    if stripped.startswith("### "):
        return "heading3", linkify(html.escape(stripped[4:]))
    if stripped.startswith("## "):
        return "heading2", linkify(html.escape(stripped[3:]))
    if stripped.startswith("# "):
        return "heading1", linkify(html.escape(stripped[2:]))
    if stripped in ("---", "***"):
        return "rule", ""
    if not stripped:
        return "blank", ""
    if stripped.startswith("- ") or stripped.startswith("* "):
        return "bullet", linkify(html.escape(stripped[2:]))
    return "text", linkify(html.escape(stripped))


def render(markdown_text):
    out = [
        '<div style="font-family: -apple-system, Segoe UI, Arial, sans-serif; '
        'font-size: 14px; line-height: 1.6; color: #1a1a1a;">'
    ]
    in_list = False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw_line in markdown_text.splitlines():
        kind, content = render_line(raw_line)
        if kind == "bullet":
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{content}</li>")
            continue
        close_list()
        if kind == "heading1":
            out.append(f"<h1>{content}</h1>")
        elif kind == "heading2":
            out.append(f"<h2>{content}</h2>")
        elif kind == "heading3":
            out.append(f"<h3>{content}</h3>")
        elif kind == "rule":
            out.append("<hr>")
        elif kind == "blank":
            out.append("<br>")
        else:
            out.append(f"<p>{content}</p>")
    close_list()
    out.append("</div>")
    return "\n".join(out)


def main():
    markdown_text = SRC.read_text(encoding="utf-8") if SRC.exists() else ""
    DEST.write_text(render(markdown_text), encoding="utf-8")


if __name__ == "__main__":
    main()
