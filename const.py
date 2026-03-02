"""Constants for the SwitchBot Light integration (Telink Mesh protocol)."""

from uuid import UUID

DOMAIN = "switchbot_light"

# BLE manufacturer ID — Telink vendor ID 0x0211 (decimal 529)
# Registered to Wonderlabs but actually a Telink Semiconductor chip
MANUFACTURER_ID = 529

# Telink Mesh Vendor ID (little-endian: 0x11, 0x02)
TELINK_VENDOR_ID = 0x0211

# ============================================================================
# GATT Service and Characteristics (Telink BLE Mesh)
# ============================================================================
SERVICE_UUID = UUID("00010203-0405-0607-0809-0a0b0c0d1910")
NOTIFY_CHAR_UUID = UUID("00010203-0405-0607-0809-0a0b0c0d1911")  # Notify + Write
WRITE_CHAR_UUID = UUID("00010203-0405-0607-0809-0a0b0c0d1912")   # Write (commands)
PAIR_CHAR_UUID = UUID("00010203-0405-0607-0809-0a0b0c0d1914")    # Pair (login)

# ============================================================================
# Telink Mesh Login Protocol Constants
# ============================================================================
BLE_GATT_OP_PAIR_ENC_REQ = 0x0C
BLE_GATT_OP_PAIR_ENC_RSP = 0x0D
BLE_GATT_OP_PAIR_ENC_FAIL = 0x0E

# Default Telink mesh credentials (Sunricher factory defaults from SDK)
DEFAULT_MESH_NAME = "Srm@7478@a"
DEFAULT_MESH_PASSWORD = "475869"

# Encryption packet header lengths
PLAIN_HEADER_LEN_COMMAND = 3
PLAIN_HEADER_LEN_NOTIFY = 5

# ============================================================================
# Telink Mesh Command Codes
# ============================================================================

# --- Generic Telink mesh commands ---
COMMAND_ONLINE_STATUS_REPORT = 0xDC
COMMAND_GROUP_ID_QUERY = 0xDD
COMMAND_GROUP_ID_REPORT = 0xD4
COMMAND_GROUP_EDIT = 0xD7
COMMAND_ADDRESS_EDIT = 0xE0
COMMAND_ADDRESS_REPORT = 0xE1
COMMAND_RESET = 0xE3
COMMAND_TIME_SET = 0xE4
COMMAND_TIME_QUERY = 0xE8
COMMAND_DEVICE_INFO_QUERY = 0xEA
COMMAND_DEVICE_INFO_REPORT = 0xEB

# --- Light-specific commands (telinkpp / generic Telink) ---
COMMAND_LIGHT_ON_OFF = 0xF0
COMMAND_LIGHT_ATTRIBUTES_SET = 0xF1
COMMAND_SCENARIO_LOAD = 0xF2
COMMAND_SCENARIO_EDIT = 0xF3
COMMAND_STATUS_QUERY = 0xDA
COMMAND_STATUS_REPORT = 0xDB

# --- Sunricher-specific commands (from MeshCommand.swift) ---
# These may work if the device uses the Sunricher command set
SR_COMMAND_ON_OFF = 0xD0       # param: 0x01=on, 0x00=off
SR_COMMAND_BRIGHTNESS = 0xD2   # param: 0-100
SR_COMMAND_COLOR_SET = 0xE2    # param=0x04: RGB, param=0x05: CCT

# Mesh address constants
MESH_ADDRESS_BROADCAST = 0xFFFF
MESH_ADDRESS_UNKNOWN = -1

# ============================================================================
# Timings
# ============================================================================
DEVICE_TIMEOUT = 30
UPDATE_SECONDS = 60
DISCONNECT_DELAY = 300  # Keep connection alive 5 minutes
KEEP_ALIVE_INTERVAL = 10  # Send keep-alive every 10 seconds

# ============================================================================
# Advertisement parsing
# ============================================================================
ADV_NAME_PREFIX = "Srm@"

# Color modes from advertisement data
COLOR_MODE_RGB = 1
COLOR_MODE_SCENE = 2
COLOR_MODE_MUSIC = 3
COLOR_MODE_CONTROLLER = 4
COLOR_MODE_COLOR_TEMP = 5
