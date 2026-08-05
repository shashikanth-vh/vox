"""Markdown → .docx, stdlib only — the mirror of ``extract_text``'s docx branch.

The workbench conversation ends with the CAM's text sitting in the box as Markdown.
The analyst's deliverable is a WORD file (the committee reads Word, the template is
Word), so this module renders that Markdown into a properly-styled .docx: real
Heading styles (the document outline works in Word's navigation pane), bold/italic/
code runs, bullet and numbered lists, tables with borders, and an Arial base font.

Deliberately dependency-free: a .docx is a zip of a few XML parts, and PRISM already
READS them with the stdlib — writing them the same way keeps the image unchanged.
What this is NOT: a full Markdown engine. It covers what the drafting engine actually
emits (headings, emphasis, lists, tables, code fences); anything else lands as plain
paragraph text rather than being dropped.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '</Types>')

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '</Relationships>')

_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '</Relationships>')


def _style(sid: str, name: str, size_half_pts: int, *, bold: bool = True,
           outline: int | None = None, color: str | None = None,
           space_before: int = 240, space_after: int = 120) -> str:
    ppr = (f'<w:pPr><w:keepNext/><w:spacing w:before="{space_before}" w:after="{space_after}"/>'
           + (f'<w:outlineLvl w:val="{outline}"/>' if outline is not None else '')
           + '</w:pPr>')
    rpr = ('<w:rPr>' + ('<w:b/>' if bold else '')
           + (f'<w:color w:val="{color}"/>' if color else '')
           + f'<w:sz w:val="{size_half_pts}"/><w:szCs w:val="{size_half_pts}"/></w:rPr>')
    return (f'<w:style w:type="paragraph" w:styleId="{sid}">'
            f'<w:name w:val="{name}"/><w:basedOn w:val="Normal"/>{ppr}{rpr}</w:style>')


# Arial throughout (a professional default); headings carry outline levels so the
# document navigates like a real Word file, not a wall of bolded paragraphs.
_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles {_W}>'
    '<w:docDefaults><w:rPrDefault><w:rPr>'
    '<w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/>'
    '<w:sz w:val="21"/><w:szCs w:val="21"/>'
    '</w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
    '</w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    + _style("Title", "Title", 40, outline=None, space_before=0, space_after=240)
    + _style("Heading1", "heading 1", 30, outline=0)
    + _style("Heading2", "heading 2", 26, outline=1)
    + _style("Heading3", "heading 3", 23, outline=2)
    + '</w:styles>')


# ---- inline runs ----------------------------------------------------------- #
_INLINE = re.compile(r'(\*\*.+?\*\*|__.+?__|\*[^*\n]+\*|_[^_\n]+_|`[^`\n]+`)')


def _runs(text: str, *, bold: bool = False) -> str:
    """**bold**, *italic*, `code` → <w:r> runs; everything else plain text."""
    out: list[str] = []
    for piece in _INLINE.split(text):
        if not piece:
            continue
        b, i, mono = bold, False, False
        if piece.startswith(('**', '__')) and piece.endswith(('**', '__')) and len(piece) > 4:
            b, piece = True, piece[2:-2]
        elif piece.startswith(('*', '_')) and piece.endswith(('*', '_')) and len(piece) > 2:
            i, piece = True, piece[1:-1]
        elif piece.startswith('`') and piece.endswith('`') and len(piece) > 2:
            mono, piece = True, piece[1:-1]
        rpr = ('<w:rPr>' + ('<w:b/>' if b else '') + ('<w:i/>' if i else '')
               + ('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>' if mono else '')
               + '</w:rPr>') if (b or i or mono) else ''
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(piece)}</w:t></w:r>')
    return ''.join(out) or '<w:r><w:t xml:space="preserve"></w:t></w:r>'


def _p(text: str, *, style: str | None = None, indent: int = 0,
       bullet: bool = False, number: str | None = None, bold: bool = False) -> str:
    ppr = ''
    if style or indent or bullet:
        ppr = ('<w:pPr>'
               + (f'<w:pStyle w:val="{style}"/>' if style else '')
               + (f'<w:ind w:left="{indent}" w:hanging="240"/>' if (bullet or number) and indent
                  else f'<w:ind w:left="{indent}"/>' if indent else '')
               + '</w:pPr>')
    lead = ''
    if bullet:
        lead = '<w:r><w:t xml:space="preserve">•  </w:t></w:r>'
    elif number:
        lead = f'<w:r><w:t xml:space="preserve">{escape(number)}  </w:t></w:r>'
    return f'<w:p>{ppr}{lead}{_runs(text, bold=bold)}</w:p>'


def _code_p(text: str) -> str:
    return ('<w:p><w:pPr><w:spacing w:after="0"/><w:ind w:left="360"/></w:pPr>'
            '<w:r><w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>'
            '<w:sz w:val="18"/></w:rPr>'
            f'<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')


_TBL_BORDER = ('<w:tblBorders>'
               + ''.join(f'<w:{side} w:val="single" w:sz="4" w:color="BFBFBF"/>'
                         for side in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'))
               + '</w:tblBorders>')


def _table(rows: list[list[str]], header: bool) -> str:
    xml = ['<w:tbl><w:tblPr>'
           '<w:tblW w:w="0" w:type="auto"/>' + _TBL_BORDER
           + '<w:tblCellMar><w:left w:w="80" w:type="dxa"/><w:right w:w="80" w:type="dxa"/></w:tblCellMar>'
           '</w:tblPr>']
    for r, cells in enumerate(rows):
        is_head = header and r == 0
        xml.append('<w:tr>')
        for cell in cells:
            shade = '<w:shd w:val="clear" w:fill="F2F2F2"/>' if is_head else ''
            xml.append(f'<w:tc><w:tcPr>{shade}</w:tcPr>'
                       + _p(cell, bold=is_head) + '</w:tc>')
        xml.append('</w:tr>')
    xml.append('</w:tbl>')
    # Word requires a paragraph after a table (a table may not end the body directly,
    # and back-to-back tables merge without one).
    xml.append('<w:p/>')
    return ''.join(xml)


_HR = re.compile(r'^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$')
_HEADING = re.compile(r'^(#{1,6})\s+(.*)$')
_BULLET = re.compile(r'^(\s*)[-*+]\s+(.*)$')
_NUMBERED = re.compile(r'^(\s*)(\d+)[.)]\s+(.*)$')


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip('|').split('|')]


def _is_separator(line: str) -> bool:
    body = line.strip().strip('|')
    return bool(body) and all(re.fullmatch(r'\s*:?-{3,}:?\s*', c) for c in body.split('|'))


def _render_body(md: str, title: str | None = None) -> str:
    """Markdown → the <w:body> INNER XML (no sectPr) — shared by the standalone
    package and the render-into-template path."""
    body: list[str] = []
    if title:
        body.append(_p(title, style="Title"))

    lines = md.replace('\r\n', '\n').split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.strip().startswith('```'):                       # fenced code
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                body.append(_code_p(lines[i]))
                i += 1
            i += 1
            body.append('<w:p/>')
            continue

        if '|' in line and line.strip().startswith('|'):          # table block
            rows: list[list[str]] = []
            header = False
            while i < len(lines) and lines[i].strip().startswith('|'):
                if _is_separator(lines[i]):
                    header = len(rows) == 1
                else:
                    rows.append(_split_row(lines[i]))
                i += 1
            if rows:
                width = max(len(r) for r in rows)
                body.append(_table([r + [''] * (width - len(r)) for r in rows], header))
            continue

        if not line.strip():
            i += 1
            continue
        if _HR.match(line):
            body.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" '
                        'w:color="BFBFBF"/></w:pBdr></w:pPr></w:p>')
            i += 1
            continue
        if (m := _HEADING.match(line)):
            level = min(len(m.group(1)), 3)
            body.append(_p(m.group(2).strip().strip('#').strip(),
                           style=f"Heading{level}"))
            i += 1
            continue
        if (m := _BULLET.match(line)):
            depth = len(m.group(1)) // 2
            body.append(_p(m.group(2), indent=360 + depth * 360, bullet=True))
            i += 1
            continue
        if (m := _NUMBERED.match(line)):
            depth = len(m.group(1)) // 2
            body.append(_p(m.group(3), indent=360 + depth * 360,
                           number=f"{m.group(2)}."))
            i += 1
            continue
        if line.lstrip().startswith('>'):
            body.append(_p(line.lstrip()[1:].strip(), indent=360))
            i += 1
            continue

        body.append(_p(line.strip()))
        i += 1

    return ''.join(body)


def markdown_to_docx(md: str, title: str | None = None) -> bytes:
    """Render workbench Markdown into a complete standalone .docx."""
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document {_W}><w:body>' + _render_body(md, title)
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
          '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/>'
          '</w:sectPr></w:body></w:document>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', _CONTENT_TYPES)
        z.writestr('_rels/.rels', _ROOT_RELS)
        z.writestr('word/_rels/document.xml.rels', _DOC_RELS)
        z.writestr('word/styles.xml', _STYLES)
        z.writestr('word/document.xml', document)
    return buf.getvalue()


# ---- render INTO an existing template ------------------------------------- #
_PARA = re.compile(r'<w:p(?: [^>]*)?/>|<w:p(?: [^>]*)?>.*?</w:p>', re.S)
_TAGS = re.compile(r'<[^>]+>')


def _letterhead_prefix(body_xml: str, cap: int = 10) -> str:
    """The template's LEADING paragraphs that are letterhead, not letter: logos
    (drawings/pictures) and blank spacers, up to the first paragraph with real
    text. Word cannot nest w:p in w:p, so the non-greedy paragraph match is exact
    for this leading scan (a leading table ends the scan anyway)."""
    kept: list[str] = []
    pos = 0
    for _ in range(cap):
        m = _PARA.match(body_xml, pos)
        if m is None:
            break
        para = m.group(0)
        has_art = '<w:drawing' in para or '<w:pict' in para
        has_text = bool(_TAGS.sub('', para).strip())
        if has_text and not has_art:
            break
        kept.append(para)
        pos = m.end()
    return ''.join(kept)


def markdown_into_template(md: str, template_blob: bytes,
                           title: str | None = None) -> bytes:
    """Render Markdown INSIDE the template's own package: its styles, theme,
    fonts, numbering, images and section setup all survive, so the output looks
    like the credit team's letterhead — only the letter text is replaced. Heading
    styles resolve against the TEMPLATE's Heading1/2/3 (colors included). Falls
    back to the standalone package when the template is not a readable .docx."""
    try:
        with zipfile.ZipFile(io.BytesIO(template_blob)) as z:
            parts = {n: z.read(n) for n in z.namelist()}
        doc = parts['word/document.xml'].decode('utf-8', 'ignore')
        m = re.search(r'(<w:body(?: [^>]*)?>)(.*)(</w:body>)', doc, re.S)
        if m is None:
            return markdown_to_docx(md, title)
    except (zipfile.BadZipFile, KeyError, OSError):
        return markdown_to_docx(md, title)
    inner = m.group(2)
    sect = re.search(r'<w:sectPr(?: [^>]*)?>[\s\S]*?</w:sectPr>|<w:sectPr(?: [^>]*)?/>',
                     inner)
    sect_xml = sect.group(0) if sect else \
        ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
         '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>')
    new_inner = _letterhead_prefix(inner) + _render_body(md, title) + sect_xml
    parts['word/document.xml'] = (
        doc[:m.start(2)] + new_inner + doc[m.end(2):]).encode('utf-8')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, blob in parts.items():
            z.writestr(name, blob)
    return buf.getvalue()
