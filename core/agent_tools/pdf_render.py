import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .dom_convert import _markdown_to_html_dom, normalize_tables

_PDF_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
            "script-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
_PDF_WAIT_S = 15          # how long to wait for the browser's PDF after it has exited
_DOCTYPE_RX =re.compile(r"^\s*<!doctype[^>]*>", re.IGNORECASE)


def _find_chromium_binary() -> Optional[str]:
    import shutil
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        shutil.which("msedge"),
        shutil.which("chrome"),
        shutil.which("chromium"),
        # Linux package / snap names
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium-browser"),
        shutil.which("microsoft-edge"),
        "/snap/bin/chromium",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _lock_down_html(html_content: str) -> str:
    body = _DOCTYPE_RX.sub("", html_content, count=1)
    return ('<!doctype html><html><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{_PDF_CSP}"></head>' + body)


def _render_html_to_pdf(html_content: str, output_pdf_path: Path) -> bool:
    browser_bin = _find_chromium_binary()
    if not browser_bin:
        return False

    import shutil
    import tempfile
    abs_pdf = os.path.abspath(str(output_pdf_path))
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as tf:
        tf.write(_lock_down_html(html_content))
        tmp_html = os.path.abspath(tf.name)
    # throwaway profile: never the service account's real browser cookies/logins
    profile_dir = tempfile.mkdtemp(prefix="pdfprof_")

    try:
        cmd = [
            browser_bin,
            "--headless=new",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--user-data-dir={profile_dir}",
            # no "--blink-settings=scriptEnabled=false": in current Edge/Chrome it makes --print-to-pdf hang without
            # writing a file. Scripts are blocked anyway by the page's CSP (script-src 'none', _lock_down_html).
            "--host-resolver-rules=MAP * ~NOTFOUND",
            "--proxy-server=127.0.0.1:9",
            "--disable-extensions",
            "--no-first-run",
            f"--print-to-pdf={abs_pdf}",
            tmp_html
        ]
        try:
            os.remove(abs_pdf)            # a leftover file must not be mistaken for this render
        except OSError:
            pass
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if res.returncode != 0:
            print(f"[_render_html_to_pdf] browser exited {res.returncode}: "
                  f"{(res.stderr or '').strip()[:300]}", file=sys.stderr)
            return False
        # Current Edge builds hand the print job to a detached child and exit at once, so the PDF shows up a
        # moment AFTER this process returned. Wait for it, and for it to stop growing, instead of giving up
        # (which silently fell back to the plain ReportLab writer: no tables, raw backticks).
        deadline = time.monotonic() + _PDF_WAIT_S
        last_size = -1
        while True:
            if _pdf_looks_valid(Path(abs_pdf)):
                size = os.path.getsize(abs_pdf)
                if size == last_size:
                    return True
                last_size = size
            if time.monotonic() >= deadline:
                return _pdf_looks_valid(Path(abs_pdf))
            time.sleep(0.25)
    except Exception as e:
        print(f"[_render_html_to_pdf browser error: {e}]", file=sys.stderr)
        return False
    finally:
        try:
            if os.path.exists(tmp_html):
                os.remove(tmp_html)
        except Exception:
            pass
        shutil.rmtree(profile_dir, ignore_errors=True)


def _pdf_looks_valid(p: Path) -> bool:
    """True only for a real PDF: present, non-empty, headed %PDF-."""
    try:
        return p.is_file() and p.stat().st_size > 0 and p.read_bytes()[:5] == b"%PDF-"
    except OSError:
        return False


def _reportlab_table(rows: list, width_pts: float = 522):
    """A Markdown table (list of '| a | b |' lines) as a ReportLab Table, for the no-browser fallback."""
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, Table, TableStyle
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    header = len(cells) >= 2 and all(re.match(r"^:?-+:?$", c.replace(" ", "")) for c in cells[1])
    if header:
        del cells[1]
    ncol = max(len(r) for r in cells)
    base = getSampleStyleSheet()["Normal"]
    body = ParagraphStyle("TblCell", parent=base, fontSize=8.5, leading=11)

    def cell(text: str, bold: bool):
        t = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        return Paragraph(f"<b>{t}</b>" if bold else t, body)

    data = [[cell(r[i] if i < len(r) else "", header and ri == 0) for i in range(ncol)] for ri, r in enumerate(cells)]
    tbl = Table(data, colWidths=[width_pts / ncol] * ncol, repeatRows=1 if header else 0)
    style = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")), ("VALIGN", (0, 0), (-1, -1), "TOP")]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")))
    tbl.setStyle(TableStyle(style))
    return tbl


