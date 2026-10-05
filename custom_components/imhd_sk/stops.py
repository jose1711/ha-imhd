"""Stop list and stop-id resolution.

Stop list comes from the backend of the open-source Transi app
(api.magicsk.eu). Its `id` is the same as imhd.sk's `st` parameter and it also
carries platform letters (e.g. platform 837 = "A").

Fallback: the user can paste any imhd.sk stop / live-board URL or a plain number.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from math import asin, cos, radians, sin, sqrt
import re
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

STOPS_URL = "https://api.magicsk.eu/stops"
CACHE_SECONDS = 24 * 3600

_ST_RE = re.compile(r"[?&;]st=(\d+)")


@dataclass(slots=True)
class Stop:
    id: int
    name: str
    city: str = ""
    type: str = ""
    lat: float | None = None
    lng: float | None = None
    platform_labels: dict[str, str] = field(default_factory=dict)  # platform id -> letter


_cache: tuple[float, list[Stop]] | None = None
_lock = asyncio.Lock()


def _parse(raw: Any) -> list[Stop]:
    stops: list[Stop] = []
    if not isinstance(raw, list):
        return stops
    for s in raw:
        try:
            stops.append(
                Stop(
                    id=int(s["id"]),
                    name=str(s["name"]),
                    city=str(s.get("city") or ""),
                    type=str(s.get("type") or ""),
                    lat=s.get("lat"),
                    lng=s.get("lng"),
                    platform_labels={
                        str(p["id"]): str(p["label"])
                        for p in (s.get("platform_labels") or [])
                        if isinstance(p, dict) and "id" in p and "label" in p
                    },
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return stops


async def get_stops(session: aiohttp.ClientSession, timeout: float = 10) -> list[Stop]:
    """Stop list, cached for a day. Returns [] when unavailable."""
    global _cache
    async with _lock:
        if _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        try:
            async with session.get(
                STOPS_URL, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                resp.raise_for_status()
                stops = _parse(await resp.json(content_type=None))
        except Exception as err:  # noqa: BLE001 - optional source, never fatal
            _LOGGER.debug("Stop list unavailable: %s", err)
            return _cache[1] if _cache else []
        if stops:
            _cache = (time.monotonic(), stops)
        return stops


def find(stops: list[Stop], stop_id: int) -> Stop | None:
    return next((s for s in stops if s.id == stop_id), None)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in metres."""
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371000 * asin(sqrt(a))


def sorted_options(
    stops: list[Stop], home: tuple[float, float] | None
) -> list[dict[str, str]]:
    """Select options, nearest to home first (alphabetical without a home)."""

    def dist(s: Stop) -> float | None:
        if home is None or s.lat is None or s.lng is None:
            return None
        return distance_m(home[0], home[1], float(s.lat), float(s.lng))

    rows = [(dist(s), s) for s in stops]
    rows.sort(key=lambda r: (r[0] is None, r[0] or 0, r[1].name))
    options = []
    for d, s in rows:
        label = s.name
        if d is not None:
            label += f" · {d / 1000:.1f} km" if d >= 1000 else f" · {int(round(d, -1))} m"
        options.append({"value": str(s.id), "label": label})
    return options


async def resolve_stop_id(session: aiohttp.ClientSession, text: str) -> int | None:
    """Turn a selected value, plain number or imhd.sk URL into a stop id."""
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    if m := _ST_RE.search(text):
        return int(m.group(1))
    if text.startswith(("http://", "https://")) and "imhd.sk" in text:
        # Stop page (…/zastavka/<name>/<hash>) links to its live board with ?st=<id>.
        try:
            async with session.get(text, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Cannot load %s: %s", text, err)
            return None
        if m := _ST_RE.search(html.replace("&amp;", "&")):
            return int(m.group(1))
    return None
