# Limente BLE Mesh Light

A Home Assistant custom integration for controlling **Limente BLE Mesh LED lights** over Bluetooth Low Energy.

These lights use Sunricher/Telink Semiconductor BLE Mesh hardware. The integration connects to a single mesh node which acts as a gateway, discovering and controlling all devices on the same mesh network.

> **Disclaimer**: This integration is entirely vibe coded. Use at your own risk. There are no guarantees of correctness, reliability, or completeness. It may break at any time, for any reason, or for no reason at all.

## Features

- Automatic discovery of Limente BLE Mesh devices via Bluetooth
- Single BLE connection controls all lights on the mesh network
- Dynamic device discovery -- new mesh devices appear automatically
- On/off and brightness control
- AES-128 encrypted Telink mesh protocol
- Auto-reconnection and keep-alive handling

## Requirements

- Home Assistant with Bluetooth support
- A Bluetooth adapter accessible to Home Assistant
- Limente BLE Mesh LED lights (Sunricher/Telink-based, manufacturer ID `529`)

## Installation

### HACS (Manual Repository)

1. Open HACS in Home Assistant
2. Go to **Integrations**
3. Click the three-dot menu and select **Custom repositories**
4. Add `https://github.com/kbrah/limente-ha-integration` with category **Integration**
5. Search for "Limente BLE Mesh Light" and install it
6. Restart Home Assistant

### Manual

1. Copy the `limente_light` directory into your Home Assistant `custom_components` folder:
   ```
   custom_components/
   └── limente_light/
       ├── __init__.py
       ├── ble_client.py
       ├── config_flow.py
       ├── const.py
       ├── light.py
       ├── manifest.json
       ├── models.py
       ├── strings.json
       └── translations/
           └── en.json
   ```
2. Restart Home Assistant

## Configuration

The integration supports two setup methods:

1. **Automatic discovery** -- Home Assistant will detect Limente BLE Mesh devices broadcasting via Bluetooth and prompt you to set up the integration.

2. **Manual setup** -- Go to **Settings > Devices & Services > Add Integration**, search for "Limente BLE Mesh Light", and select your device from the list.

Only one config entry is created per mesh network. A single connection to any node provides access to all devices on the mesh.

### Default Mesh Credentials

The integration uses Sunricher factory default credentials:

- **Mesh name**: `Srm@7478@a`
- **Mesh password**: `475869`

If your mesh uses different credentials, you will need to modify `const.py`.

## Supported Features

| Feature | Status |
|---|---|
| On/Off | Supported |
| Brightness | Supported |
| RGB Color | Not yet exposed |
| Color Temperature | Not yet exposed |

The underlying protocol supports RGB and color temperature commands, but the light entity currently only exposes brightness control.

## How It Works

The integration implements the Telink BLE Mesh protocol:

1. Discovers devices via BLE advertisements (manufacturer ID `529` or name prefix `Srm@`)
2. Connects to a single mesh node over BLE GATT
3. Authenticates using AES-128 encrypted handshake with mesh credentials
4. Sends encrypted commands to control lights (Sunricher commands with generic Telink fallback)
5. Receives mesh notifications for device discovery and state updates
6. Maintains connection with periodic keep-alive queries (every 10s)
7. Auto-disconnects after 5 minutes of inactivity

## Protocol References

- [FoxDenHome/BlissLightControl](https://github.com/FoxDenHome/BlissLightControl) (Python)
- [vpaeder/telinkpp](https://github.com/vpaeder/telinkpp) (C++)
- [Sunricher-iOS/telinkblemeshsdk](https://github.com/nicepkg/nice-zustand/tree/master) (Swift)

## License

No license specified.
