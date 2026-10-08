import asyncio
from copy import deepcopy

import pytest
from wiz_fade_engine import (
    FadeEngine,
    cct_channels,
    native_minimum,
    parse_cct_table,
    transition_ms,
)

TABLE = [(2700, 0, 0, 0, 255, 0), (3500, 0, 0, 0, 255, 150), (6500, 0, 0, 0, 0, 255)]


class FakeBulb:
    def __init__(self, minimum=10):
        self.minimum = minimum
        self.state = {"state": True, "sceneId": 0, "temp": 3500, "dimming": 20}
        self.messages = []
        self.fail = False

    async def send(self, message):
        self.messages.append(deepcopy(message))
        if self.fail:
            raise OSError("offline")
        if message["method"] == "getPilot":
            return {"result": dict(self.state)}
        params = dict(message["params"])
        params.pop("trans", None)
        if "dimming" in params:
            params["dimming"] = max(self.minimum, params["dimming"])
        if "temp" in params:
            for key in ("r", "g", "b", "w", "c"):
                self.state.pop(key, None)
            self.state["sceneId"] = 0
        elif "w" in params:
            self.state.pop("temp", None)
            self.state["sceneId"] = 0
        self.state.update(params)
        return {"result": {"success": True}}


def make_engine(minimum=10, low=True, transitions=True):
    bulb = FakeBulb(minimum)
    engine = FadeEngine(
        bulb,
        {"minDimLevel": minimum},
        TABLE,
        low_dim=low,
        transitions=transitions,
        fade_out_ms=0,
    )
    engine.observe(bulb.state)
    return engine


@pytest.mark.parametrize(
    "seconds,expected", [(0, 0), (0.5, 500), (20, 20000), (50, 50000)]
)
def test_transition_units(seconds, expected):
    assert transition_ms(seconds) == expected


@pytest.mark.parametrize("seconds", [-1, float("nan"), float("inf"), 3601])
def test_invalid_transition(seconds):
    with pytest.raises(ValueError):
        transition_ms(seconds)


@pytest.mark.parametrize(
    "model,expected",
    [
        ({}, 10),
        ({"minDimLevel": 1}, 1),
        ({"minDimLevel": 10}, 10),
        ({"minDimLevel": 0}, 10),
        ({"minDimLevel": "1"}, 10),
        ({"minDimLevel": True}, 10),
    ],
)
def test_native_minimum(model, expected):
    assert native_minimum(model) == expected


def test_table_validation_and_interpolation():
    assert parse_cct_table({"cctPoints": [list(row) for row in TABLE]}) == TABLE
    assert cct_channels(3100, TABLE) == (0, 0, 0, 255, 75)
    assert parse_cct_table({"cctPoints": 16}) == []
    assert parse_cct_table({"cctPoints": [[3500, 999, 0, 0, 0, 0]]}) == []
    assert parse_cct_table({"cctPoints": [list(TABLE[0]), list(TABLE[0])]}) == []
    with pytest.raises(ValueError):
        cct_channels(2000, TABLE)


async def test_native_one_percent_sent_without_library_clamp():
    engine = make_engine(minimum=1)
    await engine.turn_on(percent=1, transition=20)
    assert engine.bulb.messages[0]["params"] == {
        "state": True,
        "dimming": 1,
        "trans": 20000,
    }
    assert engine.percent == 1
    assert engine.scaled is None


async def test_native_one_percent_preserves_effect():
    engine = make_engine(minimum=1)
    engine.observe({**engine.state, "sceneId": 6})
    await engine.turn_on(percent=1, scene=6)
    assert engine.bulb.messages[0]["params"]["sceneId"] == 6
    assert engine.bulb.messages[0]["params"]["dimming"] == 1


async def test_two_percent_matches_hardware_experiment():
    engine = make_engine()
    await engine.turn_on(percent=2)
    assert engine.bulb.messages[0]["params"] == {
        "state": True,
        "dimming": 10,
        "r": 0,
        "g": 0,
        "b": 0,
        "w": 51,
        "c": 30,
    }
    assert engine.percent == 2
    assert engine.state["dimming"] == 10
    assert engine.scaled.kelvin == 3500


async def test_repeated_low_requests_do_not_multiply_scaling():
    engine = make_engine()
    await engine.turn_on(percent=2)
    await engine.turn_on(percent=5)
    assert engine.state["w"] == 128
    assert engine.state["c"] == 75
    assert engine.percent == 5


async def test_return_to_normal_restores_white_temperature():
    engine = make_engine()
    await engine.turn_on(percent=2)
    await engine.turn_on(percent=10)
    assert engine.bulb.messages[-2]["params"] == {
        "state": True,
        "temp": 3500,
        "dimming": 10,
    }
    assert engine.scaled is None


async def test_color_change_while_scaled_keeps_low_brightness():
    engine = make_engine()
    await engine.turn_on(percent=2)
    await engine.turn_on(kelvin=2700)
    assert engine.state["w"] == 51
    assert engine.state["c"] == 0
    assert engine.percent == 2


