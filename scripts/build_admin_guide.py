from __future__ import annotations

import argparse
import html
import re
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    ListFlowable,
    ListItem,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "docs" / "admin-guide-ru.md"
DEFAULT_OUTPUT = ROOT / "docs" / "admin-guide-ru.pdf"

FONT_CANDIDATES = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/local/share/fonts/DejaVuSans.ttf"),
)


def _font_path(filename: str) -> Path:
    for regular in FONT_CANDIDATES:
        candidate = regular.with_name(filename)
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"DejaVu font not found: {filename}")


def register_fonts() -> None:
    pdfmetrics.registerFont(TTFont("GuideSans", str(_font_path("DejaVuSans.ttf"))))
    pdfmetrics.registerFont(TTFont("GuideSans-Bold", str(_font_path("DejaVuSans-Bold.ttf"))))
    pdfmetrics.registerFont(TTFont("GuideSans-Oblique", str(_font_path("DejaVuSans.ttf"))))
    pdfmetrics.registerFont(TTFont("GuideMono", str(_font_path("DejaVuSansMono.ttf"))))
    pdfmetrics.registerFontFamily(
        "GuideSans",
        normal="GuideSans",
        bold="GuideSans-Bold",
        italic="GuideSans-Oblique",
        boldItalic="GuideSans-Bold",
    )


def inline_markup(value: str) -> str:
    escaped = html.escape(value, quote=False)
    tokens: list[str] = []

    def save_token(match: re.Match[str]) -> str:
        tokens.append(match.group(1))
        return f"\x00{len(tokens) - 1}\x00"

    escaped = re.sub(r"`([^`]+)`", save_token, escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", escaped)
    for index, token in enumerate(tokens):
        escaped = escaped.replace(f"\x00{index}\x00", f'<font name="GuideMono">{token}</font>')
    return escaped


def guide_styles() -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "GuideTitle",
            parent=sample["Title"],
            fontName="GuideSans-Bold",
            fontSize=22,
            leading=26,
            textColor=colors.HexColor("#17324D"),
            alignment=TA_CENTER,
            spaceAfter=8 * mm,
        ),
        "subtitle": ParagraphStyle(
            "GuideSubtitle",
            parent=sample["Normal"],
            fontName="GuideSans",
            fontSize=10.8,
            leading=14,
            textColor=colors.HexColor("#40566B"),
            alignment=TA_CENTER,
            spaceAfter=3 * mm,
        ),
        "date": ParagraphStyle(
            "GuideDate",
            parent=sample["Normal"],
            fontName="GuideSans-Oblique",
            fontSize=9.5,
            leading=12,
            textColor=colors.HexColor("#667788"),
            alignment=TA_CENTER,
            spaceAfter=10 * mm,
        ),
        "h1": ParagraphStyle(
            "GuideH1",
            parent=sample["Heading1"],
            fontName="GuideSans-Bold",
            fontSize=17,
            leading=21,
            textColor=colors.HexColor("#17324D"),
            spaceBefore=6 * mm,
            spaceAfter=3 * mm,
            keepWithNext=True,
        ),
        "h2": ParagraphStyle(
            "GuideH2",
            parent=sample["Heading2"],
            fontName="GuideSans-Bold",
            fontSize=12.5,
            leading=16,
            textColor=colors.HexColor("#24577A"),
            spaceBefore=4.5 * mm,
            spaceAfter=2 * mm,
            keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "GuideBody",
            parent=sample["BodyText"],
            fontName="GuideSans",
            fontSize=9,
            leading=11.7,
            textColor=colors.HexColor("#1E2832"),
            alignment=TA_LEFT,
            spaceAfter=1.9 * mm,
        ),
        "list": ParagraphStyle(
            "GuideList",
            parent=sample["BodyText"],
            fontName="GuideSans",
            fontSize=8.9,
            leading=11.6,
            textColor=colors.HexColor("#1E2832"),
            leftIndent=0,
            firstLineIndent=0,
            spaceAfter=1.1 * mm,
        ),
        "table": ParagraphStyle(
            "GuideTable",
            parent=sample["BodyText"],
            fontName="GuideSans",
            fontSize=7.8,
            leading=10.2,
            textColor=colors.HexColor("#1E2832"),
        ),
        "table_head": ParagraphStyle(
            "GuideTableHead",
            parent=sample["BodyText"],
            fontName="GuideSans-Bold",
            fontSize=7.8,
            leading=10.2,
            textColor=colors.white,
        ),
    }


def _list_flow(items: list[str], styles: dict[str, ParagraphStyle], *, numbered: bool) -> ListFlowable:
    children = [
        ListItem(
            Paragraph(inline_markup(item), styles["list"]),
            leftIndent=5 * mm,
        )
        for item in items
    ]
    return ListFlowable(
        children,
        bulletType="1" if numbered else "bullet",
        start=1 if numbered else None,
        leftIndent=5 * mm,
        bulletFontName="GuideSans-Bold",
        bulletFontSize=8.8,
        bulletColor=colors.HexColor("#24577A"),
        spaceAfter=1.5 * mm,
    )


