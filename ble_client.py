"""BLE client for Telink Mesh light devices (Sunricher/LIMENTE).

This module acts as a mesh gateway: a single BLE connection to one mesh node
gives us access to ALL devices on the mesh. Each device is identified by its
1-byte mesh address (derived from the last byte of its MAC address).

Implements the Telink BLE Mesh protocol:
- AES-128 encrypted communication
- Login/pairing via characteristic 1914
- Commands written to characteristic 1912
- Notifications received on characteristic 1911

Protocol references:
- FoxDenHome/BlissLightControl (Python)
- vpaeder/telinkpp (C++)
- Sunricher-iOS/telinkblemeshsdk (Swift)
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.service import BleakGATTCharacteristic
from bleak.exc import BleakDBusError, BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from .const import (
    BLE_GATT_OP_PAIR_ENC_FAIL,
    BLE_GATT_OP_PAIR_ENC_REQ,
    BLE_GATT_OP_PAIR_ENC_RSP,
    COMMAND_LIGHT_ATTRIBUTES_SET,
    COMMAND_LIGHT_ON_OFF,
    COMMAND_ONLINE_STATUS_REPORT,
    COMMAND_STATUS_QUERY,
    COMMAND_STATUS_REPORT,
    DEFAULT_MESH_NAME,
    DEFAULT_MESH_PASSWORD,
    DISCONNECT_DELAY,
    KEEP_ALIVE_INTERVAL,
    MANUFACTURER_ID,
    MESH_ADDRESS_BROADCAST,
    NOTIFY_CHAR_UUID,
    PAIR_CHAR_UUID,
    PLAIN_HEADER_LEN_COMMAND,
    PLAIN_HEADER_LEN_NOTIFY,
    SR_COMMAND_BRIGHTNESS,
    SR_COMMAND_ON_OFF,
    TELINK_VENDOR_ID,
    WRITE_CHAR_UUID,
)

_LOGGER = logging.getLogger(__name__)

BLEAK_EXCEPTIONS = (BleakError, BleakDBusError, asyncio.TimeoutError)


# ============================================================================
# Per-device state tracked by the mesh gateway
# ============================================================================

@dataclass
class MeshDeviceState:
    """State of a single device on the mesh."""

    mesh_address: int
    is_on: bool | None = None
    brightness: int = 0  # 0-100 percentage
    online: bool = False
    callbacks: list[Callable[[], None]] = field(default_factory=list)

    @property
    def brightness_ha(self) -> int:
        """Return brightness on HA 0-255 scale."""
        return min(255, round(self.brightness * 2.55))

    def fire_callbacks(self) -> None:
        """Notify all listeners of state change."""
        for cb in self.callbacks:
            cb()


# ============================================================================
# Telink Mesh AES Crypto Functions
# ============================================================================

try:
    from Cryptodome.Cipher import AES as _AES_Module
except ImportError:
    try:
        from Crypto.Cipher import AES as _AES_Module
    except ImportError:
        _AES_Module = None


def _aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """Standard AES-128-ECB encryption of a single 16-byte block."""
    if _AES_Module is not None:
        cipher = _AES_Module.new(key, _AES_Module.MODE_ECB)
        return cipher.encrypt(data)
    raise ImportError(
        "No AES library found. Install pycryptodome: pip install pycryptodome"
    )


def _pad_to_16(data: bytes) -> bytes:
    """Pad data with zeros to 16 bytes."""
    if len(data) >= 16:
        return data[:16]
    return data + b"\x00" * (16 - len(data))


def _telink_aes_base_encrypt(key: bytes, data: bytes) -> bytes:
    """Telink's AES encrypt: reverse key, reverse data, AES-ECB encrypt."""
    return _aes_ecb_encrypt(key[::-1], data[::-1])


def _telink_aes_att_encrypt(key: bytes, data: bytes) -> bytes:
    """Telink's AES ATT encrypt: base encrypt, then reverse result."""
    return _telink_aes_base_encrypt(key, data)[::-1]


def _bytes_xor(a: bytes, b: bytes) -> bytes:
    """XOR two byte sequences of equal length."""
    return bytes(x ^ y for x, y in zip(a, b))


