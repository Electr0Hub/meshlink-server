# meshlink-server

<img width="1853" height="926" alt="image" src="https://github.com/user-attachments/assets/48447070-ed5d-45cd-91f2-c93608d9d7c0" />


Python service that reads structured serial output from a USB-connected ESP32 bridge node, parses it into JSON sensor data, and serves a live web dashboard.

Part of the [MeshLink](../meshlink-station) home mesh network. The bridge ESP32 runs `meshlink-station` firmware in relay-only mode, printing received mesh traffic over USB serial.

## Architecture

```
[Sensor nodes] --ESP-NOW--> [Bridge ESP32] --USB serial--> [server.py] --> Dashboard / REST API
```

## Requirements

- Python 3.10+
- ESP32 bridge node connected via USB

## Setup

```bash
pip install -r requirements.txt
```

## Running

```bash
# Auto-detect ESP32 port
python server.py

# Specify port explicitly
python server.py /dev/ttyACM0
```

The dashboard is available at `http://localhost:8000` once connected.

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8000` | Web server port |
| `SERIAL_PORT` | _(auto)_ | Serial port to use, skips detection |

## Serial Format

The server expects lines from the bridge in this format:

```
[RELAY] origin=AA:BB:CC:DD:EE:FF msg=42 temp=23.5 hops=1
[SEND]  origin=AA:BB:CC:DD:EE:FF msg=10 temp=22.0 hops=0
```

Nodes are identified by their MAC address. Unrecognized lines are printed as `[debug]`.

## Device Names

Nodes are identified by MAC address by default. You can assign persistent friendly names via the API — they survive server restarts and are stored in `aliases.json`.

```bash
# Assign a name
curl -X PUT http://localhost:8000/api/aliases/AA:BB:CC:DD:EE:FF \
  -H "Content-Type: application/json" \
  -d '{"name": "kitchen"}'

# Remove a name (reverts to MAC)
curl -X DELETE http://localhost:8000/api/aliases/AA:BB:CC:DD:EE:FF
```

## REST API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/nodes` | All nodes with current state and metric history |
| `GET` | `/api/stats` | Server stats (uptime, message count, port, connection status) |
| `GET` | `/api/history` | Last N raw readings (`?limit=100`) |
| `GET` | `/api/aliases` | All MAC → name mappings |
| `PUT` | `/api/aliases/{mac}` | Set friendly name for a MAC address |
| `DELETE` | `/api/aliases/{mac}` | Remove friendly name |
| `GET` | `/api/events` | SSE stream of live events |

### SSE Events

The `/api/events` endpoint streams Server-Sent Events:

| Event | Description |
|---|---|
| `snapshot` | Full state on connect (all nodes + stats) |
| `reading` | New sensor reading from a node |
| `alias` | A node name was added or removed |
| `status` | Serial connection connected/disconnected |

## Project Structure

```
server.py         # Serial reader, parser, FastAPI app
templates/
  index.html      # Web dashboard
aliases.json      # Persistent MAC → name map (auto-created)
requirements.txt
```
