from __future__ import annotations

from larp_bot.domain.models import Platform
from larp_bot.domain.security import participant_key, sign_request


def test_participant_key_is_opaque_and_scoped_to_event() -> None:
    a = participant_key("secret", Platform.TELEGRAM, 123456, "event-a1")
    b = participant_key("secret", Platform.TELEGRAM, 123456, "event-b1")
    assert a != b
    assert "123456" not in a
    assert len(a) == 43


def test_request_signature_covers_body_and_path() -> None:
    signature = sign_request("secret", "1", "r", "POST", "/path", b"body")
    assert signature != sign_request("secret", "1", "r", "POST", "/path", b"other")
    assert signature != sign_request("secret", "1", "r", "POST", "/other", b"body")
