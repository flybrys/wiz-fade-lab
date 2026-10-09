"""Read existing WiZ entries or discover devices on HA's local networks."""

import asyncio
import ipaddress
import logging
import re
from dataclasses import dataclass

from homeassistant.components import network
from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.const import CONF_HOST
from homeassistant.helpers import device_registry as dr
from pywizlight.discovery import find_wizlights

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def mac_key(value: str | None) -> str:
    """Compare MACs consistently without changing existing entry IDs."""
    value = (value or "").replace(":", "").replace("-", "").lower()
    return value if re.fullmatch(r"[0-9a-f]{12}", value) else ""


@dataclass(frozen=True)
class Candidate:
    host: str
    mac: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.host})"


def existing_devices(hass) -> dict[str, Candidate]:
    """Use configured WiZ hosts and user-assigned device names, without edits."""
    registry = dr.async_get(hass)
    candidates = {}
    for entry in hass.config_entries.async_entries("wiz"):
        mac = mac_key(entry.unique_id)
        host = entry.data.get(CONF_HOST)
        if not mac or not host or entry.source == SOURCE_IGNORE or entry.disabled_by:
            continue
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            continue
        devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
        name = next(
            (device.name_by_user for device in devices if device.name_by_user),
            entry.title,
        )
        candidates[mac] = Candidate(host, mac, name)
    return candidates


def unconfigured_devices(
    hass, candidates: dict[str, Candidate]
) -> dict[str, Candidate]:
    """Exclude existing Lab entries, including disabled and ignored entries."""
    entries = hass.config_entries.async_entries(DOMAIN)
    macs = {mac_key(entry.unique_id) for entry in entries}
    hosts = {entry.data.get(CONF_HOST) for entry in entries}
    return {
        key: device
        for key, device in candidates.items()
        if key not in macs and device.host not in hosts
    }


async def discover_devices(hass) -> dict[str, Candidate]:
    """Run one five-second scan across HA's configured IPv4 interfaces."""
    addresses = await network.async_get_ipv4_broadcast_addresses(hass)
    scans = asyncio.gather(
        *(find_wizlights(5, str(address)) for address in addresses),
        return_exceptions=True,
    )
    try:
        results = await asyncio.shield(scans)
    except asyncio.CancelledError:
        # pywizlight 0.6.3 closes discovery sockets only after its timer completes.
        await scans
        raise
    candidates = {}
    successful = False
    known = existing_devices(hass)
    for result in results:
        if isinstance(result, Exception):
            _LOGGER.debug("WiZ discovery failed on one interface: %s", result)
            continue
        if isinstance(result, BaseException):
            raise result
        successful = True
        for device in result:
            if not (mac := mac_key(device.mac_address)):
                continue
            try:
                ipaddress.IPv4Address(device.ip_address)
            except ValueError:
                continue
            name = known[mac].name if mac in known else f"WiZ {mac[-6:]}"
            candidates[mac] = Candidate(device.ip_address, mac, name)
    if not successful:
        raise OSError("No local IPv4 interface could be scanned")
    return candidates
