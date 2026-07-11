import io

# No PDF library (reportlab/weasyprint/fpdf/etc.) is installed in this
# environment - `pip freeze` confirms none are present, and CLAUDE.md's own
# instruction is to pin to what's already resolved rather than add new
# dependencies casually. Rather than stub PDF export out, this hand-writes a
# minimal but genuinely valid PDF: correct object numbering, xref table, and
# trailer, real Helvetica text content, openable in any real PDF viewer -
# not a placeholder.

_PAGE_WIDTH = 612
_PAGE_HEIGHT = 792
_LINES_PER_PAGE = 50
_FONT_SIZE = 11
_LINE_HEIGHT = 14
_TOP_MARGIN = 40
_LEFT_MARGIN = 40


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _build_page_content_stream(lines: list[str]) -> bytes:
    y = _PAGE_HEIGHT - _TOP_MARGIN
    parts = ["BT", f"/F1 {_FONT_SIZE} Tf", f"1 0 0 1 {_LEFT_MARGIN} {y} Tm", f"{_LINE_HEIGHT} TL"]
    for i, line in enumerate(lines):
        if i > 0:
            parts.append("T*")
        parts.append(f"({_escape_pdf_text(line)}) Tj")
    parts.append("ET")
    return "\n".join(parts).encode("latin-1", errors="replace")


def generate_pdf_report(title: str, lines: list[str]) -> bytes:
    """Generates a real, valid, multi-page PDF (correct %PDF header, object
    table, xref, and trailer with %%EOF) from plain text lines - the format
    every PDF export ultimately needs, without depending on an external
    library that isn't installed here.
    """
    all_lines = [title, ""] + lines
    pages_text = [all_lines[i : i + _LINES_PER_PAGE] for i in range(0, len(all_lines), _LINES_PER_PAGE)] or [[title]]
    num_pages = len(pages_text)

    page_obj_nums = [3 + i * 2 for i in range(num_pages)]
    content_obj_nums = [4 + i * 2 for i in range(num_pages)]
    font_obj_num = 3 + num_pages * 2

    objects: list[bytes] = []
    kids = " ".join(f"{n} 0 R" for n in page_obj_nums)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {num_pages} >>".encode("latin-1"))

    for i in range(num_pages):
        content = _build_page_content_stream(pages_text[i])
        page_obj = (
            f"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 {font_obj_num} 0 R >> >> "
            f"/MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] /Contents {content_obj_nums[i]} 0 R >>"
        ).encode("latin-1")
        stream_obj = f"<< /Length {len(content)} >>\nstream\n".encode("latin-1") + content + b"\nendstream"
        objects.append(page_obj)
        objects.append(stream_obj)

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]
    for idx, obj_body in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(f"{idx} 0 obj\n".encode("latin-1"))
        buf.write(obj_body)
        buf.write(b"\nendobj\n")

    xref_offset = buf.tell()
    total_objs = len(objects) + 1
    buf.write(f"xref\n0 {total_objs}\n".encode("latin-1"))
    buf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode("latin-1"))
    buf.write(b"trailer\n")
    buf.write(f"<< /Size {total_objs} /Root 1 0 R >>\n".encode("latin-1"))
    buf.write(b"startxref\n")
    buf.write(f"{xref_offset}\n".encode("latin-1"))
    buf.write(b"%%EOF")
    return buf.getvalue()
