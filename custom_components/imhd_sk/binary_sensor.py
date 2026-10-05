"""Diagnostic connectivity sensor per stop."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_STOP_ID, REFRESH_SECONDS
from .sensor import stop_device_info
from .source import StopSource


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([ImhdConnectivitySensor(entry, entry.runtime_data)])


class ImhdConnectivitySensor(BinarySensorEntity):
    """On = connected and fresh data is arriving."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Pripojenie"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"last_update"})

    def __init__(self, entry, source: StopSource) -> None:
        self._source = source
        self._attr_is_on = None
        self._attr_extra_state_attributes = {}
        self._attr_unique_id = f"{entry.data[CONF_STOP_ID]}_connectivity"
        self._attr_device_info = stop_device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._source.add_listener(self._handle_update))
        # Staleness changes with time alone, without any event.
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._handle_tick, timedelta(seconds=REFRESH_SECONDS)
            )
        )
        self._handle_update()

    @callback
    def _handle_tick(self, _now) -> None:
        self._handle_update()

    @callback
    def _handle_update(self) -> None:
        src = self._source
        new_on = src.healthy
        attrs: dict[str, Any] = {
            "mode": src.mode,
            "connected": src.connected,
            "last_update": src.last_update.isoformat() if src.last_update else None,
            "reconnects": src.reconnects,
            "last_error": src.last_error,
        }
        if (
            self._attr_extra_state_attributes
            and new_on == self._attr_is_on
            and attrs == self._attr_extra_state_attributes
        ):
            return
        self._attr_is_on = new_on
        self._attr_extra_state_attributes = attrs
        if self.hass is not None:
            self.async_write_ha_state()
