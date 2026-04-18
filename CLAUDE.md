# MeshLink Server - Serial Reader for ESP-NOW Mesh

## Project Overview

Python service that reads structured serial output from a USB-connected ESP32 bridge node and parses it into JSON sensor data. This is the server-side component of the MeshLink home mesh network.

The bridge ESP32 runs the same `meshlink-station` firmware but has no BMP280 — it operates in relay-only mode, printing `[RELAY]` lines over USB serial as it receives mesh traffic.

Companion project: `../meshlink-station` (ESP32 firmware for all nodes).

## Architecture

```
[Sensor nodes] --ESP-NOW--> [Bridge ESP32] --USB serial--> [server.py] --> JSON stdout
```

### How It Works
1. Opens serial port (`/dev/ttyUSB0` default, or argv[1]) at 115200 baud
2. Reads lines, matches against `SENSOR_RE` regex
3. Matched lines are parsed into JSON with fields: `type`, `node_name`, `node_id`, `msg_id`, `temperature`, `hops`, `received_at`
4. Unmatched lines are passed through as `[debug]` for diagnostics
5. Auto-reconnects on serial errors (3s retry)

### Serial Line Format Expected
```
[RELAY] origin=kitchen(2) msg=42 temp=23.5 hops=1
[SEND] origin=living-room(1) msg=10 temp=22.0 hops=0
```

The regex captures: type (SEND/RELAY), node_name, node_id, msg_id, temperature, hops.

**Important**: The regex pattern must stay in sync with the `Serial.printf` format strings in `meshlink-station/src/main.cpp`. Any firmware format change requires updating `SENSOR_RE`.

## Project Structure

```
server.py            # Single-file server: serial reading, parsing, JSON output
requirements.txt     # pyserial>=3.5
```

## Running

```bash
pip install -r requirements.txt

# Default port /dev/ttyUSB0
python server.py

# Custom port
python server.py /dev/ttyACM0
```

## Dependencies

- Python 3.10+
- `pyserial>=3.5`

## Key Design Decisions

- Single-file design — intentionally minimal
- JSON to stdout (not a database or HTTP server yet) — easy to pipe into other tools
- Unrecognized lines printed as `[debug]` rather than discarded — aids development
- Infinite reconnect loop — the server is meant to run unattended

## Development Guidelines

- Keep `server.py` as the single entry point
- Any new serial line formats from the firmware need corresponding regex updates
- `parse_line()` returns `dict | None` — add new fields there as the payload struct grows
- Pressure data is present in the firmware payload but not yet parsed by the server regex
