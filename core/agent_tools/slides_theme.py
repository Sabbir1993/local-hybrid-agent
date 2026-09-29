SLIDES_CSS = """
  @page {
    size: 11in 6.1875in landscape;
    margin: 0;
  }
  @media print {
    body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: #0f172a;
    color: #f8fafc;
  }
  .slide {
    page-break-after: always;
    width: 11in;
    height: 6.1875in;
    padding: 40px 52px;
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    background: #0f172a;
    box-sizing: border-box;
    overflow: hidden;
  }
  .slide:nth-child(even) {
    background: #1e293b;
  }
  .slide-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 2px solid #3b82f6;
    padding-bottom: 12px;
    margin-bottom: 24px;
  }
  .slide-title {
    margin: 0;
    font-size: 28px;
    font-weight: 700;
    color: #60a5fa;
  }
  .slide-num {
    font-size: 13px;
    color: #94a3b8;
    font-weight: 600;
    background: rgba(255,255,255,0.08);
    padding: 4px 10px;
    border-radius: 6px;
  }
  .slide-body {
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    font-size: 16px;
    line-height: 1.6;
  }
  .slide-subtitle {
    color: #93c5fd;
    font-size: 19px;
    margin: 12px 0 8px 0;
  }
  .slide-text {
    color: #cbd5e1;
    margin: 0 0 12px 0;
    font-size: 16px;
  }
  .slide-list {
    margin: 8px 0 16px 0;
    padding-left: 24px;
  }
  .slide-list li {
    margin-bottom: 10px;
    color: #e2e8f0;
  }
  .slide-footer {
    margin-top: auto;
    padding-top: 12px;
    border-top: 1px solid rgba(255,255,255,0.1);
    font-size: 11px;
    color: #64748b;
    display: flex;
    justify-content: space-between;
  }
  .inline-code {
    background: rgba(255,255,255,0.1);
    color: #93c5fd;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 14px;
  }
"""


def render_slides_page(escaped_title: str, slides_html: list[str]) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{escaped_title}</title>
<style>{SLIDES_CSS}</style>
</head>
<body>
{''.join(slides_html)}
</body>
</html>"""
