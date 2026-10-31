"""DOCX (python-docx) + PDF (LibreOffice) export with book-style formatting.

Two-pass export: build DOCX -> convert to PDF -> read chapter page numbers from the PDF
outline -> rebuild DOCX with a Table of Contents that has correct page numbers -> final PDF.
"""
from __future__ import annotations

import datetime
import shutil
import subprocess
import sys
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import (WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER)
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

WORDS_PER_PAGE = 280  # rough, 6x9 @ 12pt, used only to pick the KDP gutter


# ------------------------------------------------------------------ helpers
def kdp_gutter(pages: int) -> float:
    """Inside (gutter) margin in inches per KDP's page-count table."""
    for limit, g in [(150, 0.375), (300, 0.5), (500, 0.625), (700, 0.75)]:
        if pages <= limit:
            return g
    return 0.875


def _set_fonts(style, font: str, size: float):
    style.font.name = font
    style.font.size = Pt(size)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for a in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if rfonts.get(qn(a)) is not None:
            del rfonts.attrib[qn(a)]
    for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(a), font)
    szcs = rpr.find(qn("w:szCs"))
    if szcs is None:
        szcs = OxmlElement("w:szCs")
        rpr.append(szcs)
    szcs.set(qn("w:val"), str(int(size * 2)))


def _style(doc, name, base="Normal", font=None, size=None, bold=None, italic=None,
           align=None, before=0, after=0, indent=None, line=None, keep_next=False):
    st = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    st.base_style = doc.styles[base]
    if font:
        _set_fonts(st, font, size or 12)
    elif size:
        st.font.size = Pt(size)
    if bold is not None: st.font.bold = bold
    if italic is not None: st.font.italic = italic
    st.font.color.rgb = RGBColor(0, 0, 0)
    pf = st.paragraph_format
    if align is not None: pf.alignment = align
    pf.space_before, pf.space_after = Pt(before), Pt(after)
    if indent is not None: pf.first_line_indent = Inches(indent)
    if line: pf.line_spacing = line
    pf.keep_with_next = keep_next
    pf.widow_control = True
    return st


def _field(run, instr: str):
    """Insert a simple complex field (e.g. PAGE) into a run."""
    for kind, text in (("begin", None), (None, instr), ("end", None)):
        if kind:
            el = OxmlElement("w:fldChar"); el.set(qn("w:fldCharType"), kind)
        else:
            el = OxmlElement("w:instrText"); el.set(qn("xml:space"), "preserve"); el.text = text
        run._r.append(el)


def _fldchar(par, kind):
    r = par.add_run()
    el = OxmlElement("w:fldChar"); el.set(qn("w:fldCharType"), kind)
    r._r.append(el)
    return r


def _instr(par, text):
    r = par.add_run()
    el = OxmlElement("w:instrText"); el.set(qn("xml:space"), "preserve"); el.text = text
    r._r.append(el)


def _body_width_in(section) -> float:
    return (section.page_width - section.left_margin - section.right_margin) / 914400



def _spacer(doc, points: float):
    """Empty paragraph that pushes content down (space_before is ignored at page top)."""
    p = doc.add_paragraph(style="Normal")
    p.paragraph_format.space_after = Pt(points)
    p.paragraph_format.space_before = Pt(0)
    return p

def _chapter_heading_text(cfg, n, title):
    return f"{n}. {title}" if cfg.get("format", {}).get("chapter_numbering", True) else title


