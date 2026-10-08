"""WiZ command planning and cancellable fades, independent of Home Assistant.

Use the existing pywizlight transport. No firmware changes or cloud calls.
"""

import asyncio
import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

CHANNELS = ("r", "g", "b", "w", "c")


def transition_ms(seconds: float) -> int:
    """Validate before converting HA seconds into firmware milliseconds."""
    if not math.isfinite(seconds) or not 0 <= seconds <= 3600:
        raise ValueError("Transition must be between 0 and 3600 seconds")
    return round(seconds * 1000)


def native_minimum(model: Mapping[str, Any]) -> int:
    """Honor the bulb's advertised floor; use legacy 10% when unavailable."""
    value = model.get("minDimLevel", 10)
    return value if type(value) is int and 1 <= value <= 100 else 10


def parse_cct_table(result: Mapping[str, Any]) -> list[tuple[int, ...]]:
    """Reject malformed calibration data rather than invent white ratios."""
    rows = result.get("cctPoints")
    if not isinstance(rows, list):
        return []
    valid = [
        tuple(row)
        for row in rows
        if isinstance(row, list)
        and len(row) == 6
        and all(type(x) is int for x in row)
        and 1000 <= row[0] <= 12000
        and all(0 <= x <= 255 for x in row[1:])
        and any(row[1:])
    ]
    if len(valid) != len(rows) or len({row[0] for row in valid}) != len(valid):
        return []
    return sorted(valid)


def cct_channels(kelvin: int, table: list[tuple[int, ...]]) -> tuple[int, ...]:
    """Interpolate only within a device-supplied table."""
    if not table or not table[0][0] <= kelvin <= table[-1][0]:
        raise ValueError("No device CCT calibration available for this temperature")
    for row in table:
        if row[0] == kelvin:
            return row[1:]
    for left, right in zip(table, table[1:]):
        if left[0] < kelvin < right[0]:
            fraction = (kelvin - left[0]) / (right[0] - left[0])
            return tuple(
                round(a + (b - a) * fraction) for a, b in zip(left[1:], right[1:])
            )
    raise ValueError("Invalid CCT table")


def wire_signature(state: Mapping[str, Any]) -> tuple:
    """Use light settings, not RSSI/source, to recognize our scaled state."""
    return (
        state.get("state"),
        state.get("dimming"),
        state.get("sceneId", 0),
        state.get("temp", 0),
        *(state.get(key, 0) for key in CHANNELS),
    )


@dataclass
class ScaledState:
    """Logical brightness and unscaled channels for our current low state."""

    percent: float
    channels: tuple[int, ...]
    kelvin: int | None
    expected: dict[str, Any]

    def matches(self, state: Mapping[str, Any]) -> bool:
        return wire_signature(self.expected) == wire_signature(state)


@dataclass
class Plan:
    params: dict[str, Any]
    scaled: ScaledState | None = None


