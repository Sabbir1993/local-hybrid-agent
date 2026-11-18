"""Reading benchmark (Phase 0 of the file-reading speed plan): how long the extraction paths take and how often a
document is parsed. Run before and after a change:  python scripts/bench_read.py

It builds a 30-page PDF and a 5,000-line text file in a temp folder, then times
  * paging a PDF 4 times through the upload extractor (what read_file_chunk does per chunk),
  * pdf_read of 3 page ranges,
  * a 5,000-line text read in 10 slices.
It counts parses by wrapping pdfplumber.open. Nothing leaves the temp folder.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_pdf(path: Path, pages: int = 30) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4)
    for n in range(1, pages + 1):
        c.setFont("Helvetica", 11)
        for i in range(48):
            c.drawString(50, 800 - i * 15, f"Page {n} line {i}: quarterly settlement summary figure {n * i:06d} for the sample report")
        c.showPage()
    c.save()


def main() -> None:
    import pdfplumber
    opens = {"n": 0}
    real_open = pdfplumber.open

    def counting_open(*a, **k):
        opens["n"] += 1
        return real_open(*a, **k)
    pdfplumber.open = counting_open

    from core.file_tools.extract_cache import cached_text
    from core.file_tools.extractors import extract_file_content
    from core.pdf_tools import read as pdf_read

    with tempfile.TemporaryDirectory() as td:
        pdf = Path(td) / "sample.pdf"
        txt = Path(td) / "big.txt"
        _make_pdf(pdf)
        txt.write_text("\n".join(f"line {i}: " + "x" * 60 for i in range(5000)), encoding="utf-8")
        data = pdf.read_bytes()

        opens["n"] = 0
        t = time.perf_counter()
        for _ in range(4):                       # four chunks of one uploaded PDF
            cached_text(pdf, lambda f: extract_file_content(f, max_chars=999_999_999)[0])   # what read_file_chunk does
        print(f"4 x read_file_chunk extraction (cached): {time.perf_counter() - t:6.2f}s  pdf opens: {opens['n']}")

        opens["n"] = 0
        t = time.perf_counter()
        for pages in ("1-10", "11-20", "21-30"):
            pdf_read.read(data, pages=pages)
        print(f"3 x pdf_read(10 pages):               {time.perf_counter() - t:6.2f}s  pdf opens: {opens['n']}")

        t = time.perf_counter()
        for i in range(10):
            lines = txt.read_text(encoding="utf-8").splitlines()
            _ = "\n".join(lines[i * 500:(i + 1) * 500])
        print(f"10 x text slice (full read each):     {time.perf_counter() - t:6.2f}s")


if __name__ == "__main__":
    main()
