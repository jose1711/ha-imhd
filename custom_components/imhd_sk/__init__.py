"""imhd.sk departures as a one-shot action."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from datetime import timedelta

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady

from .api import ImhdError, fetch_departures
from .const import (
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_PLATFORM_LABELS,
    CONF_SCAN_INTERVAL,
    CONF_STOP_ID,
    DEFAULT_SCAN_INTERVAL,
    ENTRY_TYPE_STOP,
    MODE_MANUAL,
    MODE_POLL,
    ATTR_LIMIT,
    ATTR_LINES,
    ATTR_PLATFORMS,
    ATTR_STOP_ID,
    ATTR_SETTLE,
    ATTR_TIMEOUT,
    DEFAULT_SETTLE,
    DEFAULT_LIMIT,
    DEFAULT_TIMEOUT,
    DOMAIN,
    SERVICE_GET_DEPARTURES,
)

from .source import PollSource, PushSource
from .stops import find, get_stops, resolve_stop_id

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]


def _lines(value) -> list[str]:
    """Accept a list or a comma separated string."""
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    return [str(v).strip() for v in cv.ensure_list(value) if str(v).strip()]


SERVICE_SCHEMA = vol.Schema(
    {
        # Number, or an imhd.sk stop / live-board URL.
        vol.Required(ATTR_STOP_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional(ATTR_LINES): _lines,
        vol.Optional(ATTR_PLATFORMS): _lines,
        vol.Optional(ATTR_LIMIT, default=DEFAULT_LIMIT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
        vol.Optional(ATTR_TIMEOUT, default=DEFAULT_TIMEOUT): vol.All(
            vol.Coerce(float), vol.Range(min=2, max=60)
        ),
        vol.Optional(ATTR_SETTLE, default=DEFAULT_SETTLE): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=30)
        ),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the action."""

    async def _get_departures(call: ServiceCall) -> ServiceResponse:
        lines = call.data.get(ATTR_LINES)
        platforms = call.data.get(ATTR_PLATFORMS)
        session = async_get_clientsession(hass)
        stop_id = await resolve_stop_id(session, call.data[ATTR_STOP_ID])
        if stop_id is None:
            raise HomeAssistantError(f"Neznáma zastávka: {call.data[ATTR_STOP_ID]}")
        try:
            result = await fetch_departures(
                stop_id,
                lines=set(lines) if lines else None,
                platforms=set(platforms) if platforms else None,
                limit=call.data[ATTR_LIMIT],
                timeout=call.data[ATTR_TIMEOUT],
                settle=call.data[ATTR_SETTLE],
                http_session=session,
            )
        except ImhdError as err:
            raise HomeAssistantError(str(err)) from err
        info = find(await get_stops(session), stop_id)
        if info is not None:
            result["stop_name"] = info.name
            result["platform_labels"] = info.platform_labels
        return result

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_DEPARTURES,
        _get_departures,
        schema=SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


def _is_stop(entry: ConfigEntry) -> bool:
    return entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_STOP


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Action-only entry: nothing to do. Stop entry: start source + sensors."""
    if not _is_stop(entry):
        return True

    session = async_get_clientsession(hass)
    stop_id = int(entry.data[CONF_STOP_ID])
    mode = entry.data.get(CONF_MODE)
    if mode == MODE_POLL:
        minutes = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        source = PollSource(hass, stop_id, timedelta(minutes=minutes), session)
    elif mode == MODE_MANUAL:
        source = PollSource(hass, stop_id, None, session)
    else:
        source = PushSource(hass, stop_id, session)
    try:
        await source.start()
    except ImhdError as err:
        await source.stop()
        raise ConfigEntryNotReady(f"imhd.sk nedostupné: {err}") from err

    # Platform letters (837 -> "A"). Entries created before this existed get them now.
    if CONF_PLATFORM_LABELS not in entry.data:
        info = find(await get_stops(session), stop_id)
        if info is not None:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_PLATFORM_LABELS: info.platform_labels}
            )

    entry.runtime_data = source
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not _is_stop(entry):
        return True
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.stop()
    return ok


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