class FadeEngine:
    """Own temporary logical state and a single cancellable delayed off."""

    def __init__(
        self,
        bulb: Any,
        model: dict,
        table: list[tuple[int, ...]],
        *,
        low_dim: bool = False,
        transitions: bool = False,
        fade_out_ms: int = 1000,
    ) -> None:
        self.bulb = bulb
        self.minimum = native_minimum(model)
        self.table = table
        self.low_dim = low_dim
        self.transitions = transitions
        self.fade_out_ms = fade_out_ms
        self.scaled: ScaledState | None = None
        self.state: dict[str, Any] = {}
        self._pending: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._generation = 0
        self._fade_signature: tuple | None = None
        self.on_change: Callable[[], None] = lambda: None
        self.on_error: Callable[[Exception], None] = lambda error: None

    def observe(self, state: dict) -> None:
        """Clear logical state and pending off if another controller intervenes."""
        self.state = dict(state)
        if self.scaled and not self.scaled.matches(state):
            self.scaled = None
        if self._fade_signature and wire_signature(state) != self._fade_signature:
            self.cancel()

    @property
    def percent(self) -> float:
        return self.scaled.percent if self.scaled else self.state.get("dimming", 100)

    def restore_scaled(self, saved: dict) -> None:
        """Restore metadata only if the actual bulb still matches it exactly."""
        try:
            scaled = ScaledState(**saved)
            if (
                0 < scaled.percent < self.minimum
                and len(scaled.channels) == 5
                and all(type(x) is int and 0 <= x <= 255 for x in scaled.channels)
                and scaled.matches(self.state)
            ):
                self.scaled = scaled
        except (TypeError, ValueError, KeyError):
            return

    def metadata(self) -> dict | None:
        return asdict(self.scaled) if self.scaled else None

    def plan(
        self,
        *,
        percent: float | None = None,
        kelvin: int | None = None,
        channels: tuple[int, ...] | None = None,
        scene: int | None = None,
        transition: float | None = None,
    ) -> Plan:
        if percent is not None and (
            not math.isfinite(percent) or not 0 < percent <= 100
        ):
            raise ValueError(
                "Brightness must be greater than zero and at most 100 percent"
            )
        if channels is not None and (
            len(channels) != 5
            or any(type(x) is not int or not 0 <= x <= 255 for x in channels)
        ):
            raise ValueError("RGBWW channels must contain five integers from 0 to 255")
        params: dict[str, Any] = {"state": True}
        if transition is not None:
            if not self.transitions:
                raise ValueError("Enable native transitions for this test device first")
            params["trans"] = transition_ms(transition)

        desired = self.percent if percent is None else percent
        current_scene = self.state.get("sceneId", 0)
        effect_active = scene is not None or (
            current_scene != 0 and kelvin is None and channels is None
        )
        if scene is not None:
            params["sceneId"] = scene
        if kelvin is not None:
            params["temp"] = kelvin
        elif channels is not None:
            params.update(zip(CHANNELS, channels))

        if desired < self.minimum and self.low_dim:
            if effect_active:
                raise ValueError(
                    "Experimental low dimming cannot preserve dynamic effects; select a white temperature or RGBWW color first"
                )
            if kelvin is None and channels is None:
                if self.scaled:
                    kelvin, channels = self.scaled.kelvin, self.scaled.channels
                elif self.state.get("temp"):
                    kelvin = self.state["temp"]
                elif any(self.state.get(k, 0) for k in CHANNELS):
                    channels = tuple(self.state.get(k, 0) for k in CHANNELS)
            if kelvin is not None:
                channels = cct_channels(kelvin, self.table)
            if channels is None or not any(channels):
                raise ValueError(
                    "Cannot scale this mode: no white calibration or RGBWW channels"
                )
            ratio = desired / self.minimum
            scaled_channels = tuple(round(x * ratio) for x in channels)
            if not any(scaled_channels):
                raise ValueError("Requested channel levels round to zero")
            params.pop("temp", None)
            params.update(zip(CHANNELS, scaled_channels))
            params["dimming"] = self.minimum
            expected = {
                "state": True,
                "sceneId": 0,
                "dimming": self.minimum,
                **dict(zip(CHANNELS, scaled_channels)),
            }
            return Plan(params, ScaledState(desired, channels, kelvin, expected))

        # Leaving our scaled mode must restore the original mix, not retain dim channels.
        if self.scaled and scene is None and kelvin is None and channels is None:
            if self.scaled.kelvin is not None:
                params["temp"] = self.scaled.kelvin
            else:
                params.update(zip(CHANNELS, self.scaled.channels))
        if percent is not None or self.scaled:
            params["dimming"] = max(self.minimum, round(desired))
        return Plan(params)

    def cancel(self) -> None:
        self._generation += 1
        self._fade_signature = None
        if self._pending:
            self._pending.cancel()
            self._pending = None

    async def close(self) -> None:
        pending = self._pending
        self.cancel()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)

    async def _send(self, params: dict) -> None:
        response = await self.bulb.send({"method": "setPilot", "params": params})
        if not response or not response.get("result", {}).get("success"):
            raise ValueError("Bulb did not acknowledge the command")

    async def refresh(self) -> None:
        response = await self.bulb.send({"method": "getPilot", "params": {}})
        if "result" not in response:
            raise ValueError("Bulb returned no state")
        self.observe(response["result"])

    async def turn_on(self, **kwargs: Any) -> None:
        self.cancel()
        async with self._lock:
            plan = self.plan(**kwargs)
            await self._send(plan.params)
            self.scaled = plan.scaled
            await self.refresh()

    async def turn_off(self, transition: float | None = None) -> None:
        self.cancel()
        async with self._lock:
            if transition is None or transition == 0 or not self.state.get("state"):
                await self._send({"state": False})
                self.scaled = None
                await self.refresh()
                return
            if not self.transitions:
                raise ValueError("Native transitions are disabled")
            total_ms = transition_ms(transition)
            if total_ms < self.fade_out_ms:
                raise ValueError(
                    "Requested transition is shorter than the bulb's configured fade-out"
                )
            # Firmware ignores trans on state:false, so dim first, then send off.
            target = min(self.percent, 1 if self.low_dim else self.minimum)
            plan = self.plan(
                percent=target, transition=(total_ms - self.fade_out_ms) / 1000
            )
            await self._send(plan.params)
            self.scaled = plan.scaled
            await self.refresh()
            self._fade_signature = wire_signature(self.state)
            generation = self._generation
            self._pending = asyncio.create_task(
                self._finish_off((total_ms - self.fade_out_ms) / 1000, generation),
                name="wiz-fade-delayed-off",
            )

    async def _finish_off(self, delay: float, generation: int) -> None:
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                if generation != self._generation:
                    return
                # Check again even when the fade is shorter than the poll interval.
                response = await self.bulb.send({"method": "getPilot", "params": {}})
                current = response.get("result")
                if current is None or wire_signature(current) != self._fade_signature:
                    if current is not None:
                        self.state = current
                        self.scaled = None
                    return
                self._fade_signature = None
                await self._send({"state": False})
                self.scaled = None
                await self.refresh()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.on_error(error)
        finally:
            if generation == self._generation:
                self._pending = None
                self._fade_signature = None
                self.on_change()
