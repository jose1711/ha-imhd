"""Refresh button for stops in polling / manual mode."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_STOP_ID
from .sensor import stop_device_info
from .source import PollSource


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback
) -> None:
    source = entry.runtime_data
    # Push mode is always live; a refresh button would do nothing there.
    if isinstance(source, PollSource):
        async_add_entities([ImhdRefreshButton(entry, source)])


class ImhdRefreshButton(ButtonEntity):
    """Fetch the board now."""

    _attr_has_entity_name = True
    _attr_name = "Obnoviť"
    _attr_icon = "mdi:refresh"

    def __init__(self, entry, source: PollSource) -> None:
        self._source = source
        self._attr_unique_id = f"{entry.data[CONF_STOP_ID]}_refresh"
        self._attr_device_info = stop_device_info(entry)

    async def async_press(self) -> None:
        await self._source.refresh()
