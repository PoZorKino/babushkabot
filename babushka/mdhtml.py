"""Markdown (как его пишут LLM) -> HTML-подмножество Telegram.

Работает и на недописанном тексте: незакрытый блок кода закрывается,
незакрытые ** и прочее остаются буквальным текстом, пока не придёт пара.
"""
import html
import re

_FENCE = re.compile(r"```[ \t]*([\w+#.-]*)[ \t]*\n?(.*?)(?:```|\Z)", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.S)
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
_ITALIC_UND = re.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S)
_HEADER = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$")
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+")
_QUOTE = re.compile(r"^&gt;[ \t]?")
_HR = re.compile(r"^[ \t]*([-*_])([ \t]*\1){2,}[ \t]*$")


_TABLE_ROW = re.compile(r"^[ \t]*\|.*$")
_TABLE_SEP = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$")
TABLE_MAX_WIDTH = 42  # шире - на телефоне в <pre> начнёт переноситься


def _plain(cell: str) -> str:
    cell = _LINK.sub(r"\1", cell)
    cell = re.sub(r"(\*\*|__|~~|`)", "", cell)
    return cell.strip()


def _cells(line: str) -> list[str]:
    return [_plain(c) for c in line.strip().strip("|").split("|")]


def _render_table(rows: list[list[str]]) -> str:
    """Telegram не умеет таблицы: узкие рисуем в <pre>, широкие - карточками."""
    head, body = rows[0], rows[1:]
    cols = len(head)
    body = [(r + [""] * cols)[:cols] for r in body]
    widths = [max(len(r[i]) for r in [head] + body) for i in range(cols)]
    if sum(widths) + 3 * (cols - 1) <= TABLE_MAX_WIDTH:
        def line(r: list[str]) -> str:
            return " │ ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
        rule = "─┼─".join("─" * w for w in widths)
        text = "\n".join([line(head), rule, *map(line, body)])
        return "<pre>" + html.escape(text, quote=False) + "</pre>"
    cards = []
    for r in body:
        lines = [f"<b>{html.escape(r[0], quote=False)}</b>"]
        for h, c in zip(head[1:], r[1:]):
            if c:
                lines.append(f"{html.escape(h, quote=False)}: {html.escape(c, quote=False)}")
        cards.append("\n".join(lines))
    return "\n\n".join(cards)


def _tables(md: str, keep) -> str:
    lines, out, i = md.split("\n"), [], 0
    while i < len(lines):
        if _TABLE_ROW.match(lines[i]) and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            j = i + 2
            while j < len(lines) and _TABLE_ROW.match(lines[j]):
                j += 1
            rows = [_cells(lines[i])] + [_cells(l) for l in lines[i + 2 : j]]
            out.append(keep(_render_table(rows)))
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _inline(text: str) -> str:
    text = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _BOLD.sub(r"<b>\2</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITALIC_STAR.sub(r"<i>\1</i>", text)
    text = _ITALIC_UND.sub(r"<i>\1</i>", text)
    return text


def to_telegram_html(md: str) -> str:
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    def fence(m: re.Match) -> str:
        lang, body = m.group(1), m.group(2).rstrip("\n")
        code = html.escape(body, quote=False)
        if lang:
            return keep(f'<pre><code class="language-{html.escape(lang)}">{code}</code></pre>')
        return keep(f"<pre>{code}</pre>")

    md = md.replace("\x00", "")
    md = _FENCE.sub(fence, md)
    md = _tables(md, keep)
    md = _INLINE_CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), md)
    md = html.escape(md, quote=False)

    out: list[str] = []
    quote: list[str] = []

    def flush_quote() -> None:
        if quote:
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote.clear()

    for line in md.split("\n"):
        if _QUOTE.match(line):
            quote.append(_inline(_QUOTE.sub("", line, count=1)))
            continue
        flush_quote()
        if _HR.match(line):
            out.append("────────")
        elif h := _HEADER.match(line):
            out.append(f"<b>{_inline(h.group(1))}</b>")
        else:
            line = _BULLET.sub(lambda m: m.group(1) + "• ", line)
            out.append(_inline(line))
    flush_quote()

    result = "\n".join(out)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], result)
