# WiZ Fade Lab

Experimental Home Assistant integration for native WiZ UDP brightness transitions
and optional dimming below the device's advertised minimum. Target: Home Assistant
**2026.9.4**. This adds a separate light entity for an explicitly selected bulb;
it does not replace the built-in WiZ integration.

## What works

- Native timed brightness changes: Home Assistant `transition: 20` sends
  `trans: 20000` to the bulb. The bulb performs the transition itself.
- Native low brightness: honor `getModelConfig.minDimLevel`. A bulb advertising
  1% receives `dimming: 1`, rather than the fixed 10% clamp in pywizlight 0.6.3.
- Optional approximate low brightness for older RGB bulbs: keep `dimming` at the
  native minimum and proportionally reduce the RGB/white channel levels. White
  mixing comes from the bulb's own CCT table, not a hard-coded model assumption.
- Timed off: dim first, then issue off using the device's saved global fade-out.
  A newer command or integration unload cancels the delayed off. Detectable
  external state changes also cancel it.
- Scaled-state tracking: the experimental entity reports the requested approximate
  brightness, with the actual wire brightness exposed separately. Normal dimming
  restores the original white temperature or channel mix. Metadata is restored
  after HA restart only when it matches the actual bulb state.

Both experimental options are **off by default** and enabled separately per bulb.
The built-in WiZ entity will still report wire brightness (for example 10% while
the lab entity shows approximately 2%). Use only the lab entity for a test
automation; do not target a group containing both entities for the same bulb.

## Install for testing

1. In HACS, add `https://github.com/flybrys/wiz-fade-lab` as a custom repository
   of type **Integration**, then download WiZ Fade Lab and restart Home Assistant.
   Alternatively, place the repository's `custom_components/wiz_fade` directory
   under your HA configuration's `custom_components` directory and restart.
2. Open **Settings > Devices & services > Add integration > WiZ Fade Lab**.
3. Enter one bulb's IP address. Enable native transitions for the test. Enable
   experimental low dimming only when testing channel scaling on an RGB bulb.
4. Use the newly created **Fade Lab** light entity. Its actual entity ID is shown
   in Home Assistant; replace the example ID below with that ID.

Options can be changed using the integration's configuration menu.
The integration is light-only and supports single-head bulbs. Keep using the
built-in WiZ integration for effects, sockets, fans, occupancy and other features.

```yaml
action: light.turn_on
target:
  entity_id: light.study_fade_lab
data:
  brightness_pct: 2
  color_temp_kelvin: 3500
  transition: 20
```

```yaml
action: light.turn_off
target:
  entity_id: light.study_fade_lab
data:
  transition: 20
```

To return to normal white:

```yaml
action: light.turn_on
target:
  entity_id: light.study_fade_lab
data:
  brightness_pct: 10
  color_temp_kelvin: 3500
```

## Evidence and limits

Test bulb: `ESP24_SHRGB_01`, firmware `1.36.1`, native minimum 10%.
Native dimming from 60% to 10% over 10 seconds was visually confirmed. Scaling
3500 K white channels from `(w=255,c=150)` to `(w=51,c=30)` while retaining
`dimming:10` was visibly dimmer. "2%" is an approximate channel-scaling target,
not a photometric measurement; perceived brightness is nonlinear.

The firmware parser at `0x42014ef0` reads `trans` and passes it to the light
driver. The off-only path at `0x420c458e` instead reads global `fadeOut`.
`state:false,trans:10000` was accepted but visibly switched off quickly.
The standalone `pulse` command did not produce a visible fade in our tests.

Limitations:

- No claim of compatibility with every WiZ firmware. Channel scaling and native
  transitions require broader device testing before inclusion in Home Assistant Core.
- Low channel scaling does not preserve dynamic effects. Select an explicit white
  temperature or RGBWW color first. Bulbs with native 1% support need no scaling.
- Transitions that cross the native floor change both channels and dimming. The
  resulting light curve is approximate and may not be linear. This has not been
  visually validated for every combination of color and duration.
- Timed off is two commands, not a native one-command fade to zero. Timing includes
  small network delays. A duration shorter than configured global fade-out is
  rejected. HA must remain running for the final off command.
- Reissuing the same physical state from another controller cannot be detected as
  a new command. A fade may also be affected by other automations controlling the bulb.
- This is a separate polling integration. It does not alter your existing WiZ
  integration's entities or automations. The live HA instance must be tested after
  installation; passing isolated tests is not an installation test.

To remove: delete the WiZ Fade Lab integration entry, remove its HACS download
or `custom_components/wiz_fade` folder, then restart. Existing WiZ entities remain.

## Relation to issue 179420 and upstream work

[Home Assistant issue #179420](https://github.com/home-assistant/core/issues/179420)
describes bulbs advertising `minDimLevel:1` being clamped by the library to 10%.
That is distinct from our older bulb's genuine 10% command floor. In the reviewed
pywizlight 0.6.6 source, the builder already permits values down to 1%; HA 2026.9.4
still pins 0.6.3. This lab uses the existing 0.6.3 transport with explicit payloads
so it does not force a conflicting library version into HA.

Suggested upstream split:

1. Library support for a validated transition argument mapped to `trans`, with
   documented firmware scope, and capability-aware native minimum brightness.
2. Home Assistant's light adapter maps `ATTR_TRANSITION` and advertises transition
   support only for validated devices. Off transitions need separate semantics;
   simply forwarding `trans` on off would be incorrect.
3. Keep channel scaling experimental until calibration, effect handling, state
   restoration, and multiple hardware families have been validated.

No upstream pull request has been submitted by this project.

## Development

```sh
python -m venv .venv
# Activate the environment, then:
python -m pip install -r requirements-dev.txt
python -m pytest -q
ruff check custom_components tests scripts
```

Engine tests run without Home Assistant. The GitHub Actions job also installs
Home Assistant 2026.9.4 on Linux/Python 3.14 and exercises the real entity adapter.
Those tests are skipped on machines where Home Assistant is not installed.

Read-only command planning against a local bulb:

```sh
python scripts/probe.py BULB_IP
```

Add `--apply` for a brief hardware test (20%, approximate 2%, 10%, cancellation),
with restoration of the original white state. It requires a bulb already on in
plain white-temperature mode. No firmware binaries, private addresses, device
identifiers, credentials or household configuration are included in this repo.
