import datetime
import html
import re

DOC_CSS = """
  @page {
    size: A4;
    margin: 20mm 18mm 20mm 18mm;
  }
  @media print {
    body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Nirmala UI", "Noto Sans Bengali", "Shonar Bangla", Vrinda, sans-serif;
    color: #1e293b;
    line-height: 1.65;
    font-size: 13.5px;
    margin: 0;
    padding: 0;
    background: #ffffff;
  }
  .doc-container {
    max-width: 820px;
    margin: 0 auto;
    padding: 24px;
  }
  .doc-header-badge {
    display: inline-block;
    background: #eff6ff;
    color: #2563eb;
    border: 1px solid #bfdbfe;
    font-size: 10.5px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 4px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 8px;
  }
  .doc-h1 {
    color: #0f172a;
    font-size: 26px;
    font-weight: 700;
    border-bottom: 2px solid #2563eb;
    padding-bottom: 10px;
    margin: 0 0 16px 0;
  }
  .doc-h2 {
    color: #1e40af;
    font-size: 17px;
    font-weight: 700;
    border-left: 4px solid #3b82f6;
    padding-left: 10px;
    margin: 24px 0 10px 0;
  }
  .doc-h3 {
    color: #334155;
    font-size: 14px;
    font-weight: 600;
    margin: 18px 0 6px 0;
  }
  .doc-p {
    color: #334155;
    margin: 0 0 12px 0;
  }
  .doc-list, .doc-ordered {
    margin: 6px 0 14px 0;
    padding-left: 22px;
    color: #334155;
  }
  .doc-list li, .doc-ordered li {
    margin-bottom: 6px;
  }
  .doc-hr {
    border: 0;
    height: 1px;
    background: #e2e8f0;
    margin: 20px 0;
  }
  .doc-quote {
    border-left: 4px solid #94a3b8;
    background: #f8fafc;
    color: #475569;
    padding: 8px 14px;
    margin: 14px 0;
    border-radius: 0 6px 6px 0;
    font-style: italic;
  }
  .doc-callout {
    border-radius: 6px;
    padding: 10px 14px;
    margin: 14px 0;
    border-left: 4px solid #3b82f6;
    background: #f8fafc;
  }
  .doc-callout-header {
    font-weight: 700;
    font-size: 12.5px;
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .doc-callout p.doc-p {
    margin-bottom: 4px;
  }
  .doc-callout-note { border-left-color: #2563eb; background: #eff6ff; }
  .doc-callout-note .doc-callout-header { color: #1e40af; }
  .doc-callout-tip { border-left-color: #10b981; background: #ecfdf5; }
  .doc-callout-tip .doc-callout-header { color: #065f46; }
  .doc-callout-important { border-left-color: #8b5cf6; background: #f5f3ff; }
  .doc-callout-important .doc-callout-header { color: #5b21b6; }
  .doc-callout-warning { border-left-color: #f59e0b; background: #fffbeb; }
  .doc-callout-warning .doc-callout-header { color: #92400e; }
  .doc-callout-caution { border-left-color: #ef4444; background: #fef2f2; }
  .doc-callout-caution .doc-callout-header { color: #991b1b; }
  .doc-table {
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0;
    font-size: 12.5px;
  }
  .doc-table th {
    background: #f1f5f9;
    color: #1e293b;
    font-weight: 600;
    text-align: left;
    padding: 8px 12px;
    border-bottom: 2px solid #cbd5e1;
    border-top: 1px solid #e2e8f0;
  }
  .doc-table td {
    padding: 8px 12px;
    border-bottom: 1px solid #e2e8f0;
    color: #475569;
  }
  .doc-table tr:nth-child(even) td {
    background: #f8fafc;
  }
  .code-block {
    background: #0f172a;
    color: #f8fafc;
    padding: 14px;
    border-radius: 6px;
    font-size: 11px;
    line-height: 1.25;
    white-space: pre;
    overflow-x: auto;
    margin: 14px 0;
  }
  .code-block code {
    background: none;
    padding: 0;
  }
  code {
    font-family: "Cascadia Code", Consolas, Monaco, monospace;
  }
  .inline-code {
    background: #f1f5f9;
    color: #0f172a;
    padding: 2px 5px;
    border-radius: 4px;
    font-size: 12px;
  }
  .doc-footer {
    margin-top: 36px;
    padding-top: 12px;
    border-top: 1px solid #e2e8f0;
    font-size: 11px;
    color: #94a3b8;
    display: flex;
    justify-content: space-between;
  }
  .doc-kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 12px;
    margin: 16px 0;
  }
  .doc-kpi-card {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 12px;
    text-align: center;
  }
  .doc-kpi-val {
    font-size: 22px;
    font-weight: 700;
    color: #1e40af;
    line-height: 1.2;
    margin-bottom: 3px;
  }
  .doc-kpi-delta {
    font-size: 11px;
    font-weight: 600;
    color: #166534;
    margin-bottom: 3px;
  }
  .doc-kpi-label {
    font-size: 11px;
    color: #475569;
  }
  .doc-badge {
    display: inline-block;
    padding: 2px 7px;
    border-radius: 4px;
    font-size: 10.5px;
    font-weight: 600;
  }
  .doc-badge-strong { background: #dcfce7; color: #166534; }
  .doc-badge-alert { background: #fee2e2; color: #991b1b; }
  .doc-badge-info { background: #dbeafe; color: #1e40af; }
  .doc-badge-neutral { background: #f1f5f9; color: #475569; }
"""


