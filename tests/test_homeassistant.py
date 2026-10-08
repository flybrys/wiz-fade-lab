"""Exercise the real HA entity adapter on Linux with the target HA release."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.light import ColorMode, LightEntityFeature
from homeassistant.exceptions import HomeAssistantError
from test_engine import make_engine

from custom_components.wiz_fade.config_flow import option_schema
from custom_components.wiz_fade.light import WizFadeLight


def make_light(low=True, transitions=True):
    coordinator = MagicMock()
    coordinator.engine = engine = make_engine(low=low, transitions=transitions)
    engine.bulb.mac = "aabbccddeeff"
    engine.bulb.bulbtype = SimpleNamespace(
        name="ESP24_SHRGB_01",
        fw_version="1.36.1",
        white_channels=2,
        features=SimpleNamespace(color=True, color_tmp=True, brightness=True),
        kelvin_range=SimpleNamespace(min=2200, max=6500),
    )
    return WizFadeLight(coordinator, "Test bulb")


async def test_ha_brightness_and_transition_mapping():
    light = make_light()
    await light.async_turn_on(brightness=5, transition=20, color_temp_kelvin=3500)
    message = light.engine.bulb.messages[0]["params"]
    assert message["trans"] == 20000
    assert message["dimming"] == 10
    assert message["w"] == 50
    light._update_attrs()
    assert light.brightness == 5
    assert light.color_mode == ColorMode.COLOR_TEMP
    assert light.color_temp_kelvin == 3500
    assert light.extra_state_attributes["wire_brightness_percent"] == 10


async def test_ha_restore_normal_white_and_brightness():
    light = make_light()
    await light.async_turn_on(brightness=5, color_temp_kelvin=3500)
    await light.async_turn_on(brightness=128)
    light._update_attrs()
    assert light.engine.bulb.messages[-2]["params"]["temp"] == 3500
    assert light.extra_state_attributes["scaled_state"] is None


async def test_ha_zero_brightness_is_off():
    light = make_light()
    await light.async_turn_on(brightness=0)
    assert light.engine.bulb.messages[0]["params"] == {"state": False}


async def test_ha_invalid_transition_becomes_service_error():
    light = make_light()
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on(brightness=100, transition=-1)


def test_ha_transition_feature_is_opt_in():
    assert make_light().supported_features & LightEntityFeature.TRANSITION
    assert (
        not make_light(transitions=False).supported_features
        & LightEntityFeature.TRANSITION
    )


def test_options_default_to_disabled():
    assert option_schema({})({}) == {
        "native_transitions": False,
        "experimental_low_dimming": False,
    }
