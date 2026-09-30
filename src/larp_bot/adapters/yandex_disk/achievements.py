from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import cast

import pypdfium2 as pdfium
from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.util import Pt

PLACEHOLDER_TEXT = "ЛОГОС"
ACHIEVEMENT_FONT = "Oswald"
ACHIEVEMENT_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "assets" / "pdf_achievements_usual.pdf"
RENDER_SCALE = 3
TITLE_BOX_WIDTH_POINTS = 180.0
TITLE_BOX_HEIGHT_POINTS = 24.0
TITLE_HORIZONTAL_PADDING_POINTS = 6.0
FULL_SIZE_CHARACTER_LIMIT = 20
MIN_FONT_SIZE_POINTS = 1.0
_PDFIUM_LOCK = Lock()


class AchievementTemplateError(RuntimeError):
    pass


@dataclass(frozen=True)
class _Placeholder:
    center_x: float
    center_y: float
    font_size: float


def _normalized_game_name(game_name: str) -> str:
    normalized = " ".join(game_name.split())
    if not normalized:
        raise ValueError("achievement game name must not be empty")
    return normalized


def _fitted_font_size(text: str, original_size: float) -> float:
    character_count = max(len(text), 1)
    fitted = original_size * min(1.0, FULL_SIZE_CHARACTER_LIMIT / character_count)
    fitted = math.floor(fitted * 4) / 4
    return max(MIN_FONT_SIZE_POINTS, min(original_size, fitted))


def _placeholder(text_object: pdfium.PdfTextObj, page_height: float) -> _Placeholder:
    left, pdf_bottom, right, pdf_top = text_object.get_bounds()
    matrix = text_object.get_matrix()
    vertical_scale = math.hypot(matrix.c, matrix.d)
    font_size = text_object.get_font_size() * vertical_scale
    top = page_height - pdf_top
    bottom = page_height - pdf_bottom
    return _Placeholder(
        center_x=(left + right) / 2,
        center_y=(top + bottom) / 2,
        font_size=font_size,
    )


def _render_background(page: pdfium.PdfPage) -> Image.Image:
    bitmap = page.render(scale=RENDER_SCALE, optimize_mode="print")
    try:
        image = bitmap.to_pil().convert("RGB").copy()
    finally:
        bitmap.close()
    return cast(Image.Image, image)


def _presentation_bytes(
    page_width: float,
    page_height: float,
    background: Image.Image,
    placeholders: list[_Placeholder],
    game_name: str,
) -> bytes:
    presentation = Presentation()
    presentation.slide_width = Pt(page_width)
    presentation.slide_height = Pt(page_height)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])

    background_bytes = BytesIO()
    background.save(background_bytes, format="PNG", compress_level=6)
    background_bytes.seek(0)
    slide.shapes.add_picture(background_bytes, 0, 0, width=Pt(page_width), height=Pt(page_height))

    for placeholder in placeholders:
        text_box = slide.shapes.add_textbox(
            Pt(placeholder.center_x - TITLE_BOX_WIDTH_POINTS / 2),
            Pt(placeholder.center_y - TITLE_BOX_HEIGHT_POINTS / 2),
            Pt(TITLE_BOX_WIDTH_POINTS),
            Pt(TITLE_BOX_HEIGHT_POINTS),
        )
        text_box.fill.background()
        text_box.line.fill.background()
        frame = text_box.text_frame
        frame.clear()
        frame.auto_size = MSO_AUTO_SIZE.NONE
        frame.word_wrap = False
        frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        frame.margin_left = Pt(TITLE_HORIZONTAL_PADDING_POINTS)
        frame.margin_right = Pt(TITLE_HORIZONTAL_PADDING_POINTS)
        frame.margin_top = 0
        frame.margin_bottom = 0
        paragraph = frame.paragraphs[0]
        paragraph.alignment = PP_ALIGN.CENTER
        run = paragraph.add_run()
        run.text = game_name
        run.font.name = ACHIEVEMENT_FONT
        run.font.size = Pt(_fitted_font_size(game_name, placeholder.font_size))
        run.font.bold = False
        run.font.color.rgb = RGBColor(0, 0, 0)  # type: ignore[no-untyped-call]

    output = BytesIO()
    presentation.save(output)
    return output.getvalue()


def _render_presentation(template: bytes, game_name: str) -> bytes:
    try:
        with pdfium.PdfDocument(template) as document:
            if len(document) != 1:
                raise AchievementTemplateError("achievement template must contain exactly one page")
            page = document[0]
            try:
                page_width, page_height = page.get_size()
                text_page = page.get_textpage()
                try:
                    placeholder_objects = [
                        text_object
                        for text_object in page.get_objects(max_depth=1, textpage=text_page)
                        if isinstance(text_object, pdfium.PdfTextObj)
                        and text_object.extract().strip() == PLACEHOLDER_TEXT
                    ]
                    placeholders = [_placeholder(text_object, page_height) for text_object in placeholder_objects]
                finally:
                    text_page.close()
                if not placeholders:
                    raise AchievementTemplateError("achievement template has no ЛОГОС placeholders")
                for text_object in placeholder_objects:
                    page.remove_obj(text_object)
                    text_object.close()
                page.gen_content()
                background = _render_background(page)
            finally:
                page.close()
    except pdfium.PdfiumError as error:
        raise AchievementTemplateError("achievement template is not a valid PDF") from error
    return _presentation_bytes(page_width, page_height, background, placeholders, game_name)


def achievement_presentation_bytes(template: bytes, game_name: str) -> bytes:
    """Render the PDF template as a PPTX while leaving the source bytes untouched."""

    normalized_name = _normalized_game_name(game_name)
    # PDFium is process-global and not thread-safe. Achievement rendering runs
    # in worker threads, so serialize PDF operations within a warm function.
    with _PDFIUM_LOCK:
        return _render_presentation(template, normalized_name)
