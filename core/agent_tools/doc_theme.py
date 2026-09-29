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
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
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
    font-size: 12px;
    overflow-x: auto;
    margin: 14px 0;
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
"""


def render_doc_page(escaped_title: str, body_elements: list[str]) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{escaped_title}</title>
<style>{DOC_CSS}</style>
</head>
<body>
<div class="doc-container">
  <span class="doc-header-badge">Autonomous Document Engine</span>
  {''.join(body_elements)}
  <div class="doc-footer">
    <span>Document: {escaped_title}</span>
    <span>Official Document</span>
  </div>
</div>
</body>
</html>"""