async def test_rgb_scaling_restores_original_channels():
    engine = make_engine()
    await engine.turn_on(percent=2, channels=(120, 60, 30, 0, 0))
    assert engine.state["r"] == 24
    await engine.turn_on(percent=40)
    assert engine.state["r"] == 120
    assert engine.state["g"] == 60


async def test_external_change_invalidates_low_metadata():
    engine = make_engine()
    await engine.turn_on(percent=2)
    engine.observe({"state": True, "sceneId": 0, "temp": 3500, "dimming": 10})
    assert engine.scaled is None
    assert engine.percent == 10


async def test_restart_requires_matching_physical_state():
    engine = make_engine()
    await engine.turn_on(percent=2)
    saved = engine.metadata()
    other = make_engine()
    other.restore_scaled(saved)
    assert other.scaled is None
    other.observe(engine.state)
    other.restore_scaled(saved)
    assert other.percent == 2


def test_missing_cct_data_does_not_guess_channels():
    engine = make_engine()
    engine.table = []
    with pytest.raises(ValueError, match="calibration"):
        engine.plan(percent=2)


def test_low_dimming_rejects_dynamic_effects():
    engine = make_engine()
    with pytest.raises(ValueError, match="effects"):
        engine.plan(percent=2, scene=6)
    engine.observe({**engine.state, "sceneId": 6})
    with pytest.raises(ValueError, match="effects"):
        engine.plan(percent=2)
    assert engine.plan(percent=2, kelvin=3500).scaled is not None


async def test_disabled_scaling_reports_real_clamped_state():
    engine = make_engine(low=False)
    await engine.turn_on(percent=2)
    assert engine.percent == 10
    assert engine.bulb.messages[0]["params"]["dimming"] == 10


def test_disabled_transition_does_not_advertise_silent_support():
    with pytest.raises(ValueError, match="Enable native"):
        make_engine(transitions=False).plan(percent=20, transition=5)


async def test_off_is_delayed_and_finishes():
    engine = make_engine(low=False)
    await engine.turn_off(0.02)
    assert engine.state["state"] is True
    assert engine.bulb.messages[0]["params"]["trans"] == 20
    await asyncio.sleep(0.05)
    assert engine.state["state"] is False
    assert engine._pending is None


async def test_turn_on_cancels_delayed_off():
    engine = make_engine()
    await engine.turn_off(0.03)
    await engine.turn_on(percent=50)
    await asyncio.sleep(0.06)
    assert engine.state["state"] is True
    assert engine.state["dimming"] == 50
    assert not any(
        m.get("params", {}).get("state") is False for m in engine.bulb.messages
    )


async def test_external_control_cancels_delayed_off_without_poll():
    engine = make_engine()
    await engine.turn_off(0.02)
    engine.bulb.state.update(temp=4000, dimming=80)
    await asyncio.sleep(0.05)
    assert engine.state["state"] is True
    assert engine.scaled is None


async def test_unload_cancels_delayed_off():
    engine = make_engine()
    await engine.turn_off(0.03)
    await engine.close()
    await asyncio.sleep(0.05)
    assert engine.bulb.state["state"] is True


async def test_off_failure_is_reported_and_task_cleared():
    engine = make_engine()
    errors = []
    changes = []
    engine.on_error = errors.append
    engine.on_change = lambda: changes.append(True)
    await engine.turn_off(0.02)
    engine.bulb.fail = True
    await asyncio.sleep(0.05)
    assert len(errors) == 1
    assert not changes
    assert engine._pending is None


async def test_global_fadeout_subtracted_from_requested_total():
    engine = make_engine()
    engine.fade_out_ms = 1000
    await engine.turn_off(20)
    assert engine.bulb.messages[0]["params"]["trans"] == 19000
    await engine.close()


async def test_too_short_off_transition_rejected_before_command():
    engine = make_engine()
    engine.fade_out_ms = 1000
    with pytest.raises(ValueError, match="shorter"):
        await engine.turn_off(0.5)
    assert not engine.bulb.messages


async def test_already_off_does_not_flash_on():
    engine = make_engine()
    engine.observe({**engine.state, "state": False})
    await engine.turn_off(10)
    assert engine.bulb.messages[0]["params"] == {"state": False}


async def test_turn_on_after_fade_restores_original_brightness():
    engine = make_engine()
    await engine.turn_off(0.02)
    await asyncio.sleep(0.05)
    await engine.turn_on()
    assert engine.percent == 20
    assert engine.state["temp"] == 3500


async def test_turn_on_after_off_restores_scaled_state():
    engine = make_engine()
    await engine.turn_on(percent=2)
    await engine.turn_off()
    await engine.turn_on()
    assert engine.percent == 2
    assert engine.state["w"] == 51


async def test_explicit_on_brightness_overrides_resume():
    engine = make_engine()
    await engine.turn_off(0.02)
    await asyncio.sleep(0.05)
    await engine.turn_on(percent=50, kelvin=2700)
    assert engine.percent == 50
    assert engine.state["temp"] == 2700


@pytest.mark.parametrize("percent", [0, -1, 101, float("nan")])
def test_bad_brightness_rejected(percent):
    with pytest.raises(ValueError):
        make_engine().plan(percent=percent)
