"""Limente BLE Mesh Light integration light platform.

Creates one light entity per mesh device discovered on the Telink BLE mesh.
All entities share a single TelinkMeshGateway (one BLE connection), but each
targets its own mesh address for commands and state.

Devices are discovered dynamically via 0xDC (online status report) notifications
and added to Home Assistant as they appear.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .ble_client import MeshDeviceState, TelinkMeshGateway
from .const import DOMAIN
from .models import SwitchBotLightConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwitchBotLightConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up light entities for all mesh devices."""
    data = entry.runtime_data
    gateway = data.gateway

    # Track which mesh addresses already have entities to avoid duplicates
    known_addresses: set[int] = set()

    @callback
    def _async_add_mesh_device(mesh_address: int) -> None:
        """Add a new light entity when a mesh device is discovered."""
        if mesh_address in known_addresses:
            return
        known_addresses.add(mesh_address)
        _LOGGER.info(
            "Adding light entity for mesh device 0x%02x", mesh_address
        )
        async_add_entities(
            [TelinkMeshLightEntity(gateway, mesh_address, entry.entry_id)]
        )

    # Register callback for future device discoveries (before checking
    # existing devices so we don't miss any that arrive during setup)
    entry.async_on_unload(
        gateway.register_new_device_callback(_async_add_mesh_device)
    )

    # Create entities for devices the gateway already knows about
    # (e.g. from the first coordinator refresh / 0xDC notifications)
    existing_entities: list[TelinkMeshLightEntity] = []
    for mesh_address in list(gateway.devices):
        if mesh_address not in known_addresses:
            known_addresses.add(mesh_address)
            existing_entities.append(
                TelinkMeshLightEntity(gateway, mesh_address, entry.entry_id)
            )

    if existing_entities:
        _LOGGER.info(
            "Adding %d existing mesh device entities: %s",
            len(existing_entities),
            [f"0x{e.mesh_address:02x}" for e in existing_entities],
        )
        async_add_entities(existing_entities)


class TelinkMeshLightEntity(LightEntity):
    """A single white-only light on the Telink BLE mesh."""

    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_has_entity_name = True

    def __init__(
        self,
        gateway: TelinkMeshGateway,
        mesh_address: int,
        config_entry_id: str,
    ) -> None:
        """Initialize a mesh light entity."""
        self._gateway = gateway
        self._mesh_address = mesh_address

        # Unique ID: gateway BLE MAC + mesh address (hex)
        mac_clean = gateway.address.replace(":", "").lower()
        self._attr_unique_id = f"{mac_clean}_mesh_{mesh_address:02x}"

        self._attr_name = None  # Use device name only (has_entity_name=True)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=f"Limente Mesh 0x{mesh_address:02X}",
            model="BLE Mesh LED Controller",
            manufacturer="Limente (Sunricher)",
            sw_version=gateway.firmware or None,
        )

    @property
    def mesh_address(self) -> int:
        """Return the mesh address for logging."""
        return self._mesh_address

    @property
    def available(self) -> bool:
        """Return True if the device is online on the mesh."""
        device = self._gateway.devices.get(self._mesh_address)
        if device is None:
            return False
        return device.online

    @property
    def is_on(self) -> bool | None:
        """Return True if the light is on."""
        device = self._gateway.devices.get(self._mesh_address)
        if device is None:
            return None
        return device.is_on

    @property
    def brightness(self) -> int | None:
        """Return the brightness (0-255 HA scale)."""
        device = self._gateway.devices.get(self._mesh_address)
        if device is None:
            return None
        return device.brightness_ha

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on, optionally setting brightness."""
        if ATTR_BRIGHTNESS in kwargs:
            brightness_ha = kwargs[ATTR_BRIGHTNESS]
            brightness_pct = max(1, round(brightness_ha / 255 * 100))
            await self._gateway.set_brightness(self._mesh_address, brightness_pct)
        else:
            await self._gateway.turn_on(self._mesh_address)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self._gateway.turn_off(self._mesh_address)

    async def async_added_to_hass(self) -> None:
        """Register state-change callback when entity is added to HA."""
        await super().async_added_to_hass()
        device = self._gateway.get_device(self._mesh_address)
        device.callbacks.append(self._handle_state_update)

        # Also write initial state
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Unregister callback when entity is removed."""
        device = self._gateway.devices.get(self._mesh_address)
        if device is not None:
            try:
                device.callbacks.remove(self._handle_state_update)
            except ValueError:
                pass
        await super().async_will_remove_from_hass()

    @callback
    def _handle_state_update(self) -> None:
        """Handle a state update from the mesh gateway."""
        self.async_write_ha_state()
