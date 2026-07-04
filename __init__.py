"""The Limente BLE Mesh Light integration.

Sets up a TelinkMeshGateway that connects to one mesh node via BLE and
discovers all devices on the mesh. Each mesh device gets its own light entity,
added dynamically as devices are discovered via 0xDC notifications.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from bleak.exc import BleakDBusError, BleakError

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble_client import TelinkMeshGateway
from .const import (
    ADV_NAME_PREFIX,
    DEVICE_TIMEOUT,
    DOMAIN,
    MANUFACTURER_ID,
    UPDATE_SECONDS,
)
from .models import LimenteLightConfigEntry, LimenteLightData

BLEAK_EXCEPTIONS = (BleakError, BleakDBusError, asyncio.TimeoutError)

PLATFORMS: list[Platform] = [Platform.LIGHT]

_LOGGER = logging.getLogger(__name__)


def _find_gateway_ble_device(hass: HomeAssistant, preferred_address: str):
    """Find a connectable BLE node to use as the mesh gateway.

    Any node on the mesh is a valid gateway (they all share the same
    credentials and relay commands to the whole mesh). We prefer the node the
    entry was configured with, but if it is off / rebooting / out of range we
    fall back to any other connectable Limente/Telink node that is currently
    advertising. This keeps the integration working when the originally-chosen
    node is temporarily unavailable.
    """
    ble_device = bluetooth.async_ble_device_from_address(
        hass, preferred_address.upper(), True
    )
    if ble_device:
        return ble_device

    for service_info in bluetooth.async_discovered_service_info(
        hass, connectable=True
    ):
        is_mesh_node = MANUFACTURER_ID in service_info.advertisement.manufacturer_data or (
            service_info.name and service_info.name.startswith(ADV_NAME_PREFIX)
        )
        if not is_mesh_node:
            continue
        candidate = bluetooth.async_ble_device_from_address(
            hass, service_info.address, True
        )
        if candidate:
            _LOGGER.warning(
                "Configured gateway node %s is not reachable; using mesh "
                "node %s as gateway instead",
                preferred_address,
                service_info.address,
            )
            return candidate

    return None


async def async_setup_entry(
    hass: HomeAssistant, entry: LimenteLightConfigEntry
) -> bool:
    """Set up Limente BLE Mesh Light from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    ble_device = _find_gateway_ble_device(hass, address)
    if not ble_device:
        raise ConfigEntryNotReady(
            f"No reachable Limente mesh node found (configured {address})"
        )

    # Use whichever node we actually connected through for the BLE callback
    # matcher below, so passive advertisement updates refresh the right device.
    address = ble_device.address

    gateway = TelinkMeshGateway(ble_device)

    @callback
    def _async_update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Update from a BLE callback."""
        gateway.set_ble_device_and_advertisement_data(
            service_info.device, service_info.advertisement
        )

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            BluetoothCallbackMatcher({ADDRESS: address}),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )

    async def _async_update() -> None:
        """Update the device state."""
        try:
            await gateway.update()
        except BLEAK_EXCEPTIONS as ex:
            raise UpdateFailed(str(ex)) from ex
        except ValueError as ex:
            raise UpdateFailed(str(ex)) from ex

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=entry,
        name=gateway.name,
        update_method=_async_update,
        update_interval=timedelta(seconds=UPDATE_SECONDS),
    )

    # Do first refresh — triggers mesh login + status query
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        raise

    entry.runtime_data = LimenteLightData(entry.title, gateway, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _async_stop(event: Event) -> None:
        """Close the connection."""
        await gateway.stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: LimenteLightConfigEntry
) -> None:
    """Handle options update."""
    if entry.title != entry.runtime_data.title:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: LimenteLightConfigEntry
) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.gateway.stop()

    return unload_ok