# Print rules: where a page may break, repeating table headers, wrapping code, and a cover / contents page. Kept apart from
# DOC_CSS so the live draft preview (screen) is unchanged.
PRINT_CSS = """
  @media print {
    .doc-container { max-width: none; padding: 0; }
    h1, h2, h3 { break-after: avoid; page-break-after: avoid; }
    .doc-table { break-inside: auto; }
    .doc-table thead { display: table-header-group; }
    .doc-table tr, .doc-quote, .doc-callout, figure, img, li { break-inside: avoid; page-break-inside: avoid; }
    img { max-width: 100%; height: auto; }
    .code-block { white-space: pre-wrap; overflow-wrap: anywhere; overflow: visible; }
    p { orphans: 3; widows: 3; }
  }
  .doc-fig, .doc-chart { margin: 16px 0; text-align: center; break-inside: avoid; page-break-inside: avoid; }
  .doc-fig img { max-width: 100%; max-height: 105mm; border-radius: 6px; }
  .doc-fig figcaption { font-size: 10.5px; color: #64748b; margin-top: 5px; font-style: italic; }
  .doc-chart svg { max-width: 100%; height: auto; }
  .doc-cover { display: none; }
  .doc-toc { display: none; }
  @media print {
    .doc-cover { display: flex; flex-direction: column; justify-content: center; min-height: 240mm; break-after: page;
                 page-break-after: always; padding: 0 8mm; }
    .doc-cover-title { font-size: 38px; line-height: 1.15; font-weight: 800; color: #0f172a; margin: 0 0 14px 0;
                       padding-bottom: 14px; border-bottom: 4px solid #2563eb; }
    .doc-cover-sub { font-size: 17px; line-height: 1.5; color: #475569; margin: 6px 0 28px 0; }
    .doc-cover-date { font-size: 12.5px; color: #64748b; letter-spacing: .4px; }
    .doc-toc { display: block; break-after: page; page-break-after: always; }
    .doc-toc h2 { border: 0; padding: 0; font-size: 22px; color: #0f172a; margin: 0 0 14px 0; }
    .doc-toc ol { list-style: none; counter-reset: toc; padding: 0; margin: 0; }
    .doc-toc li { counter-increment: toc; padding: 7px 0; border-bottom: 1px solid #e2e8f0; font-size: 14px; }
    .doc-toc li::before { content: counter(toc, decimal-leading-zero) "   "; color: #2563eb; font-weight: 700; }
    .doc-toc a { color: #1e293b; text-decoration: none; }
  }
"""

_BENGALI = re.compile("[\u0980-\u09FF]")
_H1 = re.compile(r'<h1 class="doc-h1">(.*?)</h1>', re.S)
_H2 = re.compile(r'<h2 class="doc-h2">(.*?)</h2>', re.S)
_P = re.compile(r'<p class="doc-p">(.*?)</p>', re.S)
_TAGS = re.compile(r"<[^>]+>")
COVER_MIN_SECTIONS = 4        # a long document gets a cover page and, from 5 sections, a contents page
TOC_MIN_SECTIONS = 5


def _css_string(text: str) -> str:
    """A string safe inside CSS content: "..." (quotes, backslashes and newlines neutralised)."""
    t = _TAGS.sub("", html.unescape(text or "")).replace("\\", " ").replace('"', "'")
    return " ".join(t.split())[:80]


def _cover_and_toc(joined: str, title: str):
    """(body html, cover html, toc html). A document with 4+ sections moves its first heading and intro paragraph to a
    cover page; 5+ sections also get a contents list (anchors only: no page numbers without scripts)."""
    sections = _H2.findall(joined)
    if len(sections) < COVER_MIN_SECTIONS:
        return joined, "", ""
    m1 = _H1.search(joined)
    head = joined[:joined.find('<h2 class="doc-h2">')]
    cover_title = m1.group(1) if m1 else html.escape(title)
    sub = ""
    pm = _P.search(head)
    if pm and len(_TAGS.sub("", pm.group(1))) <= 320:
        sub = pm.group(1)
        joined = joined.replace(pm.group(0), "", 1)
    if m1:
        joined = joined.replace(m1.group(0), "", 1)
    n = [0]

    def anchor(mm):
        n[0] += 1
        return f'<h2 class="doc-h2" id="sec{n[0]}">{mm.group(1)}</h2>'
    joined = _H2.sub(anchor, joined)
    cover = (f'<section class="doc-cover"><h1 class="doc-cover-title">{cover_title}</h1>'
             + (f'<p class="doc-cover-sub">{sub}</p>' if sub else "")
             + f'<div class="doc-cover-date">{datetime.date.today().strftime("%d %B %Y")}</div></section>')
    toc = ""
    if len(sections) >= TOC_MIN_SECTIONS:
        items = "".join(f'<li><a href="#sec{i}">{s}</a></li>' for i, s in enumerate(sections, 1))
        toc = f'<nav class="doc-toc"><h2>Contents</h2><ol>{items}</ol></nav>'
    return joined, cover, toc


def render_doc_page(escaped_title: str, body_elements: list[str]) -> str:
    joined, cover, toc = _cover_and_toc("".join(body_elements), html.unescape(escaped_title))
    plain_title = _css_string(escaped_title)
    page_css = (
        '@page { @bottom-left { content: "%s"; font: 9px "Segoe UI", Arial, sans-serif; color: #94a3b8; } '
        '@bottom-right { content: "Page " counter(page) " of " counter(pages); font: 9px "Segoe UI", Arial, '
        'sans-serif; color: #94a3b8; } }' % plain_title)
    if cover:                                    # no footer on the cover itself
        page_css += ' @page :first { @bottom-left { content: none; } @bottom-right { content: none; } }'
    lang = "bn" if _BENGALI.search(joined) else "en"
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="UTF-8">
<title>{escaped_title}</title>
<style>{DOC_CSS}{PRINT_CSS}{page_css}</style>
</head>
<body>
<div class="doc-container">
  {cover}{toc}{joined}
</div>
</body>
</html>"""
