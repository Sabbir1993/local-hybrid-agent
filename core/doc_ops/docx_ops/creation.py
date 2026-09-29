import io
import re


def _add_inline(p, text: str):
    for k, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if part:
            p.add_run(part).bold = bool(k % 2)


def create(md: str, title: str = "") -> bytes:
    import docx
    doc = docx.Document()
    lines = (md or "").splitlines()
    i = 0
    in_code, code = False, []
    while i < len(lines):
        line = lines[i]
        st = line.strip()
        if st.startswith("```"):
            if in_code:
                p = doc.add_paragraph("\n".join(code))
                for r in p.runs:
                    r.font.name = "Consolas"
                code, in_code = [], False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code.append(line)
            i += 1
            continue
        if st.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not re.match(r"^:?-{2,}:?$", cells[0] or "--"):
                    rows.append(cells)
                i += 1
            ncol = max(len(r) for r in rows)
            t = doc.add_table(rows=len(rows), cols=ncol)
            t.style = "Table Grid"
            for r, vals in enumerate(rows):
                for c, v in enumerate(vals):
                    t.cell(r, c).text = v.replace("**", "")
                    if r == 0:
                        for run in t.cell(r, c).paragraphs[0].runs:
                            run.bold = True
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", st)
        if m:
            doc.add_heading(m.group(2).strip(), level=min(len(m.group(1)), 4) if len(m.group(1)) > 1 or title else 0)
        elif re.match(r"^[-*+]\s+", st):
            _add_inline(doc.add_paragraph(style="List Bullet"), re.sub(r"^[-*+]\s+", "", st))
        elif re.match(r"^\d+[.)]\s+", st):
            _add_inline(doc.add_paragraph(style="List Number"), re.sub(r"^\d+[.)]\s+", "", st))
        elif st:
            _add_inline(doc.add_paragraph(), st)
        i += 1
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