def _save_text_or_markdown_as_pdf(p: Path, content: str) -> bool:
    try:
        # Content already is PDF bytes (str form, latin1-encoded by the caller)
        if isinstance(content, str) and content.startswith("%PDF-"):
            p.write_bytes(content.encode("latin1", errors="replace"))
            return _pdf_looks_valid(p)
        if isinstance(content, bytes) and content.startswith(b"%PDF-"):
            p.write_bytes(content)
            return _pdf_looks_valid(p)

        c_low = content.strip().lower()
        if c_low.startswith("<!doctype html") or "<html" in c_low[:200]:
            if _render_html_to_pdf(content, p):
                return True
            reason = ("no Chrome/Edge is installed to render HTML to PDF"
                      if not _find_chromium_binary() else "the headless browser render failed")
            print(f"[_save_text_or_markdown_as_pdf] {reason}", file=sys.stderr)
            return False

        stem_low = p.stem.lower()
        is_slides = "slide" in stem_low or "presentation" in stem_low or "deck" in stem_low or (content.count("\n# ") >= 2 and "---" in content)
        doc_title = p.stem.replace("_", " ").replace("-", " ").title()

        # Step 1: Generate modern, beautiful HTML DOM
        html_dom = _markdown_to_html_dom(content, title=doc_title, is_slides=is_slides)

        # Step 2: Render HTML DOM to PDF via Headless Chromium/Edge
        if _render_html_to_pdf(html_dom, p):
            return True

        # Step 3: ReportLab fallback for markdown when no browser is available
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors

        doc = SimpleDocTemplate(
            str(p),
            pagesize=letter,
            rightMargin=45, leftMargin=45,
            topMargin=45, bottomMargin=45
        )
        styles = getSampleStyleSheet()
        normal = ParagraphStyle('CustomNormal', parent=styles['Normal'], fontSize=10, leading=14, textColor=colors.HexColor("#1e293b"))
        title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=18, leading=22, textColor=colors.HexColor("#0f172a"), spaceAfter=8)
        h2_style = ParagraphStyle('CustomH2', parent=styles['Heading2'], fontSize=13, leading=17, textColor=colors.HexColor("#1e40af"), spaceBefore=10, spaceAfter=6)
        h3_style = ParagraphStyle('CustomH3', parent=styles['Heading3'], fontSize=11, leading=15, textColor=colors.HexColor("#334155"), spaceBefore=6, spaceAfter=4)
        code_style = ParagraphStyle('CustomCode', parent=styles['Code'], fontSize=9, leading=12, textColor=colors.HexColor("#0f172a"), backColor=colors.HexColor("#f1f5f9"), spaceAfter=6)

        story = []
        lines = normalize_tables(content.strip()).splitlines()
        in_code_block = False
        code_buf = []
        tbl_rows = []

        for line in lines:
            trimmed = line.strip()
            if not in_code_block and trimmed.startswith("|") and trimmed.endswith("|"):
                tbl_rows.append(trimmed)
                continue
            if tbl_rows:
                story.append(_reportlab_table(tbl_rows))
                story.append(Spacer(1, 6))
                tbl_rows = []
            if trimmed.startswith("```"):
                if in_code_block:
                    story.append(Paragraph("<br/>".join(code_buf), code_style))
                    code_buf = []
                    in_code_block = False
                else:
                    in_code_block = True
                continue
            if in_code_block:
                escaped = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                code_buf.append(escaped)
                continue
            if not trimmed:
                story.append(Spacer(1, 6))
                continue
            if trimmed.startswith("# "):
                text = trimmed[2:].strip().replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                story.append(Paragraph(f"<b>{text}</b>", title_style))
                story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cbd5e1"), spaceBefore=2, spaceAfter=8))
            elif trimmed.startswith("## "):
                text = trimmed[3:].strip().replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                story.append(Paragraph(f"<b>{text}</b>", h2_style))
            elif trimmed.startswith("### "):
                text = trimmed[4:].strip().replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                story.append(Paragraph(f"<b>{text}</b>", h3_style))
            elif trimmed.startswith("- ") or trimmed.startswith("* "):
                text = trimmed[2:].strip().replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
                story.append(Paragraph(f"&bull; {text}", normal))
            elif re.match(r'^\d+\.\s+', trimmed):
                num_m = re.match(r'^(\d+\.\s+)(.*)', trimmed)
                num_prefix = num_m.group(1) if num_m else ""
                text = (num_m.group(2) if num_m else trimmed).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
                story.append(Paragraph(f"{num_prefix}{text}", normal))
            elif trimmed.startswith("---") or trimmed.startswith("***"):
                story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e2e8f0"), spaceBefore=4, spaceAfter=6))
            else:
                text = trimmed.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
                story.append(Paragraph(text, normal))
                story.append(Spacer(1, 3))

        if tbl_rows:
            story.append(_reportlab_table(tbl_rows))
        if in_code_block and code_buf:
            story.append(Paragraph("<br/>".join(code_buf), code_style))

        if not story:
            print("[_save_text_or_markdown_as_pdf] content produced no document elements",
                  file=sys.stderr)
            return False

        doc.build(story)
        return _pdf_looks_valid(p)
    except Exception as e:
        print(f"[_save_text_or_markdown_as_pdf error: {e}]", file=sys.stderr)
        return False
