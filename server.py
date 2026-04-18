#!/usr/bin/env python3
"""
MeshLink Server - reads sensor data from ESP32 bridge node via USB serial
and exposes a web dashboard + REST API.
"""

import asyncio
import json
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import serial
import serial.tools.list_ports
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

BAUD_RATE       = 115200
WEB_PORT        = int(os.getenv("PORT", 8000))
SERIAL_PORT_ENV = os.getenv("SERIAL_PORT", "")
THREE_HOURS_MS  = 3 * 60 * 60 * 1000

ALIASES_FILE = Path(__file__).parent / "aliases.json"

# Fields that are node identity / routing info — not graphed
GRAPHABLE_EXCLUDE = {"node_id", "node_name", "type", "received_at", "hops"}

# USB vendor IDs commonly used by ESP32 bridge chips
ESP32_VIDS = {
    0x10C4,  # Silicon Labs CP210x
    0x1A86,  # CH340/CH341
    0x0403,  # FTDI
    0x239A,  # Adafruit
    0x303A,  # Espressif native USB
}

# Format: [RELAY] origin=AA:BB:CC:DD:EE:FF temp=23.5 pres=858.5 hum=44.2 hops=1
HEADER_RE = re.compile(
    r"\[(SEND|RELAY)\]\s+(?:origin=)?([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\s+(.*)"
)
KV_RE = re.compile(r"(\w+)=([\d.]+)")

# --- In-memory data store ---
_store_lock = threading.Lock()
_sse_lock   = threading.Lock()

# nodes[mac] = {
#   node_id, node_name, type, hops, received_at,
#   metrics: { "temp": [[ts_ms, val], ...], "hum": [...], "pres": [...] }
# }
nodes: dict[str, dict] = {}

# aliases[mac] = friendly name, persisted to aliases.json
_aliases: dict[str, str] = {}
_aliases_lock = threading.Lock()

history: deque = deque(maxlen=500)

_stats: dict = {
    "total_messages": 0,
    "started_at": datetime.now().isoformat(),
    "serial_port": "",
    "connected": False,
}
_main_loop: asyncio.AbstractEventLoop | None = None
_sse_queues: list[asyncio.Queue] = []


# --- Alias persistence ---

def _load_aliases():
    global _aliases
    if ALIASES_FILE.exists():
        try:
            _aliases = json.loads(ALIASES_FILE.read_text())
            print(f"[meshlink] Loaded {len(_aliases)} alias(es) from {ALIASES_FILE}")
        except Exception as e:
            print(f"[meshlink] Could not load aliases: {e}")
            _aliases = {}


def _save_aliases():
    try:
        ALIASES_FILE.write_text(json.dumps(_aliases, indent=2))
    except Exception as e:
        print(f"[meshlink] Could not save aliases: {e}")


def _resolve_name(mac: str) -> str:
    with _aliases_lock:
        return _aliases.get(mac, mac)


# --- Port detection ---

def find_port() -> str:
    if SERIAL_PORT_ENV:
        return SERIAL_PORT_ENV

    ports = list(serial.tools.list_ports.comports())
    candidates = [p for p in ports if p.vid in ESP32_VIDS]

    if len(candidates) == 1:
        port = candidates[0].device
        print(f"[meshlink] Auto-detected port: {port} ({candidates[0].description})")
        return port

    if len(candidates) > 1:
        print("[meshlink] Multiple ESP32-like ports found:")
        for i, p in enumerate(candidates):
            print(f"  [{i}] {p.device} — {p.description}")
        idx = input("Select port number: ").strip()
        return candidates[int(idx)].device

    if ports:
        print("[meshlink] No ESP32 port auto-detected. Available ports:")
        for i, p in enumerate(ports):
            print(f"  [{i}] {p.device} — {p.description}")
        idx = input("Select port number: ").strip()
        return ports[int(idx)].device

    print("[meshlink] No serial ports found. Is the device plugged in?")
    sys.exit(1)


# --- Parsing ---

def parse_line(line: str) -> dict | None:
    m = HEADER_RE.search(line)
    if not m:
        return None

    mac = m.group(2).upper()
    result: dict = {
        "type":        m.group(1),
        "node_id":     mac,
        "node_name":   _resolve_name(mac),
        "received_at": datetime.now().isoformat(),
    }

    for kv in KV_RE.finditer(m.group(3)):
        key, raw = kv.group(1), kv.group(2)
        result[key] = float(raw) if '.' in raw else int(raw)

    return result


# --- Helpers ---

def _trim_metric(series: list, cutoff_ms: int) -> list:
    """Drop leading entries older than cutoff_ms (series is oldest-first)."""
    i = 0
    while i < len(series) and series[i][0] < cutoff_ms:
        i += 1
    return series[i:]


# --- SSE broadcast ---

def _broadcast(event: str, data: dict):
    if _main_loop is None:
        return
    msg = json.dumps({"event": event, "data": data})
    with _sse_lock:
        queues = _sse_queues[:]
    for q in queues:
        try:
            _main_loop.call_soon_threadsafe(q.put_nowait, msg)
        except Exception:
            pass


# --- Serial reader thread ---

