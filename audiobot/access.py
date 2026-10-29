from __future__ import annotations

from typing import Any


def has_access(
    message: Any,
    *,
    allowed_channel_id: int,
    required_role_id: int,
) -> bool:
    author = getattr(message, "author", None)
    if author is None or getattr(author, "bot", False):
        return False

    channel = getattr(message, "channel", None)
    if channel is None or getattr(channel, "id", None) != allowed_channel_id:
        return False

    if getattr(message, "guild", None) is None:
        return False

    roles = getattr(author, "roles", ())
    return any(getattr(role, "id", None) == required_role_id for role in roles)
