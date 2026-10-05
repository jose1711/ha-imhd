"""Sensors: next departure for the stop, per platform, per line × platform."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_LINES,
    CONF_MAX_DEPARTURES,
    CONF_MODE,
    CONF_LINE_SENSORS,
    CONF_PLATFORM_LABELS,
    CONF_PLATFORM_SENSORS,
    CONF_STOP_ID,
    DEFAULT_MAX_DEPARTURES,
    DOMAIN,
    MODE_MANUAL,
    MODE_POLL,
    REFRESH_SECONDS,
)
from .source import PollSource, StopSource


def parse_lines(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").replace(";", ",").split(",") if x.strip()]


def _line_key(line: str) -> tuple:
    """Natural order: 4 < 9 < 39 < 61 < N33."""
    digits = "".join(ch for ch in line if ch.isdigit())
    return (not line.isdigit(), int(digits) if digits else 0, line)


def stop_device_info(entry) -> DeviceInfo:
    """One device per stop; shared by sensors and the connectivity binary sensor."""
    stop_id = entry.data[CONF_STOP_ID]
    return DeviceInfo(
        identifiers={(DOMAIN, str(stop_id))},
        name=entry.data.get(CONF_NAME) or f"Zastávka {stop_id}",
        manufacturer="imhd.sk",
        model={
            MODE_POLL: "Virtuálna tabuľa (pravidelne)",
            MODE_MANUAL: "Virtuálna tabuľa (na požiadanie)",
        }.get(entry.data.get(CONF_MODE), "Virtuálna tabuľa (priebežne)"),
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://imhd.sk/ba/",
    )


def _platform_uid(stop_id, platform: str) -> str:
    return f"{stop_id}_platform_{platform}"


def _line_platform_uid(stop_id, line: str, platform: str) -> str:
    return f"{stop_id}_line_{line}_platform_{platform}"


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback
) -> None:
    source: StopSource = entry.runtime_data
    stop_id = entry.data[CONF_STOP_ID]
    line_set = set(parse_lines(entry.options.get(CONF_LINES, ""))) or None
    max_dep = int(entry.options.get(CONF_MAX_DEPARTURES, DEFAULT_MAX_DEPARTURES))
    want_platforms = entry.options.get(CONF_PLATFORM_SENSORS, True)
    want_lines = entry.options.get(CONF_LINE_SENSORS, True)

    registry = er.async_get(hass)
    reg_entries = er.async_entries_for_config_entry(registry, entry.entry_id)

    # Old per-line sensors without direction (≤ 0.5) were replaced by line × platform.
    old_line_prefix = f"{stop_id}_line_"
    for reg in reg_entries:
        if reg.unique_id.startswith(old_line_prefix) and "_platform_" not in reg.unique_id:
            registry.async_remove(reg.entity_id)

    entities: list[ImhdDepartureSensor] = [
        ImhdDepartureSensor(entry, source, max_dep, lines=line_set)
    ]

    # Platforms and lines are only known from data. Restore the ones registered
    # earlier right away (so they don't flap after a restart), add new ones live.
    known_platforms: set[str] = set()
    known_pairs: set[tuple[str, str]] = set()
    plat_prefix = f"{stop_id}_platform_"
    for reg in reg_entries:
        uid = reg.unique_id
        if uid.startswith(plat_prefix):
            known_platforms.add(uid.removeprefix(plat_prefix))
        elif uid.startswith(old_line_prefix) and "_platform_" in uid:
            line, plat = uid.removeprefix(old_line_prefix).rsplit("_platform_", 1)
            if not line_set or line in line_set:
                known_pairs.add((line, plat))

    def _platform_entities(platforms) -> list[ImhdDepartureSensor]:
        return [
            ImhdDepartureSensor(entry, source, max_dep, lines=line_set, platform=p)
            for p in sorted(platforms)
        ]

    def _pair_entities(pairs) -> list[ImhdDepartureSensor]:
        return [
            ImhdDepartureSensor(entry, source, max_dep, line=ln, platform=p)
            for ln, p in sorted(pairs, key=lambda x: (_line_key(x[0]), x[1]))
        ]

    def _current_pairs() -> set[tuple[str, str]]:
        return {
            (ln, p)
            for ln, p in source.line_platforms
            if not line_set or ln in line_set
        }

    if want_platforms:
        known_platforms.update(source.platforms)
        entities += _platform_entities(known_platforms)
    if want_lines:
        known_pairs.update(_current_pairs())
        entities += _pair_entities(known_pairs)
    async_add_entities(entities)

    @callback
    def _check_new() -> None:
        new_entities: list[ImhdDepartureSensor] = []
        if want_platforms:
            new = set(source.platforms) - known_platforms
            known_platforms.update(new)
            new_entities += _platform_entities(new)
        if want_lines:
            new_pairs = _current_pairs() - known_pairs
            known_pairs.update(new_pairs)
            new_entities += _pair_entities(new_pairs)
        if new_entities:
            async_add_entities(new_entities)

    if want_platforms or want_lines:
        entry.async_on_unload(source.add_listener(_check_new))


class ImhdDepartureSensor(SensorEntity):
    """Timestamp of the next departure; full list in attributes."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:bus-clock"
    # Change every minute; keep them out of the recorder database.
    _unrecorded_attributes = frozenset(
        {"departures", "minutes", "last_update", "connected", "info", "destinations"}
    )

    def __init__(
        self,
        entry,
        source: StopSource,
        max_departures: int,
        *,
        lines: set[str] | None = None,
        line: str | None = None,
        platform: str | None = None,
    ) -> None:
        self._source = source
        self._max = max_departures
        self._line = line
        self._lines = {line} if line else lines
        self._platform = platform
        self._labels: dict[str, str] = dict(entry.data.get(CONF_PLATFORM_LABELS) or {})
        self._platform_label = (
            entry.data.get(CONF_PLATFORM_LABELS, {}).get(platform) if platform else None
        )
        stop_id = entry.data[CONF_STOP_ID]
        plat_name = self._platform_label or platform
        if line is not None and platform is not None:
            self._attr_name = f"Linka {line} · {plat_name}"
            self._attr_unique_id = _line_platform_uid(stop_id, line, platform)
        elif platform is not None:
            self._attr_name = f"Nástupište {plat_name}"
            self._attr_unique_id = _platform_uid(stop_id, platform)
            self._attr_icon = "mdi:sign-direction"
        else:
            self._attr_name = "Najbližší odchod"
            self._attr_unique_id = f"{stop_id}_next"
        self._attr_device_info = stop_device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._source.add_listener(self._handle_update))
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._handle_tick, timedelta(seconds=REFRESH_SECONDS)
            )
        )
        self._handle_update()

    async def async_update(self) -> None:
        """homeassistant.update_entity: fetch now in polling mode."""
        if isinstance(self._source, PollSource):
            await self._source.refresh()

    @callback
    def _handle_tick(self, _now) -> None:
        self._handle_update()

    @callback
    def _handle_update(self) -> None:
        now = datetime.now(timezone.utc)
        platforms = {self._platform} if self._platform is not None else None
        deps = self._source.departures(self._lines, platforms)
        nxt = deps[0] if deps else None
        self._attr_available = self._source.available
        self._attr_native_value = nxt.time if nxt else None
        attrs: dict[str, Any] = {
            "stop_id": self._source.stop_id,
            "connected": self._source.connected,
            "last_update": (
                self._source.last_update.isoformat() if self._source.last_update else None
            ),
        }
        if self._platform is not None:
            if self._line is not None:
                attrs["line"] = self._line
            attrs["platform"] = self._platform
            if self._platform_label:
                attrs["platform_label"] = self._platform_label
            # Destinations served from this platform = which direction it is.
            dests: list[str] = []
            for d in self._source.departures(
                {self._line} if self._line else None, platforms
            ):
                if d.destination and d.destination not in dests:
                    dests.append(d.destination)
            attrs["destinations"] = dests
        labels = self._labels
        attrs["departures"] = [
            {**d.as_dict(now), "platform_label": labels.get(d.platform)}
            if labels.get(d.platform)
            else d.as_dict(now)
            for d in deps[: self._max]
        ]
        if nxt:
            attrs.update(
                line=nxt.line,
                destination=nxt.destination,
                delay=nxt.delay,
                realtime=nxt.realtime,
                minutes=nxt.as_dict(now)["minutes"],
            )
        if self._source.info:
            attrs["info"] = self._source.info
        self._attr_extra_state_attributes = attrs
        if self.hass is not None:
            self.async_write_ha_state()
