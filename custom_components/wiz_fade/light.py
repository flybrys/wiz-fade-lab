"""Home Assistant light adapter using firmware-native transitions."""

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
    filter_supported_color_modes,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pywizlight.scenes import SCENES, get_id_from_scene_name

from .engine import CHANNELS


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([WizFadeLight(entry.runtime_data, entry.title)])


class WizFadeLight(CoordinatorEntity, LightEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = "Fade Lab"

    def __init__(self, coordinator, name):
        super().__init__(coordinator)
        self.engine = coordinator.engine
        bulb = self.engine.bulb
        self._attr_unique_id = f"{bulb.mac}_fade_lab"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, bulb.mac)},
            manufacturer="WiZ",
            model=bulb.bulbtype.name,
            sw_version=bulb.bulbtype.fw_version,
            name=name,
        )
        features = bulb.bulbtype.features
        modes = {ColorMode.BRIGHTNESS}
        if features.color_tmp:
            modes.add(ColorMode.COLOR_TEMP)
            self._attr_min_color_temp_kelvin = bulb.bulbtype.kelvin_range.min
            self._attr_max_color_temp_kelvin = bulb.bulbtype.kelvin_range.max
        if features.color:
            modes.add(
                ColorMode.RGBWW if bulb.bulbtype.white_channels == 2 else ColorMode.RGBW
            )
        self._attr_supported_color_modes = filter_supported_color_modes(modes)
        self._attr_supported_features = LightEntityFeature(0)
        if self.engine.transitions:
            self._attr_supported_features |= LightEntityFeature.TRANSITION
        # Effects are kept out of this experimental light; use the built-in entity.
        self._update_attrs()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self.engine.low_dim and (last := await self.async_get_last_state()):
            if saved := last.attributes.get("scaled_state"):
                self.engine.restore_scaled(saved)
        self._update_attrs()

    @callback
    def _update_attrs(self):
        state = self.engine.state
        scaled = self.engine.scaled
        self._attr_is_on = state.get("state", False)
        self._attr_brightness = round(max(0, min(100, self.engine.percent)) * 255 / 100)
        self._attr_color_temp_kelvin = None
        self._attr_rgbww_color = None
        self._attr_rgbw_color = None
        kelvin = scaled.kelvin if scaled else state.get("temp")
        if kelvin and ColorMode.COLOR_TEMP in self.supported_color_modes:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_color_temp_kelvin = kelvin
        elif any(key in state for key in CHANNELS):
            channels = (
                scaled.channels if scaled else tuple(state.get(k, 0) for k in CHANNELS)
            )
            if ColorMode.RGBWW in self.supported_color_modes:
                self._attr_color_mode = ColorMode.RGBWW
                self._attr_rgbww_color = tuple(channels)
            elif ColorMode.RGBW in self.supported_color_modes:
                self._attr_color_mode = ColorMode.RGBW
                self._attr_rgbw_color = tuple(channels[:4])
            else:
                self._attr_color_mode = ColorMode.BRIGHTNESS
        elif len(self.supported_color_modes) == 1:
            self._attr_color_mode = next(iter(self.supported_color_modes))
        else:
            self._attr_color_mode = ColorMode.BRIGHTNESS
        self._attr_extra_state_attributes = {
            "native_minimum_percent": self.engine.minimum,
            "wire_brightness_percent": state.get("dimming"),
            "experimental_low_dimming": self.engine.low_dim,
            "scaled_state": self.engine.metadata(),
            "active_wiz_scene": SCENES.get(state.get("sceneId")),
        }

    @callback
    def _handle_coordinator_update(self):
        self._update_attrs()
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs):
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness == 0:
            await self.async_turn_off(**kwargs)
            return
        channels = kwargs.get(ATTR_RGBWW_COLOR)
        if channels is None and ATTR_RGBW_COLOR in kwargs:
            channels = (*kwargs[ATTR_RGBW_COLOR], 0)
        scene = None
        if ATTR_EFFECT in kwargs:
            scene = get_id_from_scene_name(kwargs[ATTR_EFFECT])
            if scene == 1000:
                raise HomeAssistantError("Use the built-in WiZ integration for Rhythm")
        try:
            await self.engine.turn_on(
                percent=brightness * 100 / 255 if brightness is not None else None,
                kelvin=kwargs.get(ATTR_COLOR_TEMP_KELVIN),
                channels=tuple(channels) if channels is not None else None,
                scene=scene,
                transition=kwargs.get(ATTR_TRANSITION),
            )
        except ValueError as error:
            raise HomeAssistantError(str(error)) from error
        self.coordinator.async_set_updated_data(dict(self.engine.state))

    async def async_turn_off(self, **kwargs):
        try:
            await self.engine.turn_off(kwargs.get(ATTR_TRANSITION))
        except ValueError as error:
            raise HomeAssistantError(str(error)) from error
        self.coordinator.async_set_updated_data(dict(self.engine.state))

    async def async_will_remove_from_hass(self):
        await self.engine.close()
        await super().async_will_remove_from_hass()
