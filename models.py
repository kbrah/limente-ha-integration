"""Models for the Limente BLE Mesh Light integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .ble_client import TelinkMeshGateway

type SwitchBotLightConfigEntry = ConfigEntry[SwitchBotLightData]


@dataclass
class SwitchBotLightData:
    """Runtime data for the integration."""

    title: str
    gateway: TelinkMeshGateway
    coordinator: DataUpdateCoordinator[None]
