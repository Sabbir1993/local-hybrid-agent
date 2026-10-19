import html as _html
import re
from .dom_theme import render_doc_page, render_slides_page

_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")


def normalize_tables(content: str) -> str:
    """Models often write a table with blank lines between its rows (or no blank line before it), which no
    Markdown parser treats as one table. Join the rows and make sure a table starts its own block."""
    lines = content.splitlines()
    out = []
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:                                        # code is left exactly as written
            out.append(line)
            continue
        if not line.strip():
            prev = next((l for l in reversed(out) if l.strip()), "")
            nxt = next((l for l in lines[i + 1:] if l.strip()), "")
            if _TABLE_ROW.match(prev) and _TABLE_ROW.match(nxt):
                continue                                    # blank line inside a table
        elif _TABLE_ROW.match(line) and out and out[-1].strip() and not _TABLE_ROW.match(out[-1]):
            out.append("")                                  # a table cannot interrupt a paragraph
        out.append(line)
    return "\n".join(out)


_DOC_CLASSES = (
    (r"<h1>", '<h1 class="doc-h1">'), (r"<h2>", '<h2 class="doc-h2">'),
    (r"<h[3-6]>", '<h3 class="doc-h3">'), (r"<p>", '<p class="doc-p">'),
    (r"<ul>", '<ul class="doc-list">'), (r"<ol( start=\"\d+\")?>", r'<ol class="doc-ordered"\1>'),
    (r"<hr ?/?>", '<hr class="doc-hr"/>'), (r"<blockquote>", '<blockquote class="doc-quote">'),
    (r"<table>", '<table class="doc-table">'), (r"<pre><code", '<pre class="code-block"><code'),
    (r'(?<!<pre>)(?<!<pre class="code-block">)<code>', '<code class="inline-code">'),   # not the <code> inside a fence
    (r"<li>\[ \] ", "<li>"), (r"<li>\[[xX]\] ", "<li>✓ "),      # task boxes mean nothing in a document
)


_CALLOUT_ICONS = {
    "NOTE": ("ℹ️", "Note"),
    "TIP": ("💡", "Tip"),
    "IMPORTANT": ("❗", "Important"),
    "WARNING": ("⚠️", "Warning"),
    "CAUTION": ("🛑", "Caution"),
}


def _convert_callouts(html: str) -> str:
    def _sub(m):
        kind = m.group(1).upper()
        icon, title = _CALLOUT_ICONS.get(kind, ("ℹ️", kind.title()))
        body = m.group(2).strip()
        # if the body already starts with a paragraph tag or contains one, preserve it
        prefix = '' if body.startswith('<p') else '<p class="doc-p">'
        suffix = '' if body.startswith('<p') else '</p>'
        return (f'<div class="doc-callout doc-callout-{kind.lower()}">'
                f'<div class="doc-callout-header"><span class="doc-callout-icon">{icon}</span> '
                f'<span class="doc-callout-title">{title}</span></div>'
                f'{prefix}{body}{suffix}</div>')
    return re.sub(
        r'<blockquote class="doc-quote">\s*<p class="doc-p">\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*(?:<br\s*/?>)?\s*([\s\S]*?)</blockquote\s*>',
        _sub,
        html,
        flags=re.IGNORECASE
    )


def _convert_kpis(html_text: str) -> str:
    """Convert fenced kpis/stats code blocks into responsive .doc-kpi-grid."""
    def _sub(m):
        raw = m.group(1).strip()
        cards = []
        for line in raw.splitlines():
            line = line.strip().lstrip("-*• ")
            if not line:
                continue
            parts = [p.strip() for p in line.split("|")]
            val = parts[0] if len(parts) > 0 else ""
            delta = parts[1].replace("^", "↑ ") if len(parts) > 1 else ""
            label = parts[2] if len(parts) > 2 else ""
            if not label and delta:
                label, delta = delta, ""
            delta_html = f'<div class="doc-kpi-delta">{_html.escape(delta)}</div>' if delta else ""
            label_html = f'<div class="doc-kpi-label">{_html.escape(label)}</div>' if label else ""
            cards.append(
                f'<div class="doc-kpi-card">'
                f'<div class="doc-kpi-val">{_html.escape(val)}</div>'
                f'{delta_html}{label_html}</div>'
            )
        return f'<div class="doc-kpi-grid">{"".join(cards)}</div>' if cards else m.group(0)

    return re.sub(r'<pre class="code-block"><code class="language-(?:kpis?|stats?)">([\s\S]*?)</code></pre>', _sub, html_text)