# -------------------------------------------------------------- build docx
def build_docx(cfg: dict, meta: dict, chapters: list[tuple[int, str, str]],
               path: Path, toc_pages: list[int] | None = None, toc_page_numbers_known=False,
               images: dict | None = None):
    images = images or {}
    f = cfg.get("format", {})
    book = cfg["book"]
    font = f.get("font", "Times New Roman")
    size = float(f.get("font_size", 12))
    w_in, h_in = [float(x) for x in str(f.get("trim_size", "6x9")).lower().split("x")]
    m = f.get("margins", {})
    top, bottom, outside = float(m.get("top", 0.75)), float(m.get("bottom", 0.75)), float(m.get("outside", 0.6))

    total_words = sum(len(t.split()) for _, _, t in chapters)
    est_pages = total_words // WORDS_PER_PAGE + 8 + len(images) // 2
    gutter = float(m["gutter"]) if m.get("gutter") else kdp_gutter(est_pages)

    doc = Document()
    # mirrored margins: with mirror on, w:left is the INSIDE margin, w:right the OUTSIDE one
    settings = doc.settings.element
    zoom = settings.find(qn("w:zoom"))
    if zoom is not None and zoom.get(qn("w:percent")) is None:
        zoom.set(qn("w:percent"), "100")
    mirror = OxmlElement("w:mirrorMargins")      # schema order: right after w:zoom
    if zoom is not None:
        zoom.addnext(mirror)
    else:
        settings.insert(0, mirror)
    if not toc_page_numbers_known:               # Word refreshes the TOC on open
        upd = OxmlElement("w:updateFields"); upd.set(qn("w:val"), "true")
        compat = settings.find(qn("w:compat"))
        if compat is not None:
            compat.addprevious(upd)
        else:
            settings.append(upd)

    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(w_in), Inches(h_in)
    sec.top_margin, sec.bottom_margin = Inches(top), Inches(bottom)
    sec.left_margin, sec.right_margin = Inches(outside + gutter), Inches(outside)
    sec.header_distance = sec.footer_distance = Inches(0.35)

    # ---- styles
    _set_fonts(doc.styles["Normal"], font, size)
    body_align = WD_ALIGN_PARAGRAPH.JUSTIFY if f.get("justify", True) else WD_ALIGN_PARAGRAPH.LEFT
    line = float(f.get("line_spacing", 1.15))
    indent = float(f.get("first_line_indent", 0.3))
    _style(doc, "BookBody", font=font, size=size, align=body_align, indent=indent, line=line)
    _style(doc, "BookBodyFirst", font=font, size=size, align=body_align, indent=0, line=line)
    _style(doc, "SceneBreak", font=font, size=size, align=WD_ALIGN_PARAGRAPH.CENTER, before=12, after=12)
    _style(doc, "BkTitle", font=font, size=30, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, after=10)
    _style(doc, "BkSubtitle", font=font, size=15, italic=True, align=WD_ALIGN_PARAGRAPH.CENTER, after=10)
    _style(doc, "BkAuthor", font=font, size=15, align=WD_ALIGN_PARAGRAPH.CENTER, before=90)
    _style(doc, "FrontHeading", font=font, size=20, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, before=30, after=24)
    _style(doc, "Small", font=font, size=9, align=WD_ALIGN_PARAGRAPH.LEFT, after=6, line=1.1)
    _style(doc, "Dedication", font=font, size=size, italic=True, align=WD_ALIGN_PARAGRAPH.CENTER)

    h1 = doc.styles["Heading 1"]
    _set_fonts(h1, font, float(f.get("chapter_title_size", 20)))
    h1.font.bold = True
    h1.font.color.rgb = RGBColor(0, 0, 0)
    pf = h1.paragraph_format
    pf.alignment = WD_ALIGN_PARAGRAPH.CENTER
    pf.space_before, pf.space_after = Pt(70), Pt(30)
    pf.keep_with_next = True

    # ---- title page
    _spacer(doc, 150)
    doc.add_paragraph(meta["title"], style="BkTitle")
    if meta.get("subtitle"):
        doc.add_paragraph(meta["subtitle"], style="BkSubtitle")
    doc.add_paragraph(book.get("author", ""), style="BkAuthor")
    doc.add_page_break()

    # ---- copyright page
    year = datetime.date.today().year
    _spacer(doc, 330)
    p = doc.add_paragraph(style="Small")
    p.add_run(f"Copyright \u00a9 {year} {book.get('author', '')}. All rights reserved.")
    for line_ in [book.get("copyright_notice", ""), book.get("publisher_line", "")]:
        if line_:
            doc.add_paragraph(line_, style="Small")
    doc.add_page_break()

    if book.get("dedication"):
        _spacer(doc, 160)
        doc.add_paragraph(book["dedication"], style="Dedication")
        doc.add_page_break()

    # ---- contents
    doc.add_paragraph("Contents", style="FrontHeading")
    entries = [(_chapter_heading_text(cfg, n, t), (toc_pages[i] if toc_pages else None))
               for i, (n, t, _) in enumerate(chapters)]
    _add_toc(doc, entries, _body_width_in(sec))

    # ---- body section (page numbers restart at 1, footer shows PAGE)
    body = doc.add_section(WD_SECTION.NEW_PAGE)
    pg = OxmlElement("w:pgNumType"); pg.set(qn("w:start"), "1")
    pgmar = body._sectPr.find(qn("w:pgMar"))
    pgmar.addnext(pg)                            # schema order: right after w:pgMar
    body.footer.is_linked_to_previous = False
    fp = body.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = fp.add_run(); run.font.size = Pt(10); run.font.name = font
    _field(run, "PAGE")

    for i, (n, title, text) in enumerate(chapters):
        h = doc.add_heading(_chapter_heading_text(cfg, n, title), level=1)
        if i > 0:
            h.paragraph_format.page_break_before = True
        img = images.get(n)
        if img and Path(img).exists():
            from .images import img_cfg
            width = min(float(img_cfg(cfg)["width_in"]), _body_width_in(sec))
            doc.add_picture(str(img), width=Inches(width))
            ip = doc.paragraphs[-1]
            ip.alignment = WD_ALIGN_PARAGRAPH.CENTER
            ip.paragraph_format.space_before = Pt(0)
            ip.paragraph_format.space_after = Pt(20)
            ip.paragraph_format.keep_with_next = True
            ip.paragraph_format.first_line_indent = Inches(0)
        first = True
        for para in [p_.strip() for p_ in text.split("\n\n") if p_.strip()]:
            if para.strip() in ("***", "* * *", "---"):
                doc.add_paragraph("* * *", style="SceneBreak")
                first = True
                continue
            doc.add_paragraph(" ".join(para.split()), style="BookBodyFirst" if first else "BookBody")
            first = False

    if book.get("about_author"):
        h = doc.add_paragraph("About the Author", style="FrontHeading")
        h.paragraph_format.page_break_before = True
        for para in book["about_author"].strip().split("\n\n"):
            doc.add_paragraph(" ".join(para.split()), style="BookBody")

    doc.core_properties.title = meta["title"]
    doc.core_properties.author = book.get("author", "")
    doc.save(str(path))