def serial_reader(port: str):
    print(f"[meshlink] Connecting to {port} @ {BAUD_RATE}...")
    while True:
        try:
            with serial.Serial(port, BAUD_RATE, timeout=1) as ser:
                print(f"[meshlink] Connected. Dashboard at http://0.0.0.0:{WEB_PORT}")
                with _store_lock:
                    _stats["connected"] = True
                    _stats["serial_port"] = port
                _broadcast("status", {"connected": True, "port": port})

                while True:
                    raw = ser.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    data = parse_line(line)
                    if data:
                        print(line)
                        ts_ms = int(datetime.fromisoformat(data["received_at"]).timestamp() * 1000)
                        cutoff = ts_ms - THREE_HOURS_MS

                        new_metrics = {
                            k: v for k, v in data.items()
                            if k not in GRAPHABLE_EXCLUDE and isinstance(v, (int, float))
                        }

                        with _store_lock:
                            _stats["total_messages"] += 1
                            mac = data["node_id"]
                            prev_metrics = nodes.get(mac, {}).get("metrics", {})

                            updated_metrics = {}
                            for key, val in new_metrics.items():
                                series = prev_metrics.get(key, [])
                                series.append([ts_ms, val])
                                updated_metrics[key] = _trim_metric(series, cutoff)

                            for key, series in prev_metrics.items():
                                if key not in updated_metrics:
                                    trimmed = _trim_metric(series, cutoff)
                                    if trimmed:
                                        updated_metrics[key] = trimmed

                            nodes[mac] = {
                                "node_id":     mac,
                                "node_name":   data["node_name"],
                                "type":        data["type"],
                                "hops":        data.get("hops"),
                                "received_at": data["received_at"],
                                "metrics":     updated_metrics,
                            }
                            history.append(data)

                        _broadcast("reading", {
                            "node_id":     mac,
                            "node_name":   data["node_name"],
                            "type":        data["type"],
                            "hops":        data.get("hops"),
                            "received_at": data["received_at"],
                            "ts_ms":       ts_ms,
                            "metrics":     new_metrics,
                        })
                    else:
                        print(f"[debug] {line}")

        except serial.SerialException as e:
            print(f"[meshlink] Serial error: {e}. Retrying in 3s...")
            with _store_lock:
                _stats["connected"] = False
            _broadcast("status", {"connected": False, "port": port})
            time.sleep(3)
        except KeyboardInterrupt:
            sys.exit(0)


# --- FastAPI ---

app = FastAPI(title="MeshLink")


class AliasBody(BaseModel):
    name: str


@app.get("/", response_class=FileResponse)
async def dashboard():
    return FileResponse(Path(__file__).parent / "templates" / "index.html")


@app.get("/api/nodes")
async def get_nodes():
    with _store_lock:
        return {"nodes": list(nodes.values())}


@app.get("/api/stats")
async def get_stats():
    with _store_lock:
        started  = datetime.fromisoformat(_stats["started_at"])
        uptime_s = int((datetime.now() - started).total_seconds())
        return {**_stats, "nodes_count": len(nodes), "uptime_seconds": uptime_s}


@app.get("/api/history")
async def get_history(limit: int = 100):
    with _store_lock:
        items = list(history)
    return {"history": items[-limit:]}


@app.get("/api/aliases")
async def get_aliases():
    with _aliases_lock:
        return {"aliases": dict(_aliases)}


@app.put("/api/aliases/{mac}")
async def set_alias(mac: str, body: AliasBody):
    mac = mac.upper()
    if not re.fullmatch(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}", mac):
        raise HTTPException(status_code=400, detail="Invalid MAC address format")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name must not be empty")

    with _aliases_lock:
        _aliases[mac] = name
        _save_aliases()

    # Update the live node entry if it exists
    with _store_lock:
        if mac in nodes:
            nodes[mac]["node_name"] = name

    _broadcast("alias", {"node_id": mac, "node_name": name})
    return {"node_id": mac, "node_name": name}


@app.delete("/api/aliases/{mac}")
async def delete_alias(mac: str):
    mac = mac.upper()
    with _aliases_lock:
        if mac not in _aliases:
            raise HTTPException(status_code=404, detail="Alias not found")
        del _aliases[mac]
        _save_aliases()

    with _store_lock:
        if mac in nodes:
            nodes[mac]["node_name"] = mac

    _broadcast("alias", {"node_id": mac, "node_name": mac})
    return {"node_id": mac, "node_name": mac}


@app.get("/api/events")
async def sse_stream():
    q: asyncio.Queue = asyncio.Queue()
    with _sse_lock:
        _sse_queues.append(q)

    async def generator():
        try:
            with _store_lock:
                started  = datetime.fromisoformat(_stats["started_at"])
                snapshot = {
                    "event": "snapshot",
                    "data": {
                        "nodes": list(nodes.values()),
                        "stats": {
                            **_stats,
                            "nodes_count":    len(nodes),
                            "uptime_seconds": int((datetime.now() - started).total_seconds()),
                        },
                    },
                }
            yield f"data: {json.dumps(snapshot)}\n\n"

            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            with _sse_lock:
                try:
                    _sse_queues.remove(q)
                except ValueError:
                    pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Entry point ---

def main():
    global _main_loop

    _load_aliases()
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()

    t = threading.Thread(target=serial_reader, args=(port,), daemon=True)
    t.start()

    async def run():
        global _main_loop
        _main_loop = asyncio.get_running_loop()
        config = uvicorn.Config(app, host="0.0.0.0", port=WEB_PORT, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    asyncio.run(run())


if __name__ == "__main__":
    main()