def markdown_to_body_html(content: str):
    """Document body HTML from Markdown (GitHub tables, nested lists, quotes, ...) with the doc theme's classes,
    or None when markdown-it is not installed (the caller then uses the simple converter). Raw HTML in the
    text is escaped, never passed through."""
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        return None
    md = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(["table", "strikethrough"])
    html = md.render(normalize_tables(content))
    for pat, rep in _DOC_CLASSES:
        html = re.sub(pat, rep, html)
    html = _convert_callouts(html)
    html = _convert_kpis(html)
    return html


_IMAGE_LINE = re.compile(r"^\s*(?:image|picture|photo)\s*:\s*(.+?)\s*$", re.I)
_CHART_LINE = re.compile(r"^\s*(?:chart|graph)\s*:\s*(.*?)\s*$", re.I)
_FIGURE = re.compile(r'<p class="doc-p"><img src="(data:image/[a-z]+;base64,[A-Za-z0-9+/=]+)" alt="([^"]*)"\s*/?></p>')


def image_specs(content: str) -> list:
    """The `image: search words | ...` lines of a PDF markdown, in order, as parsed specs (None for an empty one).
    Code fences are skipped. The same order is used by prepare_images / the resolver."""
    from ..doc_ops.pptx_ops import outline as O
    out, fence = [], False
    for line in content.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        m = None if fence else _IMAGE_LINE.match(line)
        if m:
            out.append(O.parse_image_spec(m.group(1)))
    return out


def prepare_images(content: str, images: dict | None):
    """Replace each `image:` line with a figure token (or drop it when no picture was found), in order.
    -> (markdown, {token index: image info})"""
    from ..doc_ops.pptx_ops import outline as O
    images = images or {}
    out, fence, n, used = [], False, -1, {}
    for line in content.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            continue
        m = None if fence else _IMAGE_LINE.match(line)
        if not m:
            out.append(line)
            continue
        n += 1
        info = images.get(n)
        spec = O.parse_image_spec(m.group(1)) or {}
        if info and info.get("data"):
            alt = " - ".join(b for b in (spec.get("caption"), info.get("credit_host")) if b) or spec.get("query", "")
            alt = re.sub(r"[\[\]()\"<>]", "", alt)[:200]
            out += ["", f"![{alt}](pdfimg:{n})", ""]
            used[n] = info
    return "\n".join(out), used


def embed_images(html_text: str, used: dict) -> str:
    """pdfimg:N -> a data: URI (the print page's CSP allows only data: images), each in a captioned <figure>."""
    import base64
    for n, info in used.items():
        data = info["data"]
        mime = "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/png"
        uri = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        html_text = html_text.replace(f"pdfimg:{n}", uri)
    def fig(m):
        alt = m.group(2)
        cap = f"<figcaption>{alt}</figcaption>" if alt else ""
        return f'<figure class="doc-fig"><img src="{m.group(1)}" alt="{alt}"/>{cap}</figure>'
    return _FIGURE.sub(fig, html_text)


def extract_charts(content: str):
    """`chart: bar | unit=%` followed by `- Label: number` bullets or a markdown table -> a token paragraph (or, when the
    numbers do not make a chart, just the plain list). -> (markdown, [svg strings])"""
    from ..doc_ops.pptx_ops import outline as O
    from .pdf_charts import chart_svg
    lines = content.splitlines()
    out, charts, i, fence = [], [], 0, False
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            i += 1
            continue
        m = None if fence else _CHART_LINE.match(line)
        if not m:
            out.append(line)
            i += 1
            continue
        spec = O.parse_chart_spec(m.group(1))
        i += 1
        bullets, rows = [], []
        while i < len(lines) and lines[i].strip():
            t = lines[i].strip()
            if t.startswith("|"):
                if not re.match(r"^\|?\s*:?-{2,}", t):
                    rows.append([c.strip() for c in t.strip("|").split("|")])
            elif re.match(r"^[-*+]\s+", t):
                bullets.append(re.sub(r"^[-*+]\s+", "", t))
            else:
                break
            i += 1
        data = O.chart_data({"chart": spec, "bullets": bullets, "table": rows})
        if data:
            charts.append(chart_svg(data))
            out += ["", f"PDFCHARTTOKEN{len(charts) - 1}", ""]
        else:                                   # not real numbers: keep the content, drop the chart line
            out += [("- " + b) for b in bullets] + ([("| " + " | ".join(r) + " |") for r in rows] if rows else [])
    return "\n".join(out), charts


