#!/usr/bin/env python3
import json, sys, html
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, black
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from pypdf import PdfReader

BLUE = HexColor('#1F4E79')
GREY = HexColor('#BFBFBF')
LINK = HexColor('#0563C1')
TEXT = black


def fonts():
    candidates = [
        ('Calibri', r'C:\\Windows\\Fonts\\calibri.ttf', r'C:\\Windows\\Fonts\\calibrib.ttf', r'C:\\Windows\\Fonts\\calibrii.ttf'),
        ('Carlito', '/usr/share/fonts/truetype/crosextra/Carlito-Regular.ttf', '/usr/share/fonts/truetype/crosextra/Carlito-Bold.ttf', '/usr/share/fonts/truetype/crosextra/Carlito-Italic.ttf'),
        ('LiberationSans', '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf', '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf', '/usr/share/fonts/truetype/liberation2/LiberationSans-Italic.ttf'),
    ]
    for name, reg, bold, italic in candidates:
        if Path(reg).exists() and Path(bold).exists():
            pdfmetrics.registerFont(TTFont(name, reg))
            pdfmetrics.registerFont(TTFont(name + '-Bold', bold))
            if Path(italic).exists():
                pdfmetrics.registerFont(TTFont(name + '-Italic', italic))
                italic_name = name + '-Italic'
            else:
                italic_name = name
            return name, name + '-Bold', italic_name
    return 'Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique'


REG, BOLD, ITALIC = fonts()


def rich(text, bold_frags):
    escaped = html.escape(str(text))
    for frag in sorted((f for f in bold_frags if f), key=len, reverse=True):
        ef = html.escape(frag)
        escaped = escaped.replace(ef, f'<b>{ef}</b>')
    return escaped


def para_height(text, width, font_name=REG, font_size=9.6, leading=11.2,
                left_indent=0, first_line_indent=0, bullet_indent=0):
    style = ParagraphStyle(
        'p', fontName=font_name, fontSize=font_size, leading=leading,
        textColor=TEXT, leftIndent=left_indent, rightIndent=0,
        firstLineIndent=first_line_indent, bulletIndent=bullet_indent,
        spaceBefore=0, spaceAfter=0,
    )
    p = Paragraph(text, style)
    _, h = p.wrap(width, 2000)
    return p, h


def draw_para(c, markup, x, y, width, font_name=REG, font_size=9.6,
              leading=11.2, gap_after=0, left_indent=0,
              first_line_indent=0, bullet_indent=0, bullet_text=None):
    style = ParagraphStyle(
        'p', fontName=font_name, fontSize=font_size, leading=leading,
        textColor=TEXT, leftIndent=left_indent, rightIndent=0,
        firstLineIndent=first_line_indent, bulletIndent=bullet_indent,
        spaceBefore=0, spaceAfter=0,
    )
    p = Paragraph(markup, style, bulletText=bullet_text)
    _, h = p.wrap(width, 2000)
    p.drawOn(c, x, y - h)
    return y - h - gap_after


def split_header(header):
    parts = [p.strip() for p in str(header).split('|')]
    if len(parts) >= 2:
        return parts[0], ' | '.join(parts[1:])
    return str(header), ''


def header_markup(header, dates):
    role, employer = split_header(header)
    bits = [f'<b>{html.escape(role)}</b>']
    if employer:
        bits.append(f'<font color="#808080"> | </font>{html.escape(employer)}')
    if dates:
        bits.append(f'<font color="#808080"> | </font>{html.escape(str(dates))}')
    return ''.join(bits)


def main():
    if len(sys.argv) != 3:
        raise SystemExit('Usage: python tools/render_cv.py input.json output.pdf')

    inp = Path(sys.argv[1])
    out = Path(sys.argv[2])
    data = json.loads(inp.read_text(encoding='utf-8'))
    out.parent.mkdir(parents=True, exist_ok=True)

    W, H = A4
    left = 0.625 * inch
    right = 0.625 * inch
    top = 0.40 * inch
    bottom = 0.42 * inch
    usable = W - left - right
    c = canvas.Canvas(str(out), pagesize=A4)
    y = H - top
    bold_frags = data.get('bold_fragments', [])

    def fail_overflow(where='content'):
        c.save()
        out.unlink(missing_ok=True)
        raise SystemExit(f'OVERFLOW: master CV does not fit one page near {where}. Prune lower-value content; do not shrink the approved typography.')

    def ensure(extra=0, where='content'):
        if y - extra < bottom:
            fail_overflow(where)

    def section(title):
        nonlocal y
        # Clear visual separation from the previous block.
        y -= 6.5
        ensure(18, f'section {title}')
        c.setFillColor(BLUE)
        c.setFont(BOLD, 10.7)
        c.drawString(left, y, title)
        y -= 4.4
        c.setStrokeColor(GREY)
        c.setLineWidth(0.45)
        c.line(left, y, W - right, y)
        y -= 6.3

    # Header
    c.setFillColor(TEXT)
    c.setFont(BOLD, 17)
    c.drawCentredString(W / 2, y, data['name'])
    y -= 15.8

    # Contact line. Render as one line in blue to retain the visual language of the master CV.
    c.setFont(REG, 9.3)
    c.setFillColor(LINK)
    c.drawCentredString(W / 2, y, data['contact'])
    y -= 6.0

    # Summary
    section('PROFESSIONAL SUMMARY')
    y = draw_para(c, rich(data['summary'], bold_frags), left, y, usable,
                  font_name=REG, font_size=9.65, leading=11.25, gap_after=0.5)
    ensure(5, 'summary')

    for sec in data.get('sections', []):
        section(sec['title'])
        for idx, entry in enumerate(sec.get('entries', [])):
            ensure(18, f"{sec['title']} header")
            y = draw_para(
                c,
                header_markup(entry.get('header', ''), entry.get('dates', '')),
                left, y, usable,
                font_name=REG, font_size=9.65, leading=11.15,
                gap_after=3.1,
            )

            for line in entry.get('lines', []):
                y = draw_para(
                    c, rich(line, bold_frags), left, y, usable,
                    font_name=ITALIC, font_size=9.25, leading=10.65,
                    gap_after=2.0,
                )
                ensure(4, f"{entry.get('header','entry')} detail")

            for b in entry.get('bullets', []):
                y = draw_para(
                    c, rich(b, bold_frags), left, y, usable,
                    font_name=REG, font_size=9.35, leading=10.85,
                    gap_after=1.4, left_indent=15, bullet_indent=3,
                    bullet_text='•',
                )
                ensure(4, f"{entry.get('header','entry')} bullet")

            # Space between entries so the next bold header never collides with a wrapped bullet.
            y -= 3.5

        for line in sec.get('lines', []):
            if ':' in line:
                label, rest = line.split(':', 1)
                markup = f'<b>{html.escape(label)}:</b>{html.escape(rest)}'
            else:
                markup = rich(line, bold_frags)
            y = draw_para(c, markup, left, y, usable,
                          font_name=REG, font_size=9.25, leading=10.55,
                          gap_after=0.3)
            ensure(3, f"{sec['title']} line")

    ensure(0, 'end')
    c.save()

    pages = len(PdfReader(str(out)).pages)
    if pages != 1:
        out.unlink(missing_ok=True)
        raise SystemExit(f'OVERFLOW: rendered {pages} pages; expected exactly 1.')
    print(out)


if __name__ == '__main__':
    main()
