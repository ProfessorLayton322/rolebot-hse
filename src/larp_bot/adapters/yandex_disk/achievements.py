from __future__ import annotations

import math
from io import BytesIO
from pathlib import Path
from typing import cast
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

PLACEHOLDER_TEXT = "ЛОГОС"
ACHIEVEMENT_FONT = "Oswald Light"
ACHIEVEMENT_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "assets" / "roleclub_achievement_basic.pptx"
SLIDE_PATH = "ppt/slides/slide1.xml"
EMU_PER_POINT = 12_700
MIN_FONT_SIZE = 100  # DrawingML uses hundredths of a point; its minimum is 1 pt.

ET.register_namespace("a", DRAWING_NS)
ET.register_namespace("p", PRESENTATION_NS)
ET.register_namespace("r", RELATIONSHIPS_NS)


class AchievementTemplateError(RuntimeError):
    pass


def _tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _normalized_game_name(game_name: str) -> str:
    normalized = " ".join(game_name.split())
    if not normalized:
        raise ValueError("achievement game name must not be empty")
    return normalized


def _text_width_em(text: str) -> float:
    """Conservative Oswald-width estimate used to select a one-line font size."""

    narrow = frozenset(".,:;!|ijlіїтгчТГЧІЇ")
    wide = frozenset("mwMWжшщыюФЖШЩЫЮ@%")
    width = 0.0
    for character in text:
        if character.isspace():
            width += 0.32
        elif character in narrow:
            width += 0.38
        elif character in wide:
            width += 0.78
        else:
            width += 0.6
    return max(width, 1.0)


def _font_size(shape: ET.Element, text: str, original_size: int) -> int:
    transform = shape.find(f"./{_tag(PRESENTATION_NS, 'spPr')}/{_tag(DRAWING_NS, 'xfrm')}")
    extent = None if transform is None else transform.find(f"./{_tag(DRAWING_NS, 'ext')}")
    body = shape.find(f"./{_tag(PRESENTATION_NS, 'txBody')}/{_tag(DRAWING_NS, 'bodyPr')}")
    if extent is None or body is None:
        return original_size
    width = int(extent.get("cx", "0"))
    left_margin = int(body.get("lIns", "91440"))
    right_margin = int(body.get("rIns", "91440"))
    available_points = max((width - left_margin - right_margin) / EMU_PER_POINT, 1.0)
    # Leave a small safety margin because the embedded font's glyph metrics are
    # not exposed through DrawingML and differ slightly between renderers.
    fitted = math.floor(available_points / _text_width_em(text) * 100 * 0.94)
    return max(MIN_FONT_SIZE, min(original_size, fitted))


def _font_element(properties: ET.Element, name: str) -> ET.Element:
    tag = _tag(DRAWING_NS, name)
    current = properties.find(f"./{tag}")
    if current is not None:
        return current
    element = ET.Element(tag)
    # Font declarations precede hyperlinks and extension lists in DrawingML.
    following_tags = {
        _tag(DRAWING_NS, "hlinkClick"),
        _tag(DRAWING_NS, "hlinkMouseOver"),
        _tag(DRAWING_NS, "extLst"),
    }
    for index, child in enumerate(properties):
        if child.tag in following_tags:
            properties.insert(index, element)
            break
    else:
        properties.append(element)
    return element


def _apply_font(properties: ET.Element, size: int) -> None:
    properties.set("sz", str(size))
    properties.set("b", "0")
    for name in ("latin", "ea", "cs", "sym"):
        _font_element(properties, name).set("typeface", ACHIEVEMENT_FONT)


def _run_properties(run: ET.Element) -> ET.Element:
    properties = run.find(f"./{_tag(DRAWING_NS, 'rPr')}")
    if properties is None:
        properties = ET.Element(_tag(DRAWING_NS, "rPr"))
        run.insert(0, properties)
    return properties


def _keep_shape_text_on_one_line(shape: ET.Element) -> None:
    body = shape.find(f"./{_tag(PRESENTATION_NS, 'txBody')}/{_tag(DRAWING_NS, 'bodyPr')}")
    if body is None:
        raise AchievementTemplateError("achievement placeholder has no text-body properties")
    body.set("wrap", "none")
    for name in ("spAutoFit", "normAutofit", "noAutofit"):
        child = body.find(f"./{_tag(DRAWING_NS, name)}")
        if child is not None:
            body.remove(child)
    body.append(ET.Element(_tag(DRAWING_NS, "noAutofit")))


def _render_slide(slide: bytes, game_name: str) -> bytes:
    try:
        root = ET.fromstring(slide)  # noqa: S314 - the deployment-owned template is trusted input
    except ET.ParseError as error:
        raise AchievementTemplateError("achievement template slide is not valid XML") from error

    parent = {child: element for element in root.iter() for child in element}
    placeholders = [element for element in root.iter(_tag(DRAWING_NS, "t")) if PLACEHOLDER_TEXT in (element.text or "")]
    if not placeholders:
        raise AchievementTemplateError("achievement template has no ЛОГОС placeholders")

    shapes: dict[int, tuple[ET.Element, list[ET.Element]]] = {}
    for text in placeholders:
        ancestor = text
        while ancestor in parent and ancestor.tag != _tag(PRESENTATION_NS, "sp"):
            ancestor = parent[ancestor]
        if ancestor.tag != _tag(PRESENTATION_NS, "sp"):
            raise AchievementTemplateError("achievement placeholder is not inside a text shape")
        shape_key = id(ancestor)
        shapes.setdefault(shape_key, (ancestor, []))[1].append(text)

    for shape, text_nodes in shapes.values():
        _keep_shape_text_on_one_line(shape)
        runs: list[ET.Element] = []
        original_sizes: list[int] = []
        for text in text_nodes:
            text.text = (text.text or "").replace(PLACEHOLDER_TEXT, game_name)
            run = parent[text]
            if run.tag != _tag(DRAWING_NS, "r"):
                raise AchievementTemplateError("achievement placeholder is not inside a text run")
            properties = _run_properties(run)
            runs.append(run)
            original_sizes.append(int(properties.get("sz", "1200")))
        size = _font_size(shape, game_name, min(original_sizes))
        for run in runs:
            _apply_font(_run_properties(run), size)
        paragraphs = {parent[run] for run in runs}
        for paragraph in paragraphs:
            end_properties = paragraph.find(f"./{_tag(DRAWING_NS, 'endParaRPr')}")
            if end_properties is None:
                end_properties = ET.SubElement(paragraph, _tag(DRAWING_NS, "endParaRPr"))
            _apply_font(end_properties, size)

    return cast(bytes, ET.tostring(root, encoding="utf-8", xml_declaration=True))


def achievement_presentation_bytes(template: bytes, game_name: str) -> bytes:
    """Render a new PPTX while leaving the supplied template bytes untouched."""

    normalized_name = _normalized_game_name(game_name)
    try:
        with ZipFile(BytesIO(template)) as source:
            slides = sorted(
                name for name in source.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            )
            if slides != [SLIDE_PATH]:
                raise AchievementTemplateError("achievement template must contain exactly one slide")
            rendered_slide = _render_slide(source.read(SLIDE_PATH), normalized_name)
            output = BytesIO()
            with ZipFile(output, "w") as rendered:
                rendered.comment = source.comment
                for item in source.infolist():
                    content = rendered_slide if item.filename == SLIDE_PATH else source.read(item.filename)
                    rendered.writestr(item, content)
    except BadZipFile as error:
        raise AchievementTemplateError("achievement template is not a valid PPTX archive") from error
    return output.getvalue()
