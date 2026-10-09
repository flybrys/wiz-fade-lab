"""Exercise bulk flows with HA's real flow manager and mocked bulb/network I/O."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant")

from homeassistant.data_entry_flow import FlowManager, FlowResultType
from pywizlight.exceptions import WizLightTimeOutError

from custom_components.wiz_fade import config_flow, discovery
from custom_components.wiz_fade.const import CONF_LOW_DIM, CONF_TRANSITIONS, DOMAIN
from custom_components.wiz_fade.discovery import Candidate

MAC1 = "aabbccddee01"
MAC2 = "aabbccddee02"
MAC3 = "aabbccddee03"


def entry(mac=MAC1, host="192.0.2.1", domain="wiz", **kwargs):
    return SimpleNamespace(
        unique_id=mac,
        data={"host": host},
        title="Study",
        domain=domain,
        entry_id=f"{domain}_{mac}",
        source=kwargs.get("source", "user"),
        disabled_by=kwargs.get("disabled_by"),
    )


class LabFlowManager(FlowManager):
    """Use real HA flow progression, replacing only persistence/platform setup."""

    async def async_create_flow(self, handler_key, *, context=None, data=None):
        flow = config_flow.WizFadeConfigFlow()
        flow.init_step = context["source"]
        return flow

    async def async_finish_flow(self, flow, result):
        if result["type"] == FlowResultType.CREATE_ENTRY:
            saved = entry(flow.unique_id, result["data"]["host"], DOMAIN)
            saved.data = dict(result["data"])
            saved.title = result["title"]
            self.hass.entries.append(saved)
        return result

    def async_flow_removed(self, flow):
        pass


@pytest.fixture
async def setup_flow(monkeypatch):
    hass = MagicMock()
    hass.entries = []
    hass.tasks = []

    def create_task(coro, *args, **kwargs):
        task = asyncio.create_task(coro)
        hass.tasks.append(task)
        return task

    hass.async_create_task = create_task
    hass.config_entries.async_entries.side_effect = lambda domain, *a, **k: [
        item for item in hass.entries if item.domain == domain
    ]
    hass.config_entries.async_entry_for_domain_unique_id.side_effect = (
        lambda domain, mac: next(
            (
                item
                for item in hass.entries
                if item.domain == domain and item.unique_id == mac
            ),
            None,
        )
    )
    manager = LabFlowManager(hass)
    hass.config_entries.flow = manager
    monkeypatch.setattr(discovery.dr, "async_get", lambda hass: None)
    monkeypatch.setattr(
        discovery.dr, "async_entries_for_config_entry", lambda *args: []
    )
    bulb = SimpleNamespace(
        get_bulbtype=AsyncMock(
            return_value=SimpleNamespace(
                features=SimpleNamespace(brightness=True, dual_head=False, color=True)
            )
        ),
        getMac=AsyncMock(return_value=MAC1),
        async_close=AsyncMock(),
    )
    factory = MagicMock(return_value=bulb)
    monkeypatch.setattr(config_flow, "wizlight", factory)
    yield hass, manager, bulb, factory
    for progress in manager.async_progress(include_uninitialized=True):
        manager.async_abort(progress["flow_id"])
    await asyncio.gather(*hass.tasks, return_exceptions=True)


async def choose(manager, option):
    result = await manager.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == FlowResultType.MENU
    assert list(result["menu_options"]) == ["existing", "discover", "manual"]
    return await manager.async_configure(result["flow_id"], {"next_step_id": option})


async def finish_progress(manager, result):
    flow = manager._progress[result["flow_id"]]
    task = flow.async_get_progress_task()
    if task:
        await task
        # Let the real manager's done callback advance SHOW_PROGRESS to DONE.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    return await manager.async_configure(result["flow_id"])


async def test_import_selects_all_with_safe_defaults_and_creates_named_entries(
    setup_flow,
):
    hass, manager, bulb, factory = setup_flow
    first = entry()
    second = entry(MAC2, "192.0.2.2")
    second.title = "Kitchen"
    hass.entries.extend([first, second])
    original = [dict(item.data) for item in hass.entries]
    bulb.getMac.side_effect = [MAC1, MAC2]
    result = await choose(manager, "existing")
    assert result["step_id"] == "select"
    defaults = result["data_schema"]({})
    assert defaults == {
        "devices": [MAC1, MAC2],
        CONF_LOW_DIM: False,
        CONF_TRANSITIONS: False,
    }
    result = await manager.async_configure(
        result["flow_id"], {**defaults, CONF_LOW_DIM: True, CONF_TRANSITIONS: True}
    )
    result = await finish_progress(manager, result)
    assert result["reason"] == "bulk_complete"
    assert result["description_placeholders"] == {
        "added": "2",
        "skipped": "0",
        "failed": "None",
    }
    assert [item.title for item in hass.entries[2:]] == [
        "Study Fade Lab",
        "Kitchen Fade Lab",
    ]
    assert all(
        item.data[CONF_TRANSITIONS] and item.data[CONF_LOW_DIM]
        for item in hass.entries[2:]
    )
    assert [item.data for item in hass.entries[:2]] == original
    assert bulb.async_close.await_count == 2
    assert factory.call_count == 2


async def test_import_can_select_subset_and_reject_empty_selection(setup_flow):
    hass, manager, bulb, _ = setup_flow
    hass.entries.extend([entry(), entry(MAC2, "192.0.2.2")])
    result = await choose(manager, "existing")
    result = await manager.async_configure(result["flow_id"], {"devices": []})
    assert result["errors"] == {"devices": "select_devices"}
    bulb.getMac.return_value = MAC2
    result = await manager.async_configure(result["flow_id"], {"devices": [MAC2]})
    result = await finish_progress(manager, result)
    assert result["description_placeholders"]["added"] == "1"
    assert hass.entries[-1].unique_id == MAC2


async def test_import_excludes_configured_disabled_ignored_and_uses_custom_names(
    setup_flow, monkeypatch
):
    hass, manager, _, _ = setup_flow
    hass.entries.extend(
        [
            entry(),
            entry(MAC2, "192.0.2.2", disabled_by="user"),
            entry(MAC3, "192.0.2.3", source="ignore"),
            entry(domain=DOMAIN),
        ]
    )
    result = await choose(manager, "existing")
    assert result["reason"] == "no_existing_devices"
    hass.entries.pop()
    monkeypatch.setattr(
        discovery.dr,
        "async_entries_for_config_entry",
        lambda *a: [SimpleNamespace(name_by_user="My reading light")],
    )
    assert discovery.existing_devices(hass)[MAC1].name == "My reading light"


async def test_discovery_progress_and_bulk_selection(setup_flow, monkeypatch):
    hass, manager, _, _ = setup_flow
    hass.entries.append(entry(domain=DOMAIN))
    found = {
        MAC1: Candidate("192.0.2.1", MAC1, "Study"),
        MAC2: Candidate("192.0.2.2", MAC2, "Kitchen"),
    }
    scan = AsyncMock(return_value=found)
    monkeypatch.setattr(config_flow, "discover_devices", scan)
    result = await choose(manager, "discover")
    assert result["type"] == FlowResultType.SHOW_PROGRESS
    result = await finish_progress(manager, result)
    assert result["data_schema"]({})["devices"] == [MAC2]
    scan.assert_awaited_once_with(hass)


@pytest.mark.parametrize(
    "error,reason", [(None, "no_devices_found"), (OSError(), "discovery_failed")]
)
async def test_discovery_empty_or_failed(setup_flow, monkeypatch, error, reason):
    _, manager, _, _ = setup_flow
    monkeypatch.setattr(
        config_flow, "discover_devices", AsyncMock(return_value={}, side_effect=error)
    )
    result = await finish_progress(manager, await choose(manager, "discover"))
    assert result["reason"] == reason


async def test_bulk_failure_does_not_stop_later_devices(setup_flow):
    hass, manager, bulb, _ = setup_flow
    hass.entries.extend([entry(), entry(MAC2, "192.0.2.2"), entry(MAC3, "192.0.2.3")])
    features = SimpleNamespace(brightness=True, dual_head=False, color=True)
    bulb.get_bulbtype.side_effect = [
        WizLightTimeOutError(),
        SimpleNamespace(features=features),
        SimpleNamespace(features=features),
    ]
    bulb.getMac.side_effect = ["000000000000", MAC3]
    result = await choose(manager, "existing")
    result = await manager.async_configure(result["flow_id"], result["data_schema"]({}))
    result = await finish_progress(manager, result)
    assert result["description_placeholders"]["added"] == "1"
    assert "cannot_connect" in result["description_placeholders"]["failed"]
    assert "wrong_device" in result["description_placeholders"]["failed"]
    assert hass.entries[-1].unique_id == MAC3
    assert bulb.async_close.await_count == 3


async def test_racing_existing_entry_is_skipped_without_changing_options(setup_flow):
    hass, manager, _, factory = setup_flow
    hass.entries.append(entry())
    result = await choose(manager, "existing")
    existing = entry(domain=DOMAIN)
    existing.data[CONF_LOW_DIM] = False
    hass.entries.append(existing)
    result = await manager.async_configure(
        result["flow_id"], {"devices": [MAC1], CONF_LOW_DIM: True}
    )
    result = await finish_progress(manager, result)
    assert result["description_placeholders"]["skipped"] == "1"
    assert existing.data[CONF_LOW_DIM] is False
    factory.assert_not_called()


async def test_unexpected_failure_leaves_no_orphan_flow(setup_flow):
    hass, manager, bulb, _ = setup_flow
    hass.entries.extend([entry(), entry(MAC2, "192.0.2.2")])
    bulb.get_bulbtype.side_effect = [
        RuntimeError("unexpected"),
        bulb.get_bulbtype.return_value,
    ]
    bulb.getMac.return_value = MAC2
    result = await choose(manager, "existing")
    result = await manager.async_configure(result["flow_id"], result["data_schema"]({}))
    result = await finish_progress(manager, result)
    assert result["description_placeholders"]["added"] == "1"
    assert "unexpected_error" in result["description_placeholders"]["failed"]
    assert not manager.async_progress(include_uninitialized=True)


@pytest.mark.parametrize(
    "attribute,error",
    [
        ("brightness", "unsupported"),
        ("dual_head", "unsupported"),
        ("color", "unsupported_low_dimming"),
    ],
)
async def test_import_rejects_unsupported_device(setup_flow, attribute, error):
    _, manager, bulb, _ = setup_flow
    setattr(
        bulb.get_bulbtype.return_value.features, attribute, attribute == "dual_head"
    )
    result = await manager.async_init(
        DOMAIN,
        context={"source": "import"},
        data={"host": "192.0.2.1", "mac": MAC1, "name": "Test", CONF_LOW_DIM: True},
    )
    assert result["reason"] == error
    bulb.async_close.assert_awaited_once()


async def test_manual_still_works_and_invalid_ip_never_connects(setup_flow):
    _, manager, bulb, factory = setup_flow
    result = await choose(manager, "manual")
    result = await manager.async_configure(result["flow_id"], {"host": "not an IP"})
    assert result["errors"] == {"host": "invalid_ip"}
    factory.assert_not_called()
    result = await manager.async_configure(result["flow_id"], {"host": "192.0.2.1"})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    bulb.async_close.assert_awaited_once()


async def test_cancel_bulk_closes_bulb_and_removes_child_flow(setup_flow):
    hass, manager, bulb, _ = setup_flow
    hass.entries.extend([entry(), entry(MAC2, "192.0.2.2")])
    started = asyncio.Event()

    async def wait_for_bulb():
        started.set()
        await asyncio.Event().wait()

    bulb.get_bulbtype.side_effect = wait_for_bulb
    result = await choose(manager, "existing")
    result = await manager.async_configure(result["flow_id"], result["data_schema"]({}))
    await started.wait()
    manager.async_abort(result["flow_id"])
    await asyncio.gather(*hass.tasks, return_exceptions=True)
    assert not manager.async_progress(include_uninitialized=True)
    bulb.async_close.assert_awaited_once()
    assert len(hass.entries) == 2


async def test_network_scan_merges_interfaces_and_tolerates_one_failure(
    setup_flow, monkeypatch
):
    hass, _, _, _ = setup_flow
    hass.entries.append(entry())
    monkeypatch.setattr(
        discovery.network,
        "async_get_ipv4_broadcast_addresses",
        AsyncMock(return_value=["192.0.2.255", "198.51.100.255", "203.0.113.255"]),
    )
    device = SimpleNamespace(ip_address="192.0.2.1", mac_address="AA:BB:CC:DD:EE:01")
    scan = AsyncMock(side_effect=[[device], OSError(), [device]])
    monkeypatch.setattr(discovery, "find_wizlights", scan)
    result = await discovery.discover_devices(hass)
    assert list(result) == [MAC1]
    assert result[MAC1].name == "Study"
    assert scan.await_count == 3


async def test_cancel_scan_waits_for_library_socket_cleanup(setup_flow, monkeypatch):
    hass, _, _, _ = setup_flow
    monkeypatch.setattr(
        discovery.network,
        "async_get_ipv4_broadcast_addresses",
        AsyncMock(return_value=["192.0.2.255"]),
    )
    started, finish = asyncio.Event(), asyncio.Event()
    closed = []

    async def fake_scan(*args):
        started.set()
        await finish.wait()
        closed.append(True)
        return []

    monkeypatch.setattr(discovery, "find_wizlights", fake_scan)
    task = asyncio.create_task(discovery.discover_devices(hass))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


@pytest.mark.parametrize("addresses", [[], ["192.0.2.255"]])
async def test_scan_all_interfaces_failed(setup_flow, monkeypatch, addresses):
    hass, _, _, _ = setup_flow
    monkeypatch.setattr(
        discovery.network,
        "async_get_ipv4_broadcast_addresses",
        AsyncMock(return_value=addresses),
    )
    monkeypatch.setattr(discovery, "find_wizlights", AsyncMock(side_effect=OSError()))
    with pytest.raises(OSError):
        await discovery.discover_devices(hass)


def test_translations_match_and_cover_menu_and_progress():
    root = Path(__file__).parents[1] / "custom_components/wiz_fade"
    strings = json.loads((root / "strings.json").read_text())
    assert strings == json.loads((root / "translations/en.json").read_text())
    assert set(strings["config"]["step"]["user"]["menu_options"]) == {
        "existing",
        "discover",
        "manual",
    }
    assert set(strings["config"]["progress"]) == {"scanning", "adding"}