def _create_login_packet(login_random: bytes, mesh_name: bytes, mesh_password: bytes) -> bytes:
    """Create the login request packet to write to the pair characteristic."""
    mesh_xor = _bytes_xor(_pad_to_16(mesh_name), _pad_to_16(mesh_password))
    padded_random = _pad_to_16(login_random)
    encrypted = _telink_aes_base_encrypt(padded_random, mesh_xor)
    return bytes([BLE_GATT_OP_PAIR_ENC_REQ]) + login_random + encrypted[8:16][::-1]


def _derive_session_key(
    login_random: bytes, mesh_name: bytes, mesh_password: bytes, login_response: bytes
) -> bytes:
    """Derive the session key from the login response."""
    if login_response[0] == BLE_GATT_OP_PAIR_ENC_FAIL:
        raise ValueError("Login failed: device rejected credentials")
    if login_response[0] != BLE_GATT_OP_PAIR_ENC_RSP:
        raise ValueError(f"Unexpected login response type: 0x{login_response[0]:02x}")

    resp_data = login_response[1:]
    mesh_xor = _bytes_xor(_pad_to_16(mesh_name), _pad_to_16(mesh_password))
    padded_device_random = _pad_to_16(resp_data[:8])

    encrypt_check = _telink_aes_att_encrypt(padded_device_random, mesh_xor)
    if encrypt_check[:8] != resp_data[8:16]:
        raise ValueError("Login verification failed: device response mismatch")

    session_key_base = login_random + resp_data[:8]
    return _telink_aes_att_encrypt(mesh_xor, session_key_base)


def _make_ivm(sequence_number: int, mac: bytes) -> bytes:
    """Make the IV for command encryption."""
    mac_reversed = mac[::-1]
    return mac_reversed[:4] + bytes([
        1,
        sequence_number & 0xFF,
        (sequence_number >> 8) & 0xFF,
        (sequence_number >> 16) & 0xFF,
    ])


def _make_ivs(mac: bytes, data: bytes) -> bytes:
    """Make the IV for notification decryption."""
    mac_reversed = mac[::-1]
    return mac_reversed[:3] + data[:5]


def _telink_encrypt_command(
    session_key: bytes, ivm: bytes, payload: bytes
) -> bytes:
    """Encrypt a 20-byte command packet using Telink mesh encryption."""
    payload_list = list(payload)
    offset_after_check = PLAIN_HEADER_LEN_COMMAND + 2
    encrypted_len = len(payload) - offset_after_check

    ivm_padded = _pad_to_16(ivm + bytes([encrypted_len]))
    encrypted_list = list(_telink_aes_att_encrypt(session_key, ivm_padded))
    for i in range(encrypted_len):
        encrypted_list[i] ^= payload_list[i + offset_after_check]
    encrypted = _telink_aes_att_encrypt(session_key, bytes(encrypted_list))
    for i in range(2):
        payload_list[i + PLAIN_HEADER_LEN_COMMAND] = encrypted[i]

    ivm_padded = _pad_to_16(b"\x00" + ivm)
    encrypted = _telink_aes_att_encrypt(session_key, ivm_padded)
    for i in range(encrypted_len):
        payload_list[i + offset_after_check] ^= encrypted[i]

    return bytes(payload_list)


def _telink_decrypt_notify(
    session_key: bytes, ivs: bytes, payload: bytes
) -> bytes:
    """Decrypt a notification packet using Telink mesh decryption."""
    payload_list = bytearray(payload)
    offset_after_check = PLAIN_HEADER_LEN_NOTIFY + 2
    encrypted_len = len(payload) - offset_after_check

    ivs_padded = _pad_to_16(b"\x00" + ivs)
    encrypted = _telink_aes_att_encrypt(session_key, ivs_padded)
    for i in range(encrypted_len):
        payload_list[i + offset_after_check] ^= encrypted[i]

    ivs_padded = _pad_to_16(ivs + bytes([encrypted_len]))
    encrypted_list = list(_telink_aes_att_encrypt(session_key, ivs_padded))
    for i in range(encrypted_len):
        encrypted_list[i] ^= payload_list[i + offset_after_check]
    encrypted = _telink_aes_att_encrypt(session_key, bytes(encrypted_list))

    if bytes(payload_list[PLAIN_HEADER_LEN_NOTIFY:PLAIN_HEADER_LEN_NOTIFY + 2]) != encrypted[:2]:
        _LOGGER.warning("Notification decryption: auth tag mismatch")

    return bytes(payload_list)


