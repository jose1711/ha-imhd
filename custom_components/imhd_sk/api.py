"""One-shot fetch of an imhd.sk virtual departure board.

Connects via socket.io, subscribes to the stop (`tabStart`), takes the first
`tabs` push and disconnects. Disconnecting is the unsubscribe.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import socketio

from .const import BASE_URL, DEFAULT_SETTLE, DEPARTED_GRACE_SECONDS, SOCKETIO_PATH

_LOGGER = logging.getLogger(__name__)


class ImhdError(Exception):
    """Board could not be fetched."""


@dataclass(slots=True)
class Departure:
    """One departure from the board."""

    line: str
    destination: str
    time: datetime
    delay: int  # minutes, negative = ahead of schedule
    realtime: bool  # False = timetable only ("cp")
    platform: str
    vehicle: str | None
    last_stop: str | None
    stuck: bool

    def as_dict(self, now: datetime) -> dict[str, Any]:
        """Serialise for a service response."""
        return {
            "line": self.line,
            "destination": self.destination,
            "time": self.time.isoformat(),
            "minutes": max(0, int((self.time - now).total_seconds() // 60)),
            "delay": self.delay,
            "realtime": self.realtime,
            "platform": self.platform,
            "vehicle": self.vehicle,
            "last_stop": self.last_stop,
            "stuck": self.stuck,
        }


def parse_tabs(data: Any) -> list[Departure]:
    """Parse a `tabs` payload: list of platforms, each with a `tab` list."""
    result: list[Departure] = []
    if not isinstance(data, list):
        return result
    for platform in data:
        if not isinstance(platform, dict):
            continue
        plat = str(platform.get("nastupiste", ""))
        for item in platform.get("tab") or []:
            try:
                cas = int(item["cas"])
            except (KeyError, TypeError, ValueError):
                continue
            vehicle = item.get("issi")
            last_stop = item.get("predoslaZstr")
            if isinstance(last_stop, str):
                last_stop = last_stop.removeprefix("Bratislava, ")
            result.append(
                Departure(
                    line=str(item.get("linka", "?")),
                    destination=str(item.get("cielStr") or item.get("konecnaZstr") or ""),
                    time=datetime.fromtimestamp(cas / 1000, tz=timezone.utc),
                    delay=int(item.get("casDelta") or 0),
                    realtime=item.get("typ", "cp") != "cp",
                    platform=plat,
                    vehicle=None if vehicle in (None, "", "offline") else str(vehicle),
                    last_stop=last_stop or None,
                    stuck=bool(item.get("uviaznute", False)),
                )
            )
    result.sort(key=lambda d: d.time)
    return result


class Board:
    """Departure board assembled from `tabs` messages.

    Each `tabs` message carries only some platforms (`nastupiste`). It replaces
    the departures of those platforms; platforms not in the message are kept.
    """

    def __init__(self) -> None:
        self._platforms: dict[str, list[Departure]] = {}
        self.messages = 0

    def update(self, data: Any) -> None:
        new: dict[str, list[Departure]] = {}
        for dep in parse_tabs(data):
            new.setdefault(dep.platform, []).append(dep)
        # A platform present in the message but with an empty `tab` means "no departures".
        if isinstance(data, list):
            for platform in data:
                if isinstance(platform, dict):
                    new.setdefault(str(platform.get("nastupiste", "")), [])
        self._platforms.update(new)
        self.messages += 1

    @property
    def departures(self) -> list[Departure]:
        out = [d for deps in self._platforms.values() for d in deps]
        out.sort(key=lambda d: d.time)
        return out


def filter_departures(
    deps: list[Departure],
    lines: set[str] | None = None,
    limit: int | None = None,
    platforms: set[str] | None = None,
) -> list[Departure]:
    """Drop departed connections, apply line/platform filter and limit."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=DEPARTED_GRACE_SECONDS)
    out = [
        d
        for d in deps
        if d.time >= cutoff
        and (not lines or d.line in lines)
        and (not platforms or d.platform in platforms)
    ]
    return out[:limit] if limit else out


def platform_summary(deps: list[Departure]) -> dict[str, list[str]]:
    """Platform -> destinations served (helps tell the directions apart)."""
    out: dict[str, list[str]] = {}
    for d in deps:
        dests = out.setdefault(d.platform, [])
        if d.destination and d.destination not in dests:
            dests.append(d.destination)
    return dict(sorted(out.items()))


def clean_info(data: Any) -> list[Any]:
    """iText payload without empty entries."""
    items = data if isinstance(data, list) else [data]
    return [i for i in items if i not in (None, "", [], {})]


async def fetch_board(
    stop_id: int,
    *,
    timeout: float = 10,
    settle: float = DEFAULT_SETTLE,
    http_session=None,
) -> tuple[list[Departure], list[Any]]:
    """Connect, subscribe and collect `tabs` until the board settles, then disconnect.

    The board is considered complete when no new `tabs` message arrived for
    `settle` seconds. `timeout` caps the whole operation.
    """
    kwargs: dict[str, Any] = {"reconnection": False}
    if http_session is not None:
        kwargs["http_session"] = http_session
    sio = socketio.AsyncClient(**kwargs)
    board = Board()
    got_tabs = asyncio.Event()
    info: list[Any] = []

    @sio.event
    async def connect() -> None:
        await sio.emit("tabStart", [stop_id, "*"])
        await sio.emit("infoStart")

    @sio.on("tabs")
    async def on_tabs(data) -> None:
        board.update(data)
        got_tabs.set()

    @sio.on("iText")
    async def on_itext(data) -> None:
        info[:] = clean_info(data)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        await sio.connect(
            BASE_URL,
            socketio_path=SOCKETIO_PATH,
            transports=["websocket", "polling"],
            wait_timeout=timeout,
        )
        # First message: wait up to the deadline.
        await asyncio.wait_for(got_tabs.wait(), max(0.1, deadline - loop.time()))
        # Further messages: stop after `settle` s of silence or at the deadline.
        while (remaining := deadline - loop.time()) > 0:
            got_tabs.clear()
            try:
                await asyncio.wait_for(got_tabs.wait(), min(settle, remaining))
            except TimeoutError:
                break
    except TimeoutError as err:
        raise ImhdError(f"Zastávka {stop_id}: imhd.sk neposlalo tabuľu do {timeout} s") from err
    except socketio.exceptions.ConnectionError as err:
        raise ImhdError(f"Nepodarilo sa pripojiť na imhd.sk: {err}") from err
    finally:
        try:
            await sio.disconnect()
        except Exception:  # noqa: BLE001
            pass
    _LOGGER.debug("Stop %s: %d tabs messages collected", stop_id, board.messages)
    return board.departures, info


async def fetch_departures(
    stop_id: int,
    *,
    lines: set[str] | None = None,
    limit: int | None = None,
    platforms: set[str] | None = None,
    timeout: float = 10,
    settle: float = DEFAULT_SETTLE,
    http_session=None,
) -> dict[str, Any]:
    """One-shot fetch formatted as a service response."""
    deps, info = await fetch_board(
        stop_id, timeout=timeout, settle=settle, http_session=http_session
    )
    now = datetime.now(timezone.utc)
    all_deps = deps
    deps = filter_departures(deps, lines, limit, platforms)
    _LOGGER.debug("Stop %s: %d departures", stop_id, len(deps))
    return {
        "stop_id": stop_id,
        "fetched_at": now.isoformat(),
        "departures": [d.as_dict(now) for d in deps],
        "platforms": platform_summary(all_deps),
        "info": info,
    }
