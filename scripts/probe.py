"""Exercise the exact integration engine against one bulb; read-only by default."""

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from pywizlight import wizlight

path = Path(__file__).parents[1] / "custom_components/wiz_fade/engine.py"
spec = importlib.util.spec_from_file_location("wiz_fade_engine", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


async def run(host: str, apply: bool):
    bulb = wizlight(host)
    engine = None
    restore = None
    try:
        model = (await bulb.getModelConfig() or {}).get("result", {})
        table = module.parse_cct_table(
            (await bulb.send({"method": "getCctTable", "params": {}}))["result"]
        )
        user = (await bulb.getUserConfig())["result"]
        engine = module.FadeEngine(
            bulb,
            model,
            table,
            low_dim=True,
            transitions=True,
            fade_out_ms=user.get("fadeOut", 1000),
        )
        await engine.refresh()
        before = dict(engine.state)
        print(
            json.dumps(
                {
                    "minimum": engine.minimum,
                    "cct_rows": len(table),
                    "proposed_2_percent": engine.plan(percent=2).params,
                }
            )
        )
        if not apply:
            return
        if (
            not before.get("state")
            or not before.get("temp")
            or before.get("sceneId", 0)
        ):
            raise ValueError(
                "Live probe requires an on bulb in plain white-temperature mode"
            )
        restore = {key: before[key] for key in ("state", "dimming", "temp")}
        for percent in (20, 2, 10):
            await engine.turn_on(percent=percent, transition=1)
            await asyncio.sleep(1.3)
            await engine.refresh()
            print(
                json.dumps(
                    {
                        "requested": percent,
                        "logical": engine.percent,
                        "wire": {
                            k: engine.state[k]
                            for k in (
                                "state",
                                "dimming",
                                "temp",
                                "r",
                                "g",
                                "b",
                                "w",
                                "c",
                            )
                            if k in engine.state
                        },
                    }
                )
            )
            assert abs(engine.percent - percent) < 0.01, (
                "Logical brightness did not survive device refresh"
            )
        # Verify that a later command prevents a scheduled off.
        await engine.turn_off(3)
        await asyncio.sleep(0.2)
        await engine.turn_on(percent=20)
        await asyncio.sleep(3.2)
        await engine.refresh()
        assert engine.state.get("state") is True, "Cancelled fade switched the bulb off"
        print("Cancellation verified against live bulb")
    finally:
        if engine:
            await engine.close()
        try:
            if restore:
                await bulb.send(
                    {"method": "setPilot", "params": dict(restore, trans=1000)}
                )
                await asyncio.sleep(1.2)
                actual = (await bulb.send({"method": "getPilot", "params": {}}))[
                    "result"
                ]
                assert all(actual.get(k) == v for k, v in restore.items()), (
                    "Restore did not match original state"
                )
                print("Original white state restored and verified")
        finally:
            await bulb.async_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Run a brief live test and restore the bulb",
    )
    args = parser.parse_args()
    asyncio.run(run(args.host, args.apply))
