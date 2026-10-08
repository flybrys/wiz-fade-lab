"""Experimental WiZ transitions and low dimming for individual test bulbs."""

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pywizlight import wizlight
from pywizlight.exceptions import WizLightConnectionError, WizLightMethodNotFound

from .const import CONF_LOW_DIM, CONF_TRANSITIONS, DOMAIN
from .engine import FadeEngine, parse_cct_table

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.LIGHT]


class FadeCoordinator(DataUpdateCoordinator[dict]):
    """Poll actual device state; do not invent successful brightness changes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, engine: FadeEngine):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=timedelta(seconds=5),
        )
        self.engine = engine
        self.engine.on_change = lambda: self.async_set_updated_data(dict(engine.state))
        self.engine.on_error = self._fade_error

    def _fade_error(self, error: Exception) -> None:
        _LOGGER.error("WiZ delayed off failed: %s", error)
        self.async_set_update_error(UpdateFailed(str(error)))

    async def _async_update_data(self) -> dict:
        try:
            await self.engine.refresh()
        except (OSError, TimeoutError, WizLightConnectionError, ValueError) as error:
            raise UpdateFailed(str(error)) from error
        return dict(self.engine.state)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    bulb = wizlight(entry.data[CONF_HOST])
    try:
        bulb_type = await bulb.get_bulbtype()
        mac = await bulb.getMac()
        if mac != entry.unique_id:
            raise ValueError("A different WiZ device is using this IP address")
        model_response = await bulb.getModelConfig()
        model = (model_response or {}).get("result", {})
        table = []
        low_dim = entry.options.get(CONF_LOW_DIM, entry.data.get(CONF_LOW_DIM, False))
        if low_dim:
            if not bulb_type.features.color or bulb_type.features.dual_head:
                raise ValueError(
                    "Channel scaling is only supported for single-head RGB bulbs"
                )
            try:
                table_response = await bulb.send(
                    {"method": "getCctTable", "params": {}}
                )
                table = parse_cct_table(table_response.get("result", {}))
            except WizLightMethodNotFound:
                _LOGGER.warning(
                    "No CCT table available; low dimming requires an explicit RGBWW color"
                )
        user_response = await bulb.getUserConfig()
        fade_out = user_response.get("result", {}).get("fadeOut", 1000)
        if type(fade_out) is not int or not 0 <= fade_out <= 3600000:
            raise ValueError("Invalid device fade-out setting")
        engine = FadeEngine(
            bulb,
            model,
            table,
            low_dim=low_dim,
            transitions=entry.options.get(
                CONF_TRANSITIONS, entry.data.get(CONF_TRANSITIONS, False)
            ),
            fade_out_ms=fade_out,
        )
        coordinator = FadeCoordinator(hass, entry, engine)
        await coordinator.async_config_entry_first_refresh()
    except Exception as error:
        await bulb.async_close()
        raise ConfigEntryNotReady(str(error)) from error
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.engine.close()
        await entry.runtime_data.engine.bulb.async_close()
        return True
    return False