def _table_flow(rows: list[list[str]], styles: dict[str, ParagraphStyle], available_width: float) -> Table:
    rendered: list[list[Paragraph]] = []
    for row_index, row in enumerate(rows):
        style = styles["table_head"] if row_index == 0 else styles["table"]
        rendered.append([Paragraph(inline_markup(cell.strip()), style) for cell in row])

    column_count = len(rows[0])
    if column_count == 2:
        widths = [available_width * 0.31, available_width * 0.69]
    elif column_count == 3:
        widths = [available_width * 0.28, available_width * 0.46, available_width * 0.26]
    else:
        widths = [available_width / column_count] * column_count

    table = Table(rendered, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24577A")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#A8B6C3")),
                ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#F4F7F9")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7F9")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    table.spaceAfter = 3 * mm
    return table


def parse_markdown(source: str, styles: dict[str, ParagraphStyle], available_width: float) -> list[Any]:
    lines = source.splitlines()
    story: list[Any] = []
    index = 0
    title_parts = 0

    while index < len(lines):
        line = lines[index].rstrip()
        if not line:
            index += 1
            continue

        if line.startswith("# "):
            story.append(Paragraph(inline_markup(line[2:].strip()), styles["title"]))
            title_parts = 1
            index += 1
            continue

        if title_parts in {1, 2} and not line.startswith(("#", "-", "|")):
            style = styles["subtitle"] if title_parts == 1 else styles["date"]
            story.append(Paragraph(inline_markup(line), style))
            title_parts += 1
            index += 1
            continue

        if line.startswith("## "):
            story.append(Paragraph(inline_markup(line[3:].strip()), styles["h1"]))
            index += 1
            continue

        if line.startswith("### "):
            story.append(Paragraph(inline_markup(line[4:].strip()), styles["h2"]))
            index += 1
            continue

        if line.startswith("| "):
            table_lines: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                table_lines.append(lines[index].strip())
                index += 1
            rows = [[cell for cell in row.strip("|").split("|")] for row in table_lines]
            rows = [row for row in rows if not all(re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in row)]
            story.append(_table_flow(rows, styles, available_width))
            continue

        bullet_match = re.match(r"^-\s+(.+)$", line)
        numbered_match = re.match(r"^\d+\.\s+(.+)$", line)
        if bullet_match or numbered_match:
            numbered = numbered_match is not None
            items: list[str] = []
            pattern = re.compile(r"^\d+\.\s+(.+)$" if numbered else r"^-\s+(.+)$")
            while index < len(lines):
                match = pattern.match(lines[index].rstrip())
                if match is None:
                    break
                items.append(match.group(1))
                index += 1
            story.append(_list_flow(items, styles, numbered=numbered))
            continue

        paragraph_lines = [line]
        index += 1
        while index < len(lines):
            candidate = lines[index].rstrip()
            if not candidate or candidate.startswith(("#", "- ", "| ")) or re.match(r"^\d+\.\s+", candidate):
                break
            paragraph_lines.append(candidate)
            index += 1
        story.append(Paragraph(inline_markup(" ".join(paragraph_lines)), styles["body"]))

    return story


class GuideDocTemplate(BaseDocTemplate):
    def __init__(self, filename: str, **kwargs: Any) -> None:
        super().__init__(filename, **kwargs)
        frame = Frame(
            self.leftMargin,
            self.bottomMargin,
            self.width,
            self.height,
            id="guide",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        self.addPageTemplates(PageTemplate(id="guide", frames=[frame], onPage=self._decorate_page))

    @staticmethod
    def _decorate_page(pdf: canvas.Canvas, document: BaseDocTemplate) -> None:
        pdf.saveState()
        pdf.setTitle("Администрирование LARP-игр в боте")
        pdf.setAuthor("Rolebot HSE")
        pdf.setSubject("Руководство по административным функциям, чатам и рассылкам")
        pdf.setFont("GuideSans", 7.5)
        pdf.setFillColor(colors.HexColor("#667788"))
        pdf.drawString(document.leftMargin, 14 * mm, "Rolebot HSE · руководство администратора")
        pdf.drawRightString(A4[0] - document.rightMargin, 14 * mm, f"Страница {document.page}")
        pdf.setStrokeColor(colors.HexColor("#D7E0E7"))
        pdf.setLineWidth(0.4)
        pdf.line(document.leftMargin, 18 * mm, A4[0] - document.rightMargin, 18 * mm)
        pdf.restoreState()


def invariant_canvas(filename: str, **kwargs: Any) -> canvas.Canvas:
    kwargs["invariant"] = 1
    kwargs["pageCompression"] = 1
    return canvas.Canvas(filename, **kwargs)


def build(source_path: Path, output_path: Path) -> None:
    register_fonts()
    styles = guide_styles()
    document = GuideDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=15 * mm,
        bottomMargin=22 * mm,
        title="Администрирование LARP-игр в боте",
        author="Rolebot HSE",
    )
    story = parse_markdown(source_path.read_text(encoding="utf-8"), styles, document.width)
    story.insert(3, Spacer(1, 1))
    document.build(story, canvasmaker=invariant_canvas)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Russian administrator guide PDF")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    build(args.source, args.output)


if __name__ == "__main__":
    main()
