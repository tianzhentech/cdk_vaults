"""
In-process SSE event bus.

The payload only carries resource names. Clients fetch the latest data through
the normal authenticated APIs after receiving an update event.
"""

import itertools
import queue
import threading
import time

CHANNELS = {"admin", "public"}
_subscribers = {channel: set() for channel in CHANNELS}
_lock = threading.Lock()
_event_ids = itertools.count(1)


def subscribe(channel: str) -> queue.Queue:
    if channel not in CHANNELS:
        raise ValueError(f"unknown SSE channel: {channel}")
    subscriber = queue.Queue(maxsize=100)
    with _lock:
        _subscribers[channel].add(subscriber)
    return subscriber


def unsubscribe(channel: str, subscriber: queue.Queue):
    if channel not in CHANNELS:
        return
    with _lock:
        _subscribers[channel].discard(subscriber)


def publish(event: str, data: dict | None = None, audience: str = "all"):
    payload = {
        "id": next(_event_ids),
        "event": event,
        "data": data or {},
    }
    channels = tuple(CHANNELS) if audience == "all" else (audience,)
    with _lock:
        subscribers = [
            subscriber
            for channel in channels
            for subscriber in _subscribers.get(channel, ())
        ]

    for subscriber in subscribers:
        try:
            subscriber.put_nowait(payload)
        except queue.Full:
            try:
                subscriber.get_nowait()
            except queue.Empty:
                pass
            try:
                subscriber.put_nowait(payload)
            except queue.Full:
                pass


def publish_update(resources: list[str] | tuple[str, ...] | set[str], audience: str = "all"):
    publish(
        "update",
        {
            "resources": sorted(set(resources)),
            "ts": time.time(),
        },
        audience=audience,
    )
