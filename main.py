"""Yandex gateway Function entry point (production is webhook-only)."""

from larp_bot.functions.gateway.handler import handler

__all__ = ["handler"]