def _add_toc(doc, entries, width_in):
    """Real Word TOC field whose cached result already holds correct page numbers."""
    for idx, (text, page) in enumerate(entries):
        p = doc.add_paragraph(style="Normal")
        p.paragraph_format.tab_stops.add_tab_stop(Inches(width_in), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)
        p.paragraph_format.space_after = Pt(6)
        if idx == 0:
            _fldchar(p, "begin")
            _instr(p, 'TOC \\o "1-1" \\h \\z \\u')
            _fldchar(p, "separate")
        p.add_run(text)
        p.add_run("\t" + (str(page) if page is not None else "00"))
        if idx == len(entries) - 1:
            _fldchar(p, "end")


# --------------------------------------------------------------------- pdf
def find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        if shutil.which(name):
            return shutil.which(name)
    for p in (r"C:\Program Files\LibreOffice\program\soffice.exe",
              r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if Path(p).exists():
            return p
    return None


def to_pdf(docx_path: Path) -> Path | None:
    out_dir = docx_path.parent
    pdf = docx_path.with_suffix(".pdf")
    soffice = find_soffice()
    if soffice:
        try:
            subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir),
                            str(docx_path)], check=True, capture_output=True, timeout=300)
            if pdf.exists():
                return pdf
        except Exception as e:
            print(f"  [warn] LibreOffice conversion failed: {e}")
    try:  # Windows/macOS with MS Word installed
        from docx2pdf import convert
        convert(str(docx_path), str(pdf))
        if pdf.exists():
            return pdf
    except Exception:
        pass
    return None


def chapter_pages(pdf: Path, n_chapters: int) -> list[int] | None:
    """Printed page numbers of each chapter start, read from the PDF outline (bookmarks)."""
    try:
        from pypdf import PdfReader
        r = PdfReader(str(pdf))
        idx = []

        def walk(items):
            for it in items:
                if isinstance(it, list):
                    walk(it)
                else:
                    idx.append(r.get_destination_page_number(it))
        walk(r.outline)
        if len(idx) != n_chapters:
            return None
        first = idx[0]
        return [i - first + 1 for i in idx]
    except Exception as e:
        print(f"  [warn] could not read page numbers from PDF: {e}")
        return None


# ------------------------------------------------------------- entry point
def export_book(cfg: dict, outline: dict, chapters: list, out_dir: Path, images: dict | None = None) -> dict:
    book = cfg["book"]
    meta = {"title": book.get("title") or outline.get("title", "Untitled"),
            "subtitle": book.get("subtitle") or outline.get("subtitle", "")}
    from .pipeline import slugify
    base = slugify(meta["title"])
    docx_path = out_dir / f"{base}.docx"
    result = {"docx": docx_path, "pdf": None}

    build_docx(cfg, meta, chapters, docx_path, toc_pages=None, toc_page_numbers_known=False, images=images)
    pdf = to_pdf(docx_path)
    if pdf:
        pages = chapter_pages(pdf, len(chapters))
        if pages:
            build_docx(cfg, meta, chapters, docx_path, toc_pages=pages, toc_page_numbers_known=True, images=images)
            pdf = to_pdf(docx_path) or pdf
        else:
            print("  [note] TOC page numbers could not be computed; Word will refresh them on open.")
        result["pdf"] = pdf
    else:
        print("  [note] PDF not created: install LibreOffice (soffice) or open the DOCX in Word -> Save as PDF.")
    return result
