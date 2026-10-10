from __future__ import annotations

from typing import Any

from scripts import verify_sqs_cutover


class _Sqs:
    def __init__(self, depths: dict[str, int]) -> None:
        self.depths = depths

    def get_queue_attributes(self, *, QueueUrl: str, AttributeNames: list[str]) -> dict[str, Any]:
        assert AttributeNames == list(verify_sqs_cutover.QUEUE_ATTRIBUTES)
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": str(self.depths[QueueUrl]),
                "ApproximateNumberOfMessagesNotVisible": "0",
            }
        }


def test_verify_accepts_consumed_probe_without_last_processing_result(monkeypatch: Any) -> None:
    monkeypatch.setattr(verify_sqs_cutover, "event_mapping", lambda client: {"State": "Enabled"})
    monkeypatch.setattr(verify_sqs_cutover.time, "sleep", lambda seconds: None)

    verify_sqs_cutover.verify(
        _Sqs({"primary": 0, "dlq": 0}),
        object(),
        "primary",
        "dlq",
        "probe-id",
    )


def test_verify_rejects_a_probe_in_the_dead_letter_queue(monkeypatch: Any) -> None:
    monkeypatch.setattr(verify_sqs_cutover, "event_mapping", lambda client: {"State": "Enabled"})
    monkeypatch.setattr(verify_sqs_cutover.time, "sleep", lambda seconds: None)

    try:
        verify_sqs_cutover.verify(
            _Sqs({"primary": 0, "dlq": 1}),
            object(),
            "primary",
            "dlq",
            "probe-id",
        )
    except RuntimeError as exc:
        assert str(exc) == "the enabled Lambda event source did not consume the cutover FIFO probe"
    else:
        raise AssertionError("the verifier accepted a non-empty dead-letter queue")
