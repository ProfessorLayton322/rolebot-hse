"""AWS Lambda Function URL entry point."""

from larp_bot.functions.gateway.handler import handler

__all__ = ["handler"]
