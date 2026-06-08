"""In-memory pub/sub + presence for encrypted share-link chat.

Each chat channel (keyed by share_link_id) has a Hub holding the live SSE
subscribers. Messages and presence are fan-out only — the actual ciphertext is
persisted (for 'persist' channels) by chat_share; this layer is purely the
real-time transport and is intentionally stateless across restarts (a reconnect
replays the persisted backlog).

Thread-safety: SSE generators run on the event loop, but the send/file endpoints
are sync `def` handlers that run in FastAPI's threadpool. So a Hub captures the
running loop on first subscribe, and publish() (callable from ANY thread) hands
each delivery back to that loop via call_soon_threadsafe — never touching an
asyncio.Queue from off-loop.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Callable


class _Sub:
    """A single SSE subscriber with a role and a delivery queue."""

    __slots__ = ("queue", "role")

    def __init__(self, role: str):
        """Initialize a subscriber.

        Args:
            role: 'owner' or 'guest'.
        """
        self.role = role                       # 'owner' | 'guest'
        self.queue: asyncio.Queue = asyncio.Queue()


class Hub:
    """Per-channel in-memory state: live subscribers, the event loop, and the sequence counter."""

    def __init__(self):
        """Initialize an empty hub for a chat channel."""
        self.subs: set[_Sub] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lock = threading.Lock()
        self.seq: int | None = None            # lazily seeded from the DB max on first append
        self.last_owner_notify = 0.0           # monotonic ts of the last "guest waiting" push (debounce)

    def present(self) -> tuple[bool, bool]:
        """Return (owner_present, guest_present) based on current subscribers.

        Returns:
            Tuple of booleans indicating whether an owner and/or guest subscriber is connected.
        """
        with self.lock:
            roles = {s.role for s in self.subs}
        return ("owner" in roles, "guest" in roles)


_hubs: dict[int, Hub] = {}
_glock = threading.Lock()


def hub(link_id: int) -> Hub:
    """Return (creating if needed) the Hub for a chat channel.

    Args:
        link_id: The share_links.id identifying the channel.

    Returns:
        The Hub instance for the channel.
    """
    with _glock:
        h = _hubs.get(link_id)
        if h is None:
            h = Hub()
            _hubs[link_id] = h
        return h


def present(link_id: int) -> tuple[bool, bool]:
    """(owner_present, guest_present) for the channel right now."""
    with _glock:
        h = _hubs.get(link_id)
    return h.present() if h else (False, False)


async def subscribe(link_id: int, role: str) -> tuple[Hub, _Sub]:
    """Register a new SSE subscriber for a channel and capture the running event loop.

    Args:
        link_id: The share_links.id identifying the channel.
        role: 'owner' or 'guest'.

    Returns:
        A (hub, sub) tuple for use with unsubscribe and the SSE generator.
    """
    h = hub(link_id)
    sub = _Sub(role)
    with h.lock:
        h.subs.add(sub)
        if h.loop is None:
            h.loop = asyncio.get_running_loop()
    return h, sub


def unsubscribe(h: Hub, sub: _Sub) -> None:
    """Remove a subscriber from its hub.

    Args:
        h: The Hub the subscriber belongs to.
        sub: The subscriber to remove.
    """
    with h.lock:
        h.subs.discard(sub)


def publish(link_id: int, event: dict, *, exclude: _Sub | None = None) -> None:
    """Fan an event out to every subscriber (optionally excluding one). Safe to call
    from any thread; deliveries are marshalled onto the channel's event loop.
    """
    with _glock:
        h = _hubs.get(link_id)
    if h is None or h.loop is None:
        return
    with h.lock:
        targets = [s for s in h.subs if s is not exclude]
    for s in targets:
        try:
            h.loop.call_soon_threadsafe(s.queue.put_nowait, event)
        except RuntimeError:               # loop shutting down — best-effort
            pass


def reset() -> None:
    """Drop all hub state (test helper; also fine to call to reclaim a closed channel)."""
    with _glock:
        _hubs.clear()


def next_seq(link_id: int, seed: Callable[[], int]) -> int:
    """Allocate the next monotonic sequence number for the channel. `seed` is called
    once (under the hub lock) to read the persisted MAX(seq) the first time the hub is
    used after a restart, so ordering survives across processes for persisted channels.
    """
    h = hub(link_id)
    with h.lock:
        if h.seq is None:
            h.seq = max(0, int(seed() or 0))
        h.seq += 1
        return h.seq