# ============================================================================
# Advertisement Data Parsing
# ============================================================================

def parse_advertisement_data(mfr_data: bytes) -> dict[str, Any]:
    """Parse Telink mesh advertisement manufacturer data."""
    result: dict[str, Any] = {}
    if len(mfr_data) < 9:
        return result

    result["sequence_number"] = mfr_data[6]
    result["isOn"] = bool(mfr_data[7] & 0x80)
    result["brightness"] = mfr_data[7] & 0x7F

    try:
        v_idx = mfr_data.index(0x56)  # 'V'
        fw = mfr_data[v_idx : v_idx + 4].decode("ascii", errors="replace")
        result["firmware"] = fw
    except (ValueError, IndexError):
        pass

    return result


def _mac_string_to_bytes(mac_str: str) -> bytes:
    """Convert MAC address string 'AA:BB:CC:DD:EE:FF' to bytes."""
    return bytes(int(x, 16) for x in mac_str.split(":"))


# ============================================================================
# Telink Mesh Gateway
# ============================================================================

class TelinkMeshGateway:
    """BLE gateway for a Telink Mesh network.

    Connects to one mesh node and provides access to ALL devices on the mesh.
    Tracks per-device state and notifies listeners when new devices are
    discovered or existing device state changes.
    """

    def __init__(
        self,
        ble_device: BLEDevice,
        advertisement_data: AdvertisementData | None = None,
        mesh_name: str | None = None,
        mesh_password: str | None = None,
    ) -> None:
        """Initialize the mesh gateway."""
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
        self._client: BleakClientWithServiceCache | None = None
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._keep_alive_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

        # Telink Mesh session state
        self._session_key: bytes | None = None
        self._mesh_name = (mesh_name or DEFAULT_MESH_NAME).encode("ascii")
        self._mesh_password = (mesh_password or DEFAULT_MESH_PASSWORD).encode("ascii")
        self._mac_bytes: bytes = _mac_string_to_bytes(ble_device.address)
        self._sequence_number: int = 1
        self._logged_in: bool = False

        # The mesh address of the BLE node we connect through
        self._gateway_mesh_address: int = self._mac_bytes[-1]

        # Per-device state: mesh_address -> MeshDeviceState
        self._devices: dict[int, MeshDeviceState] = {}

        # Callback for when a new device is discovered on the mesh
        self._new_device_callbacks: list[Callable[[int], None]] = []

        self._firmware: str = ""

        _LOGGER.debug(
            "MeshGateway: BLE node=%s MAC=%s gateway_mesh_addr=0x%02x",
            ble_device.name or ble_device.address,
            ble_device.address,
            self._gateway_mesh_address,
        )

        # Parse initial state from advertisement if available
        if advertisement_data:
            self._parse_advertisement(advertisement_data)

    @property
    def address(self) -> str:
        """Return the BLE address of the gateway node."""
        return self._ble_device.address

    @property
    def name(self) -> str:
        """Return the device name of the gateway node."""
        return self._ble_device.name or self._ble_device.address

    @property
    def firmware(self) -> str:
        """Return the firmware version."""
        return self._firmware

    @property
    def devices(self) -> dict[int, MeshDeviceState]:
        """Return all known mesh devices."""
        return self._devices

    def get_device(self, mesh_address: int) -> MeshDeviceState:
        """Get or create a device state for the given mesh address."""
        if mesh_address not in self._devices:
            self._devices[mesh_address] = MeshDeviceState(mesh_address=mesh_address)
            _LOGGER.info(
                "MeshGateway: New device discovered: mesh_addr=0x%02x",
                mesh_address,
            )
            # Notify listeners about new device
            for cb in self._new_device_callbacks:
                cb(mesh_address)
        return self._devices[mesh_address]

    def register_new_device_callback(
        self, callback: Callable[[int], None]
    ) -> Callable[[], None]:
        """Register a callback for when new mesh devices are discovered.

        Returns a function to unregister the callback.
        """
        self._new_device_callbacks.append(callback)

        def remove() -> None:
            self._new_device_callbacks.remove(callback)

        return remove

    def set_ble_device_and_advertisement_data(
        self,
        ble_device: BLEDevice,
        advertisement_data: AdvertisementData,
    ) -> None:
        """Update the BLE device and advertisement data."""
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
        self._mac_bytes = _mac_string_to_bytes(ble_device.address)
        self._parse_advertisement(advertisement_data)

    def _parse_advertisement(self, advertisement_data: AdvertisementData) -> None:
        """Parse state from advertisement data."""
        mfr_data = advertisement_data.manufacturer_data.get(MANUFACTURER_ID)
        if not mfr_data:
            return
        parsed = parse_advertisement_data(mfr_data)
        if not parsed:
            return
        if "firmware" in parsed:
            self._firmware = parsed["firmware"]

    # ========================================================================
    # BLE Connection Management
    # ========================================================================

    async def _ensure_connected(self) -> BleakClientWithServiceCache:
        """Ensure we have an active BLE connection with a valid session."""
        if self._client and self._client.is_connected and self._logged_in:
            self._reset_disconnect_timer()
            return self._client

        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
            self._logged_in = False

        _LOGGER.debug("MeshGateway: Connecting via %s...", self.name)
        client = await establish_connection(
            BleakClientWithServiceCache,
            self._ble_device,
            self.name,
        )

        try:
            await client.start_notify(
                str(NOTIFY_CHAR_UUID), self._notification_handler
            )
        except Exception as ex:
            _LOGGER.warning(
                "MeshGateway: Failed to subscribe to notifications: %s", ex
            )

        self._client = client
        await self._login()
        self._reset_disconnect_timer()
        self._start_keep_alive()
        _LOGGER.debug("MeshGateway: Connected and logged in")
        return client

    async def _login(self) -> None:
        """Perform the Telink Mesh login handshake."""
        if not self._client:
            raise RuntimeError("Not connected")

        _LOGGER.debug("MeshGateway: Starting Telink mesh login...")
        login_random = os.urandom(8)
        login_packet = _create_login_packet(
            login_random, self._mesh_name, self._mesh_password
        )

        await self._client.write_gatt_char(
            str(PAIR_CHAR_UUID), login_packet, response=True
        )
        await asyncio.sleep(0.5)

        response = bytes(await self._client.read_gatt_char(str(PAIR_CHAR_UUID)))
        _LOGGER.debug(
            "MeshGateway: Login response type=0x%02x",
            response[0] if response else 0,
        )

        if not response or len(response) < 17:
            raise ValueError(
                f"Invalid login response length: {len(response) if response else 0}"
            )

        if response[0] == BLE_GATT_OP_PAIR_ENC_FAIL:
            raise ValueError("Login failed: device rejected credentials.")

        self._session_key = _derive_session_key(
            login_random, self._mesh_name, self._mesh_password, response
        )
        self._logged_in = True
        self._sequence_number = 1
        _LOGGER.info("MeshGateway: Login successful!")

        try:
            await self._client.write_gatt_char(
                str(NOTIFY_CHAR_UUID), b"\x01", response=True
            )
        except Exception as ex:
            _LOGGER.debug(
                "MeshGateway: Failed to write notification enable byte: %s", ex
            )

    # ========================================================================
    # Notification Handling
    # ========================================================================

    def _notification_handler(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle encrypted notification from device."""
        raw = bytes(data)

        if not self._session_key or len(raw) < 20:
            return

        try:
            ivs = _make_ivs(self._mac_bytes, raw)
            decrypted = _telink_decrypt_notify(self._session_key, ivs, raw)
            self._parse_notification(decrypted)
        except Exception as ex:
            _LOGGER.debug("MeshGateway: Failed to decrypt notification: %s", ex)

    def _parse_notification(self, data: bytes) -> None:
        """Parse a decrypted notification packet."""
        if len(data) < 20:
            return

        src_addr = data[3] | (data[4] << 8)
        command = data[7]
        payload = data[10:]

        if command == COMMAND_ONLINE_STATUS_REPORT:
            self._parse_online_status(payload)
        elif command == COMMAND_STATUS_REPORT:
            self._parse_status_report(src_addr, payload)

    def _parse_online_status(self, payload: bytes) -> None:
        """Parse online status report (0xDC).

        Payload contains pairs of device status entries, 4 bytes each:
          [0] = mesh address (1 byte)
          [1] = online flag (non-zero = online)
          [2] = brightness (0-100)
          [3] = reserved (0xFF typically)
        """
        for offset in (0, 4):
            if offset + 3 > len(payload):
                break
            dev_addr = payload[offset]
            dev_online = payload[offset + 1]
            dev_brightness = payload[offset + 2]

            if dev_addr == 0:
                continue

            device = self.get_device(dev_addr)
            device.online = dev_online != 0
            device.brightness = dev_brightness
            device.is_on = dev_brightness > 0

            _LOGGER.debug(
                "MeshGateway: 0xDC addr=0x%02x online=%s on=%s brightness=%d",
                dev_addr, device.online, device.is_on, device.brightness,
            )
            device.fire_callbacks()

    def _parse_status_report(self, src_addr: int, payload: bytes) -> None:
        """Parse device status report (0xDB).

        Sunricher format:
        payload[6] = brightness (0-100 percentage)
        payload[7] = mode/status flags (0x80 = on, 0x81 = off)
        """
        dev_addr = src_addr & 0xFF
        if dev_addr == 0:
            return

        if len(payload) >= 8:
            brightness = payload[6]
            device = self.get_device(dev_addr)
            device.brightness = brightness
            device.is_on = brightness > 0
            _LOGGER.debug(
                "MeshGateway: 0xDB addr=0x%02x brightness=%d",
                dev_addr, brightness,
            )
            device.fire_callbacks()

    # ========================================================================
    # Command Sending
    # ========================================================================

    def _build_encrypted_command(
        self, command: int, payload: bytes, mesh_address: int
    ) -> bytes:
        """Build an encrypted 20-byte command packet."""
        if not self._session_key:
            raise RuntimeError("Not logged in")

        seq = self._sequence_number
        self._sequence_number += 1

        if len(payload) > 10:
            payload = payload[:10]
        elif len(payload) < 10:
            payload = payload + b"\x00" * (10 - len(payload))

        plain = bytes([
            seq & 0xFF,
            (seq >> 8) & 0xFF,
            (seq >> 16) & 0xFF,
            0x00, 0x00,
            mesh_address & 0xFF,
            (mesh_address >> 8) & 0xFF,
            command,
            TELINK_VENDOR_ID & 0xFF,
            (TELINK_VENDOR_ID >> 8) & 0xFF,
        ]) + payload

        _LOGGER.debug(
            "MeshGateway: cmd=0x%02x addr=0x%04x seq=%d",
            command, mesh_address, seq,
        )

        ivm = _make_ivm(seq, self._mac_bytes)
        return _telink_encrypt_command(self._session_key, ivm, plain)

    async def send_mesh_command(
        self, command: int, payload: bytes, mesh_address: int
    ) -> None:
        """Send an encrypted Telink mesh command."""
        async with self._lock:
            client = await self._ensure_connected()
            encrypted = self._build_encrypted_command(command, payload, mesh_address)
            await client.write_gatt_char(
                str(WRITE_CHAR_UUID), encrypted, response=False
            )

    # ========================================================================
    # Light Control (convenience methods targeting a specific mesh address)
    # ========================================================================

    async def turn_on(self, mesh_address: int) -> None:
        """Turn a light on."""
        _LOGGER.debug("MeshGateway: Turning on 0x%02x", mesh_address)
        try:
            await self.send_mesh_command(
                SR_COMMAND_ON_OFF, bytes([0x01, 0x00, 0x00]), mesh_address
            )
        except Exception:
            await self.send_mesh_command(
                COMMAND_LIGHT_ON_OFF, bytes([0x01, 0x00, 0x00]), mesh_address
            )
        device = self.get_device(mesh_address)
        device.is_on = True
        device.fire_callbacks()

    async def turn_off(self, mesh_address: int) -> None:
        """Turn a light off."""
        _LOGGER.debug("MeshGateway: Turning off 0x%02x", mesh_address)
        try:
            await self.send_mesh_command(
                SR_COMMAND_ON_OFF, bytes([0x00, 0x00, 0x00]), mesh_address
            )
        except Exception:
            await self.send_mesh_command(
                COMMAND_LIGHT_ON_OFF, bytes([0x00, 0x00, 0x00]), mesh_address
            )
        device = self.get_device(mesh_address)
        device.is_on = False
        device.fire_callbacks()

    async def set_brightness(self, mesh_address: int, brightness_pct: int) -> None:
        """Set brightness (1-100 percentage) for a specific device."""
        brightness_pct = max(1, min(100, brightness_pct))
        _LOGGER.debug(
            "MeshGateway: Setting brightness 0x%02x -> %d%%",
            mesh_address, brightness_pct,
        )
        try:
            await self.send_mesh_command(
                SR_COMMAND_BRIGHTNESS, bytes([brightness_pct]), mesh_address
            )
        except Exception:
            await self.send_mesh_command(
                COMMAND_LIGHT_ATTRIBUTES_SET,
                bytes([brightness_pct, 0, 0, 0, 0, 0, 0, 1]),
                mesh_address,
            )
        device = self.get_device(mesh_address)
        device.brightness = brightness_pct
        device.is_on = True
        device.fire_callbacks()

    async def update(self) -> None:
        """Poll all devices for current state."""
        try:
            await self.send_mesh_command(
                COMMAND_STATUS_QUERY,
                bytes([0x10]),
                MESH_ADDRESS_BROADCAST,
            )
        except BLEAK_EXCEPTIONS as ex:
            _LOGGER.debug("MeshGateway: Update failed: %s", ex)
            raise

    # ========================================================================
    # Connection Lifecycle
    # ========================================================================

    def _reset_disconnect_timer(self) -> None:
        """Reset the disconnect timer."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        loop = asyncio.get_running_loop()
        self._disconnect_timer = loop.call_later(
            DISCONNECT_DELAY, lambda: asyncio.ensure_future(self._disconnect())
        )

    def _start_keep_alive(self) -> None:
        """Start the keep-alive task."""
        if self._keep_alive_task and not self._keep_alive_task.done():
            return
        self._keep_alive_task = asyncio.ensure_future(self._keep_alive_loop())

    async def _keep_alive_loop(self) -> None:
        """Send periodic status queries to maintain connection and get state."""
        try:
            while self._client and self._client.is_connected and self._logged_in:
                if not self._lock.locked():
                    try:
                        await self.send_mesh_command(
                            COMMAND_STATUS_QUERY,
                            bytes([0x10]),
                            MESH_ADDRESS_BROADCAST,
                        )
                    except BLEAK_EXCEPTIONS:
                        _LOGGER.debug("MeshGateway: Keep-alive failed")
                        break
                    except Exception:
                        _LOGGER.debug("MeshGateway: Keep-alive error", exc_info=True)
                        break
                await asyncio.sleep(KEEP_ALIVE_INTERVAL)
        except asyncio.CancelledError:
            pass

    def _stop_keep_alive(self) -> None:
        """Stop the keep-alive task."""
        if self._keep_alive_task and not self._keep_alive_task.done():
            self._keep_alive_task.cancel()
            self._keep_alive_task = None

    async def _disconnect(self) -> None:
        """Disconnect from the device."""
        self._stop_keep_alive()
        self._logged_in = False
        self._session_key = None
        if self._client and self._client.is_connected:
            _LOGGER.debug("MeshGateway: Disconnecting")
            try:
                await self._client.disconnect()
            except Exception:
                _LOGGER.debug("MeshGateway: Error disconnecting", exc_info=True)
            self._client = None

    async def stop(self) -> None:
        """Stop the gateway and disconnect."""
        self._stop_keep_alive()
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None
        await self._disconnect()