def embed_charts(html_text: str, charts: list) -> str:
    for n, svg in enumerate(charts):
        html_text = html_text.replace(f'<p class="doc-p">PDFCHARTTOKEN{n}</p>', f'<figure class="doc-chart">{svg}</figure>')
        html_text = html_text.replace(f"PDFCHARTTOKEN{n}", f'<figure class="doc-chart">{svg}</figure>')
    return html_text


def _markdown_to_html_dom(content: str, title: str = "", is_slides: bool = False, images: dict | None = None) -> str:
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def inline_format(s: str) -> str:
        s = esc(s)
        s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
        s = re.sub(r'__(.+?)__', r'<strong>\1</strong>', s)
        s = re.sub(r'\*(.+?)\*', r'<em>\1</em>', s)
        s = re.sub(r'_(.+?)_', r'<em>\1</em>', s)
        s = re.sub(r'`([^`]+)`', r'<code class="inline-code">\1</code>', s)
        return s

    clean_title = title or "Document"
    lines = content.strip().splitlines()

    if is_slides:
        slides = []
        cur_slide = {"title": "", "lines": []}
        for l in lines:
            trimmed = l.strip()
            if trimmed.startswith("---") or (trimmed.startswith("# ") and (cur_slide["title"] or cur_slide["lines"])):
                if cur_slide["title"] or cur_slide["lines"]:
                    slides.append(cur_slide)
                cur_slide = {"title": trimmed[2:].strip() if trimmed.startswith("# ") else "", "lines": []}
            elif trimmed.startswith("# "):
                cur_slide["title"] = trimmed[2:].strip()
            else:
                cur_slide["lines"].append(l)
        if cur_slide["title"] or cur_slide["lines"]:
            slides.append(cur_slide)
        if not slides:
            slides = [{"title": clean_title, "lines": [content]}]

        slides_html = []
        for idx, s in enumerate(slides, 1):
            s_title = inline_format(s["title"] or f"Slide {idx}")
            body_parts = []
            in_ul = False
            for line in s["lines"]:
                t = line.strip()
                if not t:
                    if in_ul:
                        body_parts.append("</ul>")
                        in_ul = False
                    continue
                if t.startswith("- ") or t.startswith("* "):
                    if not in_ul:
                        body_parts.append('<ul class="slide-list">')
                        in_ul = True
                    body_parts.append(f"<li>{inline_format(t[2:])}</li>")
                elif t.startswith("## ") or t.startswith("### "):
                    if in_ul:
                        body_parts.append("</ul>")
                        in_ul = False
                    sub = t.lstrip("#").strip()
                    body_parts.append(f'<h3 class="slide-subtitle">{inline_format(sub)}</h3>')
                else:
                    if in_ul:
                        body_parts.append("</ul>")
                        in_ul = False
                    body_parts.append(f'<p class="slide-text">{inline_format(t)}</p>')
            if in_ul:
                body_parts.append("</ul>")

            slides_html.append(f"""
            <section class="slide">
              <div class="slide-header">
                <h2 class="slide-title">{s_title}</h2>
                <span class="slide-num">{idx:02d} / {len(slides):02d}</span>
              </div>
              <div class="slide-body">
                {''.join(body_parts)}
              </div>
              <div class="slide-footer">
                <span>{esc(clean_title)}</span>
              </div>
            </section>
            """)

        return render_slides_page(esc(clean_title), slides_html)

    # Standard Document mode: `image:` lines and `chart:` blocks become figures (or vanish when there is no picture)
    content, used_images = prepare_images(content, images)
    content, charts = extract_charts(content)
    body_html = markdown_to_body_html(content.strip())
    if body_html is not None:
        body_html = embed_charts(embed_images(body_html, used_images), charts)
        return render_doc_page(esc(clean_title), [body_html])
    body_elements = []
    in_code_block = False
    code_lang = ""
    code_buf = []
    in_ul = False
    in_ol = False
    table_buf = []

    def flush_table():
        nonlocal table_buf
        if not table_buf:
            return
        rows = []
        for tr in table_buf:
            cells = [c.strip() for c in tr.split("|")]
            if tr.startswith("|") and cells and cells[0] == "":
                cells.pop(0)
            if tr.endswith("|") and cells and cells[-1] == "":
                cells.pop()
            if cells:
                rows.append(cells)
        table_buf = []
        if not rows:
            return

        has_header = False
        if len(rows) >= 2 and all(re.match(r"^[\s\-:]+$", c) for c in rows[1]):
            header_row = rows[0]
            data_rows = rows[2:]
            has_header = True
        else:
            header_row = []
            data_rows = rows

        tbl_html = ['<table class="doc-table">']
        if has_header:
            tbl_html.append("<thead><tr>")
            for h in header_row:
                tbl_html.append(f"<th>{inline_format(h)}</th>")
            tbl_html.append("</tr></thead>")
        if data_rows:
            tbl_html.append("<tbody>")
            for r in data_rows:
                tbl_html.append("<tr>")
                for c in r:
                    tbl_html.append(f"<td>{inline_format(c)}</td>")
                tbl_html.append("</tr>")
            tbl_html.append("</tbody>")
        tbl_html.append("</table>")
        body_elements.append("".join(tbl_html))

    for line in lines:
        trimmed = line.strip()

        if trimmed.startswith("```"):
            flush_table()
            if in_code_block:
                escaped_code = esc("\n".join(code_buf))
                lang = f' class="language-{code_lang}"' if code_lang else ""
                body_elements.append(f'<pre class="code-block"><code{lang}>{escaped_code}</code></pre>')
                code_buf = []
                in_code_block = False
            else:
                in_code_block = True
                code_lang = trimmed[3:].strip()
            continue

        if in_code_block:
            code_buf.append(line)
            continue

        if trimmed.startswith("|") and trimmed.endswith("|"):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            table_buf.append(trimmed)
            continue
        elif table_buf:
            flush_table()

        if not trimmed:
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            continue

        if trimmed.startswith("# "):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            htext = inline_format(trimmed[2:].strip())
            body_elements.append(f'<h1 class="doc-h1">{htext}</h1>')
        elif trimmed.startswith("## "):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            htext = inline_format(trimmed[3:].strip())
            body_elements.append(f'<h2 class="doc-h2">{htext}</h2>')
        elif trimmed.startswith("### "):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            htext = inline_format(trimmed[4:].strip())
            body_elements.append(f'<h3 class="doc-h3">{htext}</h3>')
        elif trimmed.startswith("---") or trimmed.startswith("***"):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            body_elements.append('<hr class="doc-hr"/>')
        elif trimmed.startswith("> "):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            qtext = inline_format(trimmed[2:].strip())
            body_elements.append(f'<blockquote class="doc-quote">{qtext}</blockquote>')
        elif trimmed.startswith("- ") or trimmed.startswith("* "):
            if in_ol: body_elements.append("</ol>"); in_ol = False
            if not in_ul:
                body_elements.append('<ul class="doc-list">')
                in_ul = True
            body_elements.append(f'<li>{inline_format(trimmed[2:].strip())}</li>')
        elif re.match(r'^\d+\.\s+', trimmed):
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if not in_ol:
                body_elements.append('<ol class="doc-ordered">')
                in_ol = True
            m = re.match(r'^\d+\.\s+(.*)', trimmed)
            body_elements.append(f'<li>{inline_format(m.group(1).strip() if m else trimmed)}</li>')
        else:
            if in_ul: body_elements.append("</ul>"); in_ul = False
            if in_ol: body_elements.append("</ol>"); in_ol = False
            ptext = inline_format(trimmed)
            body_elements.append(f'<p class="doc-p">{ptext}</p>')

    if in_code_block and code_buf:
        escaped_code = esc("\n".join(code_buf))
        body_elements.append(f'<pre class="code-block"><code>{escaped_code}</code></pre>')
    if table_buf:
        flush_table()
    if in_ul:
        body_elements.append("</ul>")
    if in_ol:
        body_elements.append("</ol>")

    return render_doc_page(esc(clean_title), [embed_charts(embed_images(e, used_images), charts) for e in body_elements])
