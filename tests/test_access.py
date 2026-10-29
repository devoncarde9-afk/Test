from __future__ import annotations

import unittest
from types import SimpleNamespace

from audiobot.access import has_access

ALLOWED_CHANNEL = 100
REQUIRED_ROLE = 200


def make_message(
    *,
    channel_id: int = ALLOWED_CHANNEL,
    role_ids: tuple[int, ...] = (REQUIRED_ROLE,),
    is_bot: bool = False,
    in_guild: bool = True,
):
    author = SimpleNamespace(
        bot=is_bot,
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
    )
    return SimpleNamespace(
        author=author,
        channel=SimpleNamespace(id=channel_id),
        guild=object() if in_guild else None,
    )


class HasAccessTests(unittest.TestCase):
    def check(self, message) -> bool:
        return has_access(
            message,
            allowed_channel_id=ALLOWED_CHANNEL,
            required_role_id=REQUIRED_ROLE,
        )

    def test_allows_required_role_in_allowed_channel(self):
        self.assertTrue(self.check(make_message()))

    def test_allows_required_role_among_others(self):
        self.assertTrue(self.check(make_message(role_ids=(1, REQUIRED_ROLE, 3))))

    def test_ignores_other_channels(self):
        self.assertFalse(self.check(make_message(channel_id=999)))

    def test_ignores_users_without_required_role(self):
        self.assertFalse(self.check(make_message(role_ids=(1, 2))))

    def test_ignores_higher_role_without_required_role(self):
        self.assertFalse(self.check(make_message(role_ids=(999999999999999999,))))

    def test_ignores_bots(self):
        self.assertFalse(self.check(make_message(is_bot=True)))

    def test_ignores_direct_messages(self):
        self.assertFalse(self.check(make_message(in_guild=False)))


if __name__ == "__main__":
    unittest.main()
