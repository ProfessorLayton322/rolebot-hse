from __future__ import annotations

from larp_bot.domain.models import BotIdentity, Platform, TelegramUser, User

from .ports import UserRepository


def local_identity(user: User) -> BotIdentity:
    if isinstance(user, TelegramUser):
        return BotIdentity(platform=Platform.TELEGRAM, platform_user_id=user.tg_id)
    return BotIdentity(platform=Platform.VK, platform_user_id=user.vk_id)


async def global_users(users: UserRepository, user: User) -> tuple[User, ...]:
    """Return the mutually linked local profiles belonging to one person."""

    linked = await users.find_linked(user)
    return (user,) if linked is None else (user, linked)


async def global_identities(users: UserRepository, user: User) -> tuple[BotIdentity, ...]:
    return tuple(local_identity(profile) for profile in await global_users(users, user))


async def identities_for_local_user(
    users: UserRepository | None,
    platform: Platform,
    user_id: int,
) -> tuple[BotIdentity, ...]:
    local = BotIdentity(platform=platform, platform_user_id=user_id)
    if users is None:
        return (local,)
    user = await users.get(platform, user_id)
    if user is None:
        return (local,)
    return await global_identities(users, user)
