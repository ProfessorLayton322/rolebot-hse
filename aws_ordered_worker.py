"""AWS SQS partial-batch entry point."""

from larp_bot.functions.aws_ordered_worker import handler

__all__ = ["handler"]
