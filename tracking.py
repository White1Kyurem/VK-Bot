"""Monotonic elapsed-time accounting; disconnected periods are never replayed."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

from config import Settings
from storage import Store


@dataclass(frozen=True)
class VoiceParticipant:
    user_id: int
    channel_id: int
    is_bot: bool = False
    deafened: bool = False


def eligible_users(participants: Iterable[VoiceParticipant], settings: Settings,
                   afk_channel_id: int | None) -> set[int]:
    channels: dict[int, set[int]] = {}
    for person in participants:
        if person.is_bot or person.channel_id in settings.ignored_channel_ids:
            continue
        if settings.exclude_afk and person.channel_id == afk_channel_id:
            continue
        if settings.exclude_deafened and person.deafened:
            continue
        channels.setdefault(person.channel_id, set()).add(person.user_id)
    return {
        user_id for humans in channels.values() if len(humans) >= settings.min_humans
        for user_id in humans
    }


class Tracker:
    """Use only on the bot's event loop. Methods contain no await points."""
    def __init__(self, store: Store, guild_id: int, clock=time.monotonic):
        self.store = store
        self.guild_id = guild_id
        self.clock = clock
        self.active: dict[int, float] = {}

    def refresh(self, eligible: set[int]) -> set[int]:
        now = self.clock()
        stopped = set(self.active) - eligible
        started = eligible - set(self.active)
        # Commit first: if a write fails, checkpoints must not advance.
        self.store.ensure_users(self.guild_id, started)
        self.store.add_seconds(self.guild_id, {
            user_id: max(0, now - self.active[user_id]) for user_id in stopped
        })
        for user_id in stopped:
            del self.active[user_id]
        for user_id in started:
            self.active[user_id] = now
        return stopped | started

    def checkpoint(self, user_ids: set[int] | None = None) -> set[int]:
        now = self.clock()
        users = set(self.active) if user_ids is None else set(self.active) & user_ids
        self.store.add_seconds(self.guild_id, {
            user_id: max(0, now - self.active[user_id]) for user_id in users
        })
        for user_id in users:
            self.active[user_id] = now
        return users

    def total(self, user_id: int) -> float:
        pending = max(0, self.clock() - self.active[user_id]) if user_id in self.active else 0
        return self.store.total(self.guild_id, user_id) + pending

    def totals(self) -> dict[int, float]:
        now = self.clock()
        totals = self.store.totals(self.guild_id)
        for user_id, start in self.active.items():
            totals[user_id] = totals.get(user_id, 0) + max(0, now - start)
        return totals

    def abandon_unconfirmed(self) -> None:
        # On a lost Gateway connection we cannot know when people left.
        # Keep the most recent confirmed DB checkpoint and start fresh on resume.
        self.active.clear()

    def stop(self) -> None:
        self.refresh(set())
