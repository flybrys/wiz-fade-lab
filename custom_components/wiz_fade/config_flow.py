"""Manual, bulk import and on-demand discovery setup for WiZ Fade Lab."""

import asyncio
import ipaddress
import logging

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from pywizlight import wizlight
from pywizlight.exceptions import WizLightError

from .const import CONF_LOW_DIM, CONF_TRANSITIONS, DOMAIN
from .discovery import (
    Candidate,
    discover_devices,
    existing_devices,
    mac_key,
    unconfigured_devices,
)

_LOGGER = logging.getLogger(__name__)
CONF_DEVICES = "devices"


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
    """Configure bulbs without modifying the built-in WiZ integration."""

    VERSION = 1

    def __init__(self):
        self._candidates: dict[str, Candidate] = {}
        self._scan_task: asyncio.Task | None = None
        self._add_task: asyncio.Task | None = None
        self._selection: dict = {}
        self._scan_error = False
        self._summary = {"added": "0", "skipped": "0", "failed": "None"}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return WizFadeOptionsFlow()

    async def async_step_user(self, user_input=None):
        return self.async_show_menu(
            step_id="user", menu_options=["existing", "discover", "manual"]
        )

    async def async_step_existing(self, user_input=None):
        self._candidates = unconfigured_devices(self.hass, existing_devices(self.hass))
        if not self._candidates:
            return self.async_abort(reason="no_existing_devices")
        return await self.async_step_select()

    async def async_step_discover(self, user_input=None):
        if self._scan_task is None:
            self._scan_task = self.hass.async_create_task(self._scan())
        if not self._scan_task.done():
            return self.async_show_progress(
                step_id="discover",
                progress_action="scanning",
                progress_task=self._scan_task,
            )
        self._scan_task.result()
        return self.async_show_progress_done(next_step_id="select")

    async def _scan(self):
        try:
            self._candidates = unconfigured_devices(
                self.hass, await discover_devices(self.hass)
            )
        except Exception:
            _LOGGER.exception("WiZ discovery failed")
            self._scan_error = True

    async def async_step_select(self, user_input=None):
        if self._scan_error:
            return self.async_abort(reason="discovery_failed")
        if not self._candidates:
            return self.async_abort(reason="no_devices_found")
        errors = {}
        if user_input is not None:
            devices = user_input.get(CONF_DEVICES, [])
            if not devices or any(key not in self._candidates for key in devices):
                errors[CONF_DEVICES] = "select_devices"
            else:
                self._selection = user_input
                return await self.async_step_adding()
        labels = {key: device.label for key, device in self._candidates.items()}
        schema = option_schema(user_input or {}).extend(
            {vol.Required(CONF_DEVICES, default=list(labels)): cv.multi_select(labels)}
        )
        return self.async_show_form(step_id="select", data_schema=schema, errors=errors)

    async def async_step_adding(self, user_input=None):
        if self._add_task is None:
            self._add_task = self.hass.async_create_task(self._add_selected())
        if not self._add_task.done():
            return self.async_show_progress(
                step_id="adding", progress_action="adding", progress_task=self._add_task
            )
        self._add_task.result()
        return self.async_show_progress_done(next_step_id="complete")

    async def _add_selected(self):
        added = skipped = 0
        failed = []
        options = option_schema({})(
            {
                key: self._selection[key]
                for key in (CONF_LOW_DIM, CONF_TRANSITIONS)
                if key in self._selection
            }
        )
        # A separate import flow per bulb keeps existing per-device options intact.
        for key in dict.fromkeys(self._selection[CONF_DEVICES]):
            device = self._candidates[key]
            try:
                result = await self.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": SOURCE_IMPORT, "wiz_fade_batch": self.flow_id},
                    data={
                        CONF_HOST: device.host,
                        "mac": device.mac,
                        "name": device.name,
                        **options,
                    },
                )
            except asyncio.CancelledError:
                self._abort_child_flows()
                raise
            except Exception:
                self._abort_child_flows()
                _LOGGER.exception("Could not add a selected WiZ device")
                failed.append(f"{device.name}: unexpected_error")
                continue
            if result["type"] == FlowResultType.CREATE_ENTRY:
                added += 1
            elif result.get("reason") in ("already_configured", "already_in_progress"):
                skipped += 1
            else:
                failed.append(
                    f"{device.name}: {result.get('reason', 'cannot_connect')}"
                )
        self._summary = {
            "added": str(added),
            "skipped": str(skipped),
            "failed": "; ".join(failed) or "None",
        }

    def _abort_child_flows(self):
        for flow in self.hass.config_entries.flow.async_progress_by_handler(
            DOMAIN,
            include_uninitialized=True,
            match_context={"wiz_fade_batch": self.flow_id},
        ):
            self.hass.config_entries.flow.async_abort(flow["flow_id"])

    async def async_step_complete(self, user_input=None):
        return self.async_abort(
            reason="bulk_complete", description_placeholders=self._summary
        )

    async def _validate(self, data: dict, expected_mac: str | None = None):
        """Read identity and capabilities only; never change the light during setup."""
        try:
            ipaddress.IPv4Address(data[CONF_HOST])
        except (ValueError, KeyError):
            return None, "invalid_ip"
        if expected_mac and not unconfigured_devices(
            self.hass, {expected_mac: Candidate(data[CONF_HOST], expected_mac, "")}
        ):
            return None, "already_configured"
        bulb = wizlight(data[CONF_HOST])
        try:
            async with asyncio.timeout(15):
                bulb_type = await bulb.get_bulbtype()
                mac = mac_key(await bulb.getMac())
            if not mac or (expected_mac and mac != expected_mac):
                return None, "wrong_device"
            if not bulb_type.features.brightness or bulb_type.features.dual_head:
                return None, "unsupported"
            if data.get(CONF_LOW_DIM) and not bulb_type.features.color:
                return None, "unsupported_low_dimming"
            await self.async_set_unique_id(mac)
            self._abort_if_unique_id_configured()
            return mac, None
        except (OSError, TimeoutError, WizLightError):
            return None, "cannot_connect"
        finally:
            await bulb.async_close()

    async def async_step_import(self, user_input):
        expected = mac_key(user_input.get("mac"))
        if not expected:
            return self.async_abort(reason="wrong_device")
        _, error = await self._validate(user_input, expected)
        if error:
            return self.async_abort(reason=error)
        return self.async_create_entry(
            title=f"{user_input['name']} Fade Lab",
            data={
                CONF_HOST: user_input[CONF_HOST],
                **option_schema({})(
                    {
                        key: user_input[key]
                        for key in (CONF_LOW_DIM, CONF_TRANSITIONS)
                        if key in user_input
                    }
                ),
            },
        )

    async def async_step_manual(self, user_input=None):
        errors = {}
        if user_input is not None:
            mac, error = await self._validate(user_input)
            if error:
                errors[CONF_HOST if error == "invalid_ip" else "base"] = error
            else:
                return self.async_create_entry(
                    title=f"WiZ Fade {mac[-6:]}", data=user_input
                )
        schema = option_schema(user_input or {}).extend({vol.Required(CONF_HOST): str})
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)


class WizFadeOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        defaults = dict(self.config_entry.data) | dict(self.config_entry.options)
        return self.async_show_form(step_id="init", data_schema=option_schema(defaults))
