"""PDF fixtures built with reportlab for the PDF tool tests."""
import io


def text_pdf(pages=3, prefix="Page", extra=None) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for i in range(1, pages + 1):
        c.setFont("Helvetica", 14)
        c.drawString(72, 780, f"{prefix} {i} heading")
        c.setFont("Helvetica", 10)
        c.drawString(72, 750, f"This is the body text of page {i}.")
        if extra and i in extra:
            c.drawString(72, 730, extra[i])
        c.showPage()
    c.save()
    return buf.getvalue()


def table_pdf(rows) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
    buf = io.BytesIO()
    t = Table(rows)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.8, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 10)]))
    SimpleDocTemplate(buf, pagesize=A4).build([t])
    return buf.getvalue()


def scanned_pdf() -> bytes:
    """A page that is only a picture (no text layer)."""
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    img = Image.new("RGB", (800, 400), "white")
    ImageDraw.Draw(img).text((40, 40), "scanned words only", fill="black")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    c.drawImage(ImageReader(img), 40, 400, width=500, height=250)
    c.showPage()
    c.save()
    return buf.getvalue()
