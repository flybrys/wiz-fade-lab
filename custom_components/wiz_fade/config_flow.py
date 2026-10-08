"""Manual, per-device opt-in for WiZ Fade Lab."""

import ipaddress

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from pywizlight import wizlight
from pywizlight.exceptions import WizLightConnectionError, WizLightNotKnownBulb

from .const import CONF_LOW_DIM, CONF_TRANSITIONS, DOMAIN


def option_schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_TRANSITIONS, default=defaults.get(CONF_TRANSITIONS, False)
            ): bool,
            vol.Optional(CONF_LOW_DIM, default=defaults.get(CONF_LOW_DIM, False)): bool,
        }
    )


class WizFadeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure one bulb without touching the built-in WiZ integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return WizFadeOptionsFlow()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                ipaddress.IPv4Address(user_input[CONF_HOST])
            except ValueError:
                errors[CONF_HOST] = "invalid_ip"
            else:
                bulb = wizlight(user_input[CONF_HOST])
                try:
                    bulb_type = await bulb.get_bulbtype()
                    mac = await bulb.getMac()
                    if (
                        not bulb_type.features.brightness
                        or bulb_type.features.dual_head
                    ):
                        errors["base"] = "unsupported"
                    elif user_input.get(CONF_LOW_DIM) and not bulb_type.features.color:
                        errors["base"] = "unsupported_low_dimming"
                    else:
                        await self.async_set_unique_id(mac)
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(
                            title=f"WiZ Fade {mac[-6:]}", data=user_input
                        )
                except (
                    OSError,
                    TimeoutError,
                    WizLightConnectionError,
                    WizLightNotKnownBulb,
                ):
                    errors["base"] = "cannot_connect"
                finally:
                    await bulb.async_close()
        schema = option_schema(user_input or {}).extend({vol.Required(CONF_HOST): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


class WizFadeOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        defaults = dict(self.config_entry.data) | dict(self.config_entry.options)
        return self.async_show_form(step_id="init", data_schema=option_schema(defaults))
