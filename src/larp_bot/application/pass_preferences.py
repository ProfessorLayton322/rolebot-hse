from __future__ import annotations

from collections.abc import Sequence

from larp_bot.domain.models import Platform, UserPassPreference
from larp_bot.domain.security import participant_key


def event_pass_preferences(
    profiles: Sequence[UserPassPreference],
    *,
    event_id: str,
    secret: str,
    active_keys: set[str],
) -> dict[str, bool | None]:
    """Resolve reciprocal links in memory, using the latest global profile's answer."""

    vk_profiles = {profile.user_id: profile for profile in profiles if profile.platform is Platform.VK}
    owners: dict[tuple[int, str], list[UserPassPreference]] = {}
    for profile in profiles:
        if profile.platform is Platform.TELEGRAM and profile.linked_vk_id is not None and profile.telegram_handle:
            owners.setdefault((profile.linked_vk_id, profile.telegram_handle), []).append(profile)
    linked: dict[tuple[Platform, int], UserPassPreference] = {}
    for (vk_id, handle), candidates in owners.items():
        vk = vk_profiles.get(vk_id)
        if len(candidates) != 1 or vk is None or vk.telegram_handle != handle:
            continue
        telegram = candidates[0]
        latest = max((telegram, vk), key=lambda row: (row.updated_at, row.platform, row.user_id))
        linked[(telegram.platform, telegram.user_id)] = latest
        linked[(vk.platform, vk.user_id)] = latest
    answers: dict[str, bool | None] = {}
    for profile in profiles:
        key = participant_key(secret, profile.platform, profile.user_id, event_id)
        if key in active_keys:
            current = linked.get((profile.platform, profile.user_id), profile)
            answers[key] = current.needs_pass
    return answers
