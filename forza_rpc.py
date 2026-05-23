#!/usr/bin/env python3
"""Forza Horizon 6 Discord Rich Presence.

Listens for Forza's Data Out UDP telemetry and mirrors live driving state to
Discord. Works on Linux (native or Proton) and Windows.

Setup:
  1. In Forza: Settings > HUD and Gameplay > Telemetry
       Data Out:          On
       Data Out IP:       127.0.0.1
       Data Out IP Port:  5300
  2. Create a Discord Application at https://discord.com/developers/applications
     Copy the Application ID into DISCORD_CLIENT_ID below.
     Upload assets named: forza, class_d, class_c, class_b, class_a,
                          class_s1, class_s2, class_x
  3. pip install -r requirements.txt
  4. python forza_rpc.py
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from pypresence import Presence
from pypresence.exceptions import PipeClosed

DISCORD_CLIENT_ID = "1507700758743613520"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 5300
UPDATE_INTERVAL = 5.0  # Discord rate-limits presence updates to ~5s
IDLE_TIMEOUT = 10.0    # seconds without packets before we mark as "in menus"
UNITS = "mph"          # "kmh" or "mph"

# Forza Horizon Dash packet = 324 bytes (Sled 232 + 12 pad + Dash extras).
HORIZON_PACKET_LEN = 324

SCRIPT_DIR = Path(__file__).resolve().parent
CAR_NAMES_FILE = SCRIPT_DIR / "car_names.json"
UNKNOWN_CARS_LOG = SCRIPT_DIR / "unknown_cars.log"


@dataclass
class Telemetry:
    is_race_on: bool
    rpm: float
    speed_mps: float
    power_w: float
    gear: int
    car_ordinal: int
    car_class: int
    car_pi: int
    current_lap: float
    best_lap: float
    last_lap: float
    lap_number: int
    race_position: int
    distance_m: float
    fuel: float
    boost: float

    @property
    def speed_display(self) -> int:
        if UNITS == "mph":
            return int(self.speed_mps * 2.23694)
        return int(self.speed_mps * 3.6)

    @property
    def power_hp(self) -> int:
        # watts -> horsepower
        return int(self.power_w / 745.7) if self.power_w > 0 else 0

    @property
    def distance_km(self) -> float:
        return self.distance_m / 1000.0


CLASS_NAMES = {0: "D", 1: "C", 2: "B", 3: "A", 4: "S1", 5: "S2", 6: "X"}


def load_car_names() -> dict[str, str]:
    if not CAR_NAMES_FILE.exists():
        return {}
    try:
        raw = json.loads(CAR_NAMES_FILE.read_text())
    except json.JSONDecodeError as exc:
        print(f"car_names.json parse error: {exc}")
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, str)}


def log_unknown_car(ordinal: int, car_class: int, pi: int, seen: set[int]) -> None:
    if ordinal in seen or ordinal == 0:
        return
    seen.add(ordinal)
    class_label = CLASS_NAMES.get(car_class, f"Class {car_class}")
    line = f'  "{ordinal}": "?",  // {class_label} {pi}\n'
    try:
        with UNKNOWN_CARS_LOG.open("a") as f:
            f.write(line)
        print(f"[new car] ordinal {ordinal} ({class_label} {pi}) — logged to unknown_cars.log")
    except OSError:
        pass


def parse_horizon_packet(data: bytes) -> Telemetry | None:
    """Parse a Forza Horizon Dash UDP packet. Returns None if length is wrong."""
    if len(data) < HORIZON_PACKET_LEN:
        return None

    # Sled portion fields.
    is_race_on = struct.unpack_from("<i", data, 0)[0] == 1
    rpm = struct.unpack_from("<f", data, 16)[0]
    car_ordinal = struct.unpack_from("<i", data, 212)[0]
    car_class = struct.unpack_from("<i", data, 216)[0]
    car_pi = struct.unpack_from("<i", data, 220)[0]

    # Dash portion (offsets absolute into packet).
    speed_mps = struct.unpack_from("<f", data, 256)[0]
    power_w = struct.unpack_from("<f", data, 260)[0]
    boost = struct.unpack_from("<f", data, 276)[0]
    fuel = struct.unpack_from("<f", data, 276)[0]
    distance_m = struct.unpack_from("<f", data, 280)[0]
    best_lap = struct.unpack_from("<f", data, 284)[0]
    last_lap = struct.unpack_from("<f", data, 288)[0]
    current_lap = struct.unpack_from("<f", data, 292)[0]
    lap_number = struct.unpack_from("<H", data, 300)[0]
    race_position = struct.unpack_from("<B", data, 302)[0]
    gear = struct.unpack_from("<B", data, 307)[0]

    return Telemetry(
        is_race_on=is_race_on,
        rpm=rpm,
        speed_mps=speed_mps,
        power_w=power_w,
        gear=gear,
        car_ordinal=car_ordinal,
        car_class=car_class,
        car_pi=car_pi,
        current_lap=current_lap,
        best_lap=best_lap,
        last_lap=last_lap,
        lap_number=lap_number,
        race_position=race_position,
        distance_m=distance_m,
        fuel=fuel,
        boost=boost,
    )


def format_lap(seconds: float) -> str:
    if seconds <= 0:
        return "--:--.---"
    minutes = int(seconds // 60)
    rest = seconds - minutes * 60
    return f"{minutes}:{rest:06.3f}"


def class_asset_key(car_class: int) -> str:
    name = CLASS_NAMES.get(car_class, "").lower()
    return f"class_{name}" if name else "forza"


def build_presence(
    t: Telemetry,
    session_start: float,
    car_names: dict[str, str],
    unknown_seen: set[int],
) -> dict:
    unit = "mph" if UNITS == "mph" else "km/h"
    class_label = CLASS_NAMES.get(t.car_class, f"Class {t.car_class}")
    gear_label = "R" if t.gear == 0 else ("N" if t.gear == 11 else str(t.gear))

    car_name = car_names.get(str(t.car_ordinal))
    if car_name is None:
        log_unknown_car(t.car_ordinal, t.car_class, t.car_pi, unknown_seen)
        car_name = f"Unknown car #{t.car_ordinal}"

    details = f"{car_name}  •  {class_label} {t.car_pi}"

    # "In a race" heuristic: a race is on AND we've completed at least one lap
    # OR best_lap is set. Otherwise treat as free roam.
    in_race = t.is_race_on and (t.lap_number > 0 or t.best_lap > 0 or t.race_position > 0)

    if not t.is_race_on:
        state = "In menus / paused"
    elif in_race:
        # Show race position + current lap timer.
        bits = []
        if t.race_position > 0:
            bits.append(f"P{t.race_position}")
        if t.lap_number > 0:
            bits.append(f"Lap {t.lap_number}")
        bits.append(f"{t.speed_display} {unit}")
        state = "  •  ".join(bits)
    else:
        # Free roam: live speed/gear/HP.
        hp_part = f"  •  {t.power_hp} HP" if t.power_hp > 0 else ""
        state = f"{t.speed_display} {unit}  •  Gear {gear_label}{hp_part}"

    small_text_bits = []
    if t.best_lap > 0:
        small_text_bits.append(f"Best lap {format_lap(t.best_lap)}")
    if t.last_lap > 0 and not t.best_lap:
        small_text_bits.append(f"Last lap {format_lap(t.last_lap)}")
    if t.distance_km > 0.5 and not in_race:
        small_text_bits.append(f"{t.distance_km:.1f} km this session")

    payload = {
        "details": details[:128],
        "state": state[:128],
        "start": int(session_start),
        "large_image": "forza",
        "large_text": f"Forza Horizon 6  •  {int(t.rpm)} RPM",
        "small_image": class_asset_key(t.car_class),
        "small_text": "  •  ".join(small_text_bits) if small_text_bits else class_label,
    }
    return payload


def main() -> int:
    car_names = load_car_names()
    print(f"Loaded {len(car_names)} car names from {CAR_NAMES_FILE.name}")
    unknown_seen: set[int] = set()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((LISTEN_HOST, LISTEN_PORT))
    sock.settimeout(1.0)
    print(f"Listening for Forza telemetry on {LISTEN_HOST}:{LISTEN_PORT}")

    rpc: Presence | None = None
    session_start = time.time()
    last_update = 0.0
    last_packet = 0.0
    last_reload = time.time()
    latest: Telemetry | None = None

    def ensure_rpc() -> Presence | None:
        nonlocal rpc
        if rpc is not None:
            return rpc
        try:
            candidate = Presence(DISCORD_CLIENT_ID)
            candidate.connect()
        except Exception as exc:
            print(f"Discord not reachable yet ({type(exc).__name__}); will retry.")
            return None
        rpc = candidate
        print("Connected to Discord.")
        return rpc

    try:
        while True:
            try:
                data, _ = sock.recvfrom(2048)
                parsed = parse_horizon_packet(data)
                if parsed is not None:
                    latest = parsed
                    last_packet = time.time()
            except socket.timeout:
                pass

            now = time.time()

            # Hot-reload car_names.json every 30s so you can edit it while running.
            if now - last_reload > 30:
                car_names = load_car_names()
                last_reload = now

            if now - last_update < UPDATE_INTERVAL:
                continue
            last_update = now

            presence = ensure_rpc()
            if presence is None:
                continue

            try:
                if latest is None or now - last_packet > IDLE_TIMEOUT:
                    presence.update(
                        details="Idle",
                        state="Waiting for telemetry",
                        large_image="forza",
                        large_text="Forza Horizon 6",
                        start=int(session_start),
                    )
                else:
                    presence.update(**build_presence(latest, session_start, car_names, unknown_seen))
            except PipeClosed:
                print("Discord pipe closed, reconnecting...")
                rpc = None
            except Exception as exc:
                print(f"Presence update failed ({type(exc).__name__}: {exc}); reconnecting.")
                rpc = None
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        sock.close()
        if rpc is not None:
            try:
                rpc.clear()
                rpc.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
