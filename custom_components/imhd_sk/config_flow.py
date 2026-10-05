"""Config flow: action-only entry, or a stop with sensors (push or polling)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import ImhdError, fetch_board
from .stops import find, get_stops, resolve_stop_id, sorted_options
from .const import (
    CONF_PLATFORM_LABELS,
    CONF_STOP,
    CONF_ENTRY_TYPE,
    CONF_LINES,
    CONF_MAX_DEPARTURES,
    CONF_LINE_SENSORS,
    CONF_PLATFORM_SENSORS,
    CONF_MODE,
    CONF_SCAN_INTERVAL,
    CONF_STOP_ID,
    DEFAULT_MAX_DEPARTURES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ENTRY_TYPE_ACTION,
    ENTRY_TYPE_STOP,
    MODE_MANUAL,
    MODE_POLL,
    MODE_PUSH,
)


def _options_schema(defaults: dict[str, Any], mode: str) -> dict:
    schema: dict = {
        vol.Optional(CONF_LINES, default=defaults.get(CONF_LINES, "")): str,
        vol.Optional(
            CONF_MAX_DEPARTURES,
            default=defaults.get(CONF_MAX_DEPARTURES, DEFAULT_MAX_DEPARTURES),
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
        vol.Optional(
            CONF_PLATFORM_SENSORS,
            default=defaults.get(CONF_PLATFORM_SENSORS, True),
        ): bool,
        vol.Optional(
            CONF_LINE_SENSORS,
            default=defaults.get(CONF_LINE_SENSORS, True),
        ): bool,
    }
    if mode == MODE_POLL:
        schema[
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        ] = vol.All(vol.Coerce(int), vol.Range(min=1, max=60))
    return schema


class ImhdConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add the action, or a stop with sensors."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(step_id="user", menu_options=["stop", "action"])

    async def async_step_action(self, user_input: dict[str, Any] | None = None):
        """Only makes the integration load so the action is available."""
        await self.async_set_unique_id("action")
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(
                title="imhd.sk – akcia", data={CONF_ENTRY_TYPE: ENTRY_TYPE_ACTION}
            )
        return self.async_show_form(step_id="action")

    async def async_step_stop(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        session = async_get_clientsession(self.hass)
        stops = await get_stops(session)

        if user_input is not None:
            stop_id = await resolve_stop_id(session, str(user_input[CONF_STOP]))
            if stop_id is None:
                errors[CONF_STOP] = "invalid_stop"
            else:
                await self.async_set_unique_id(str(stop_id))
                self._abort_if_unique_id_configured()
                try:
                    await fetch_board(stop_id, timeout=10, http_session=session)
                except ImhdError:
                    errors["base"] = "cannot_connect"
                else:
                    info = find(stops, stop_id)
                    name = (
                        user_input.get(CONF_NAME)
                        or (info.name if info else None)
                        or f"Zastávka {stop_id}"
                    )
                    mode = user_input[CONF_MODE]
                    options = {
                        CONF_LINES: user_input.get(CONF_LINES, ""),
                        CONF_MAX_DEPARTURES: user_input.get(
                            CONF_MAX_DEPARTURES, DEFAULT_MAX_DEPARTURES
                        ),
                        CONF_PLATFORM_SENSORS: user_input.get(CONF_PLATFORM_SENSORS, True),
                        CONF_LINE_SENSORS: user_input.get(CONF_LINE_SENSORS, True),
                    }
                    if mode == MODE_POLL:
                        options[CONF_SCAN_INTERVAL] = DEFAULT_SCAN_INTERVAL
                    return self.async_create_entry(
                        title=name,
                        data={
                            CONF_ENTRY_TYPE: ENTRY_TYPE_STOP,
                            CONF_STOP_ID: stop_id,
                            CONF_NAME: name,
                            CONF_MODE: mode,
                            CONF_PLATFORM_LABELS: info.platform_labels if info else {},
                        },
                        options=options,
                    )

        home = None
        if self.hass.config.latitude is not None and self.hass.config.longitude is not None:
            home = (self.hass.config.latitude, self.hass.config.longitude)
        if stops:
            # Searchable dropdown; custom value still accepts a URL or a number.
            stop_field: Any = SelectSelector(
                SelectSelectorConfig(
                    options=sorted_options(stops, home),
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        else:
            stop_field = str

        schema = vol.Schema(
            {
                vol.Required(CONF_STOP): stop_field,
                vol.Optional(CONF_NAME, default=""): str,
                vol.Required(CONF_MODE, default=MODE_PUSH): SelectSelector(
                    SelectSelectorConfig(
                        options=[MODE_PUSH, MODE_POLL, MODE_MANUAL],
                        translation_key="mode",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                **_options_schema({}, MODE_PUSH),
            }
        )
        return self.async_show_form(
            step_id="stop",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ImhdOptionsFlow()

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        return config_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_STOP


class ImhdOptionsFlow(OptionsFlow):
    """Line filter, count, polling interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        mode = self.config_entry.data.get(CONF_MODE, MODE_PUSH)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(_options_schema(dict(self.config_entry.options), mode)),
        )
