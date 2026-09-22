from __future__ import annotations

from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from larp_bot.adapters.yandex_disk.achievements import (
    ACHIEVEMENT_FONT,
    AchievementTemplateError,
    achievement_presentation_bytes,
)
from tests.conftest import achievement_template_bytes

DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"


def tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def test_achievement_renderer_replaces_every_placeholder_with_one_line_oswald_text() -> None:
    template = achievement_template_bytes()
    original = bytes(template)
    game_name = "Очень длинное\nназвание игры"

    rendered = achievement_presentation_bytes(template, game_name)

    assert template == original
    with ZipFile(BytesIO(rendered)) as archive:
        root = ET.fromstring(archive.read("ppt/slides/slide1.xml"))  # noqa: S314
        assert archive.read("ppt/media/unchanged.bin") == b"source asset"
    texts = [element.text for element in root.iter(tag(DRAWING_NS, "t"))]
    assert texts == ["Очень длинное название игры"] * 2
    assert not any("\n" in (text or "") for text in texts)

    bodies = root.findall(f".//{tag(DRAWING_NS, 'bodyPr')}")
    assert all(body.get("wrap") == "none" for body in bodies)
    assert all(body.find(f"./{tag(DRAWING_NS, 'noAutofit')}") is not None for body in bodies)
    assert all(body.find(f"./{tag(DRAWING_NS, 'spAutoFit')}") is None for body in bodies)

    properties = root.findall(f".//{tag(DRAWING_NS, 'rPr')}")
    assert properties
    assert all(int(item.get("sz", "1200")) < 1200 for item in properties)
    for item in properties:
        assert item.get("b") == "0"
        for font_tag in ("latin", "ea", "cs", "sym"):
            assert item.find(f"./{tag(DRAWING_NS, font_tag)}").get("typeface") == ACHIEVEMENT_FONT


def test_achievement_renderer_requires_the_single_template_slide() -> None:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", "<slide />")
        archive.writestr("ppt/slides/slide2.xml", "<slide />")

    with pytest.raises(AchievementTemplateError, match="exactly one slide"):
        achievement_presentation_bytes(output.getvalue(), "Игра")


def test_achievement_renderer_requires_a_placeholder() -> None:
    output = BytesIO()
    slide = f'<p:sld xmlns:p="{PRESENTATION_NS}" xmlns:a="{DRAWING_NS}"><p:cSld><p:spTree /></p:cSld></p:sld>'
    with ZipFile(output, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", slide)

    with pytest.raises(AchievementTemplateError, match="ЛОГОС"):
        achievement_presentation_bytes(output.getvalue(), "Игра")
