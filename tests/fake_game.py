"""A stand-in for Backseat Champions' Game API, for tests and for trying the MCP server without the game.

It replays the captured packets in ``tests/data`` to a telemetry address at a fixed rate (with a consistent
header: one session uid, a rising frame number), listens for commands on its own UDP port, checks each command's
size and answers with Ack / Error / Hello in the real layouts. A few rules of the real game are modelled so the
server's logic can be exercised: a hand of cards and points, ban tokens, the tire screen, input switches, the
public lobby rule, the seat rule and the command token. It does not simulate a race, but a scripted *scenario*
can mutate the player's car in the replayed packets: laps advancing at a fixed lap time (with LAPC / PITW events),
tire health falling per lap, a puncture flag, a compound, the pit lane flag.

Behaviour seen in the real game (0.88.2) that the server must cope with is modelled too:
- pause() / resume(): session_time stops; only Session, Car Status (2 Hz) and Participants keep arriving.
  auto_pause_on_hand pauses while points > 0 and a hand is on offer (the game pauses while a hand waits).
- reorder(perm): the game re-orders car indices by race position mid-session: every per-car packet (Motion, Car
  Telemetry, Lap Data, Car Status, Car Abilities, Participants) is permuted, seat_car[0] follows my car, and
  positions are rewritten to index + 1. Participants still only comes every 5 s.
- Lap counting as observed (8-lap race at China): ``self.lap`` here is laps COMPLETED, but every packet carries
  the lap being driven: Lap Data ``lap`` = completed + 1 (the game README says "laps completed", wrong in 0.88.2;
  it reads total + 1 after the flag), the LAPC event's ``lap`` = completed + 1 (1 at the start, total + 1 at the
  flag) and Player ``laps_remaining`` = laps after the current one (total - completed - 1, 0 on the final lap).
- pit_call_drop_window_s: a set_pit(true) acknowledged less than this many seconds before a scenario lap boundary
  is dropped at the boundary (pit_planned false afterwards), as seen once in 0.88.2. game_plans_pit() plans a stop
  without any API call (the game's own strategy; can_pit false), also seen in 0.88.2.

Manual use (then point the MCP server at the same ports, which are the game's defaults):

    python tests/fake_game.py                      # telemetry to 127.0.0.1:29770, commands on 127.0.0.1:29771
    python tests/fake_game.py --events             # also fire a CARD / PITW / TIRE event every few seconds
    python tests/fake_game.py --scenario           # laps every 20 s, tires wearing 0.2 per lap, a puncture later

The layouts are packed here with ``struct`` straight from the README, on purpose not with the package's codec, so
the tests compare two independent implementations.
"""
import argparse
import os
import socket
import struct
import threading
import time

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HEADER = struct.Struct("<HBB3BBIIf4B")
COMMAND_HEADER = struct.Struct("<HBBI48s")
COMMAND_SIZES = {0: 56, 1: 56, 2: 58, 3: 58, 4: 58, 5: 65, 6: 57, 7: 58, 8: 58, 9: 58}
TELEMETRY = ["motion", "car_telemetry", "lap_data", "car_status", "player", "session", "participants",
             "ai_observations", "car_abilities", "racing_line"]
PACKET_IDS = {"motion": 0, "car_telemetry": 1, "lap_data": 2, "car_status": 3, "player": 4, "session": 5,
              "participants": 6, "event": 7, "ai_observations": 8, "ack": 9, "error": 10, "hello": 11,
              "car_abilities": 12, "racing_line": 13}
PACKET_VERSIONS = {8: 2}
SLOW = {"car_status": 0.5, "session": 0.5, "participants": 5.0}      # seconds between sends; the rest every tick
E_INVALID_PACKET, E_UNSUPPORTED_VERSION, E_UNKNOWN_COMMAND, E_PUBLIC_LOBBY, E_INPUT_DISABLED = 1, 2, 3, 4, 5
E_NOT_YOUR_CAR, E_NO_PLAYER, E_NO_POINTS, E_INVALID_INDEX, E_CARDS_UNAVAILABLE, E_CARD_BANNED = 6, 7, 9, 10, 11, 12
E_REFUSED, E_INVALID_COMPOUND, E_TIRE_SELECT_CLOSED, E_COMPOUND_REFUSED, E_NOT_HOST = 14, 15, 16, 17, 18
E_INVALID_AXIS, E_BAN_REFUSED, E_ASSIST_ON, E_CAMERA_UNAVAILABLE = 19, 21, 22, 23

PLAYER_POINTS = HEADER.size + 2          # offsets into the Player datagram
PLAYER_HAND = HEADER.size + 3
PLAYER_PIT_FLAGS = HEADER.size + 26
PLAYER_TOTAL_LAPS = HEADER.size + 22     # uint16 total_laps, then uint16 laps_remaining
PLAYER_COMPOUND = HEADER.size + 28       # uint8 compound, uint8 tire_flags, float tire_health
ABILITIES_CAR = 11
TELEMETRY_CAR = 58                       # compound at +48, tire_flags +49, tire_health +50
LAP_CAR = 18                             # lap uint16 at +2, flags at +4
STATUS_CAR = 32
PER_CAR = {"motion": 48, "car_telemetry": TELEMETRY_CAR, "lap_data": LAP_CAR, "car_status": STATUS_CAR,
           "car_abilities": ABILITIES_CAR, "participants": 35}
PAUSE_KEEPS = ("car_status", "session", "participants")     # still sent while the game is paused


class FakeGame:
    def __init__(self, telemetry_host="127.0.0.1", telemetry_port=29770, command_host="127.0.0.1", command_port=0,
                 rate_hz=20, token="", session_uid=424242, my_car=10):
        self.telemetry_target = (telemetry_host, telemetry_port)
        self.rate_hz = rate_hz
        self.token = token.encode("utf-8")
        self.session_uid = session_uid
        self.my_car = my_car
        self.my_slot = my_car            # template slot of my car (reorder() moves its index)
        self.perm = list(range(16))      # car index -> template slot
        self.frame = 0
        self.started = time.monotonic()
        self.paused_at = None            # monotonic time the game paused (session_time frozen)
        self.paused_total = 0.0
        self.auto_pause_on_hand = False
        self.pit_call_drop_window_s = 0.0   # see the module docstring
        self._last_pit_call = None       # monotonic time of the last accepted set_pit(true)
        self.templates = {}
        for name in TELEMETRY + ["event"]:
            with open(os.path.join(DATA, name + ".bin"), "rb") as f:
                self.templates[name] = bytearray(f.read())

        # what the game's settings and state would be
        self.player_input = True
        self.driver_input = True
        self.public_lobby = False
        self.broadcast = {"driver_data": True, "player_data": True, "decision_events": True,
                          "ai_observations": True}
        self.host = True                 # this machine simulates the cars
        self.assist_on = False
        self.in_session = True
        self.sending = True              # telemetry on/off without touching the switches
        self.points = 2
        self.hand = [[13, 6, 0, 0], [0, 0, 0, 0], [11, 3, 0, 0]]      # upgrade, facility, tier, flags
        self.ban_tokens = 1
        self.pit_planned = False
        self.can_pit = True
        self.tire_screen = False
        self.allowed_compounds = 0b0101001      # Standard, Fast, Wet
        self.force_error = {}            # command id -> error reason to answer with
        self.silent = False              # drop every command without answering (like a wrong token)

        # scenario overrides for the player's car; None keeps the captured value
        self.total_laps = None
        self.lap = None                  # laps COMPLETED (packets carry the lap being driven, completed + 1)
        self.tire_health = None
        self.puncture = None
        self.compound = None             # compound index
        self.in_pits = None
        self.perf_scale = {}             # car index -> factor applied to all 8 performance values
        self.scenario = None             # dict(lap_s, wear_per_lap, start_health, started) while running

        self.commands = []               # (monotonic time, command id, payload bytes, request id)
        self.lock = threading.Lock()
        self.tele_sock = socket.socket(socket.AF_INET6 if ":" in telemetry_host else socket.AF_INET,
                                       socket.SOCK_DGRAM)
        self.cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cmd_sock.bind((command_host, command_port))
        self.cmd_sock.settimeout(0.1)
        self.command_port = self.cmd_sock.getsockname()[1]
        self.running = False
        self.threads = []

    # ---------------------------------------------------------------- lifecycle

    def start(self):
        self.running = True
        for target in (self._telemetry_loop, self._command_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    def stop(self):
        self.running = False
        for thread in self.threads:
            thread.join(timeout=2)
        self.tele_sock.close()
        self.cmd_sock.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ---------------------------------------------------------------- telemetry

    def header(self, packet_id):
        seats = (self.my_car, 255, 255, 255) if self.in_session else (255, 255, 255, 255)
        return HEADER.pack(1, packet_id, PACKET_VERSIONS.get(packet_id, 1), 0, 86, 2, 16 if self.in_session else 0,
                           self.session_uid if self.in_session else 0, self.frame,
                           self.session_time(), *seats)

    def session_time(self):
        now = self.paused_at if self.paused_at is not None else time.monotonic()
        return now - self.started - self.paused_total

    def pause(self):
        """The game pauses (it does while a card hand waits): session_time stops, rate packets stop."""
        with self.lock:
            if self.paused_at is None:
                self.paused_at = time.monotonic()

    def resume(self):
        with self.lock:
            if self.paused_at is not None:
                self.paused_total += time.monotonic() - self.paused_at
                self.paused_at = None

    @property
    def paused(self):
        return self.paused_at is not None

    def reorder(self, perm):
        """Re-order car indices: new index i shows the car that was at index perm[i]. My car's index follows."""
        assert sorted(perm) == list(range(16))
        with self.lock:
            self.perm = [self.perm[j] for j in perm]
            self.my_car = self.perm.index(self.my_slot)

    def truth(self, index):
        """The template Participants entry (name, player_id) of the car at this index now."""
        slot = self.perm[index]
        raw = self.templates["participants"][HEADER.size + slot * 35:HEADER.size + (slot + 1) * 35]
        return {"player_id": raw[0], "name": bytes(raw[3:35]).split(b"\0", 1)[0].decode("utf-8")}

    def _permute(self, name, data):
        size = PER_CAR.get(name)
        if size is None or self.perm == list(range(16)):
            return
        src = bytes(data)
        for i, slot in enumerate(self.perm):
            a = HEADER.size + slot * size
            data[HEADER.size + i * size:HEADER.size + (i + 1) * size] = src[a:a + size]
        if name == "lap_data":
            for i in range(16):
                data[HEADER.size + i * LAP_CAR] = i + 1            # index = position - 1, as seen in 0.88.2

    def hand_bytes(self):
        raw = bytearray(17)
        raw[0] = len(self.hand)
        for i, card in enumerate(self.hand):
            raw[1 + i * 4:5 + i * 4] = bytes(card)
        return bytes(raw)

    def packet(self, name, car=None):
        data = bytearray(self.templates[name])
        pid = PACKET_IDS[name]
        self._permute(name, data)
        data[:HEADER.size] = self.header(pid)
        self.advance_scenario()
        if name == "player":
            data[HEADER.size] = 0
            data[HEADER.size + 1] = self.my_car
            data[PLAYER_POINTS] = self.points
            data[PLAYER_HAND:PLAYER_HAND + 17] = self.hand_bytes()
            data[PLAYER_PIT_FLAGS] = ((1 if self.pit_planned else 0) | (2 if self.can_pit else 0)
                                      | (4 if self.in_pits else 0))
            if self.total_laps is not None and self.lap is not None:
                # observed 0.88.2: laps_remaining = laps after the current one
                struct.pack_into("<HH", data, PLAYER_TOTAL_LAPS, self.total_laps,
                                 max(0, self.total_laps - self.lap - 1))
            self._patch_tires(data, PLAYER_COMPOUND)
        elif name == "car_telemetry":
            self._patch_tires(data, HEADER.size + self.my_car * TELEMETRY_CAR + 48)
        elif name == "lap_data":
            offset = HEADER.size + self.my_car * LAP_CAR
            if self.lap is not None:
                struct.pack_into("<H", data, offset + 2, self.lap + 1)     # the lap being driven (observed 0.88.2)
            flags = data[offset + 4] & ~(1 | 4)
            data[offset + 4] = flags | (1 if self.in_pits else 0) | (4 if self.pit_planned else 0)
        elif name == "car_status":
            for car, factor in self.perf_scale.items():
                offset = HEADER.size + car * STATUS_CAR
                values = struct.unpack_from("<8f", data, offset)
                struct.pack_into("<8f", data, offset, *[v * factor for v in values])
        elif name == "session":
            data[HEADER.size + 4] = ((1 if self.player_input else 0) | (2 if self.driver_input else 0)
                                     | (4 if self.public_lobby else 0))
        elif name == "car_abilities":
            offset = HEADER.size + self.my_car * ABILITIES_CAR
            data[offset + 2] = self.ban_tokens
            data[offset + 1] = (data[offset + 1] & ~8) | (8 if self.boosting() else 0)
        elif name == "ai_observations" and car is not None:
            data[HEADER.size] = car
        elif name == "racing_line":
            data[HEADER.size + 1] = self.my_car
        return bytes(data)

    def _patch_tires(self, data, offset):
        """compound (uint8), tire_flags (uint8), tire_health (float) at offset."""
        if self.compound is not None:
            data[offset] = self.compound
        if self.puncture is not None:
            data[offset + 1] = (data[offset + 1] & ~1) | (1 if self.puncture else 0)
        if self.tire_health is not None:
            struct.pack_into("<f", data, offset + 2, self.tire_health)

    def start_scenario(self, lap_s=20.0, wear_per_lap=0.2, start_health=0.95, total_laps=24, lap=0):
        """Laps advance every lap_s seconds (LAPC and PITW events at each boundary) and tire health falls
        linearly by wear_per_lap per lap. A pit stop (pit_stop()) restores the tires."""
        with self.lock:
            self.total_laps, self.lap = total_laps, lap
            self.tire_health = start_health
            self.scenario = {"lap_s": lap_s, "wear": wear_per_lap, "health0": start_health,
                             "t0": self.session_time(), "lap0": lap}

    def advance_scenario(self):
        sc = self.scenario
        if sc is None:
            return
        fire = None
        with self.lock:
            elapsed = self.session_time() - sc["t0"]
            lap = sc["lap0"] + int(elapsed / sc["lap_s"])
            if not self.in_pits:
                self.tire_health = max(0.0, sc["health0"] - sc["wear"] * elapsed / sc["lap_s"])
            if lap != self.lap:
                self.lap = lap
                fire = lap
                call = self._last_pit_call
                if (self.pit_planned and call is not None and self.pit_call_drop_window_s > 0
                        and time.monotonic() - call < self.pit_call_drop_window_s):
                    self.pit_planned = False         # the game drops a call made just before the line
        if fire is not None:
            self.lap_event(fire + 1, self.my_car + 1)            # LAPC: the lap now being driven
            self.pit_window_event(fire + 1)

    def pit_stop(self, compound=None, health=1.0):
        """Enter and leave the pit lane at once with fresh tires (PITI, PITO events)."""
        self.in_pits = True
        self.send_event("PITI", 0)
        with self.lock:
            if self.scenario is not None:
                self.scenario["health0"] = health
                self.scenario["t0"] = self.session_time() - (self.lap - self.scenario["lap0"]) * self.scenario["lap_s"]
                self.scenario["lap0"] = self.lap
            self.tire_health = health
            if compound is not None:
                self.compound = compound
            self.pit_planned = False
            self.puncture = False if self.puncture else self.puncture
        self.in_pits = False
        self.send_event("PITO", 0)

    def game_plans_pit(self):
        """The game's own strategy plans a stop (no API call); can_pit goes false."""
        with self.lock:
            self.pit_planned, self.can_pit = True, False

    def boosting(self):
        now = time.monotonic()
        with self.lock:
            return any(c[1] == 8 and c[2][1:2] != b"\0" and now - c[0] < 0.25 for c in self.commands[-20:])

    def wanted(self, name):
        b = self.broadcast
        if name in ("motion", "car_telemetry", "lap_data", "car_status", "participants", "racing_line"):
            return b["driver_data"]
        if name == "player":
            return b["player_data"]
        if name == "car_abilities":
            return b["driver_data"] or b["player_data"]
        if name == "ai_observations":
            return b["ai_observations"] and self.host
        return any(b.values())           # session

    def send_telemetry(self, data):
        try:
            self.tele_sock.sendto(data, self.telemetry_target)
        except OSError:
            pass

    def _telemetry_loop(self):
        last = {}
        period = 1.0 / self.rate_hz
        while self.running:
            if self.auto_pause_on_hand:
                if self.points > 0 and self.hand:
                    self.pause()
                elif self.paused:
                    self.resume()
            if self.in_session and self.sending:
                now = time.monotonic()
                self.frame += 1
                for name in TELEMETRY:
                    if not self.wanted(name):
                        continue
                    if self.paused and name not in PAUSE_KEEPS:
                        continue
                    if name in SLOW and now - last.get(name, -1e9) < SLOW[name]:
                        continue
                    last[name] = now
                    if name == "ai_observations":
                        for car in (0, self.my_car):
                            self.send_telemetry(self.packet(name, car))
                    else:
                        self.send_telemetry(self.packet(name))
            time.sleep(period)

    def send_event(self, code, seat=0, details=b""):
        """Fire an event: a 4-letter code, the seat (255 for the whole session) and up to 18 detail bytes."""
        if not (self.broadcast["decision_events"] and self.in_session):
            return
        body = code.encode("ascii") + bytes([seat]) + details.ljust(18, b"\0")[:18]
        self.send_telemetry(self.header(PACKET_IDS["event"]) + body)

    def card_event(self):
        self.send_event("CARD", 0, bytes([self.points]) + self.hand_bytes())

    def tire_event(self, pre_race=False, current=5, picks_left=(255, 2, 2, 2, 2, 255, 2)):
        """The tire selection screen opens (a TIRE event); pick_tire works until close_tire_screen() / a pick."""
        self.tire_screen = True
        self.send_event("TIRE", 0, bytes([1 if pre_race else 0, current, self.allowed_compounds]) + bytes(picks_left))

    def close_tire_screen(self, leave_pits=True):
        """The tire screen closes without a pick (the car drives out of the pit lane: PITO)."""
        self.tire_screen = False
        if leave_pits:
            self.in_pits = False
            self.send_event("PITO", 0)

    def pit_window_event(self, lap=3):
        self.send_event("PITW", 0, struct.pack("<HBB", lap, 1 if self.pit_planned else 0, 1 if self.can_pit else 0))

    def lap_event(self, lap=3, position=11):
        self.send_event("LAPC", 0, struct.pack("<HB", lap, position))

    def new_session(self):
        self.session_uid += 1
        self.frame = 0
        self.send_event("SSTA", 255)

    def end_session(self):
        self.send_event("SEND", 255)

    def finish_event(self, position=11):
        """FINI: player one took the flag."""
        self.send_event("FINI", 0, bytes([position]))

    # ---------------------------------------------------------------- commands

    def received(self, command_id=None, since=0.0):
        with self.lock:
            return [c for c in self.commands if (command_id is None or c[1] == command_id) and c[0] >= since]

    def _reply(self, addr, data):
        try:
            self.cmd_sock.sendto(data, addr)
        except OSError:
            pass

    def _ack(self, addr, command_id, request_id):
        self._reply(addr, self.header(9) + struct.pack("<BI", command_id, request_id))

    def _error(self, addr, command_id, request_id, reason):
        self._reply(addr, self.header(10) + struct.pack("<BIB", command_id, request_id, reason))

    def _hello(self, addr, request_id):
        api = (1 if self.player_input else 0) | (2 if self.driver_input else 0) | (4 if self.public_lobby else 0)
        b = self.broadcast
        flags = ((1 if b["driver_data"] else 0) | (2 if b["player_data"] else 0)
                 | (4 if b["decision_events"] else 0) | (8 if b["ai_observations"] else 0))
        self._reply(addr, self.header(11) + struct.pack("<I16sBBBHI", request_id, b"0.86.2", api, flags,
                                                         self.rate_hz, 12, 0x40A5F76F))

    def _command_loop(self):
        while self.running:
            try:
                data, addr = self.cmd_sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if not self.running:
                    return
                continue
            self.handle(data, addr)

    def handle(self, data, addr):
        if self.silent:
            return
        if len(data) < COMMAND_HEADER.size:
            return self._error(addr, 255, 0, E_INVALID_PACKET)
        packet_format, command_id, version, request_id, token = COMMAND_HEADER.unpack_from(data)
        if self.token and token.split(b"\0", 1)[0] != self.token:
            return                                   # dropped without an answer, as the game does
        payload = data[COMMAND_HEADER.size:]
        with self.lock:
            self.commands.append((time.monotonic(), command_id, payload, request_id))
        if packet_format != 1 or version != 1:
            return self._error(addr, command_id, request_id, E_UNSUPPORTED_VERSION)
        if command_id not in COMMAND_SIZES:
            return self._error(addr, command_id, request_id, E_UNKNOWN_COMMAND)
        if len(data) != COMMAND_SIZES[command_id]:
            return self._error(addr, command_id, request_id, E_INVALID_PACKET)
        if command_id in self.force_error:
            return self._error(addr, command_id, request_id, self.force_error[command_id])
        if command_id == 0:
            return self._ack(addr, command_id, request_id)
        if command_id == 1:
            return self._hello(addr, request_id)

        if self.public_lobby and command_id != 9:
            return self._error(addr, command_id, request_id, E_PUBLIC_LOBBY)
        if not (self.driver_input if command_id in (5, 6) else self.player_input):
            return self._error(addr, command_id, request_id, E_INPUT_DISABLED)
        if payload[0] != 0:
            return self._error(addr, command_id, request_id, E_NOT_YOUR_CAR)
        if not self.in_session:
            return self._error(addr, command_id, request_id, E_NO_PLAYER)
        reason = getattr(self, "_cmd_%d" % command_id)(payload)
        if reason:
            return self._error(addr, command_id, request_id, reason)
        if command_id not in (5, 8):                 # drive and boost only answer errors
            self._ack(addr, command_id, request_id)

    def _cmd_2(self, payload):                       # pick_card
        index = payload[1]
        if self.points <= 0:
            return E_NO_POINTS
        if not self.hand:
            return E_CARDS_UNAVAILABLE
        if index >= len(self.hand):
            return E_INVALID_INDEX
        if self.hand[index][3] & 1:
            return E_CARD_BANNED
        self.points -= 1
        self.hand = [[2, 1, 0, 0], [7, 2, 0, 0], [15, 1, 0, 0]] if self.points else []
        if self.points:
            self.card_event()

    def _cmd_3(self, payload):                       # set_pit
        if payload[1] and not self.can_pit:
            return E_REFUSED
        self.pit_planned = bool(payload[1])
        self._last_pit_call = time.monotonic() if payload[1] else None

    def _cmd_4(self, payload):                       # pick_tire
        if payload[1] > 6:
            return E_INVALID_COMPOUND
        if not self.tire_screen:
            return E_TIRE_SELECT_CLOSED
        if not self.allowed_compounds & (1 << payload[1]):
            return E_COMPOUND_REFUSED
        self.tire_screen = False

    def _cmd_5(self, payload):                       # drive
        pedal, steer = struct.unpack_from("<ff", payload, 1)
        if not self.host:
            return E_NOT_HOST
        if pedal != pedal or steer != steer or abs(pedal) == float("inf") or abs(steer) == float("inf"):
            return E_INVALID_AXIS

    def _cmd_6(self, payload):                       # release_drive
        return None

    def _cmd_7(self, payload):                       # ban_card
        index = payload[1]
        if index >= len(self.hand):
            return E_INVALID_INDEX
        pickable = [c for c in self.hand if not c[3] & 1]
        if self.ban_tokens <= 0 or len(pickable) <= 1 or self.hand[index][3] & 1:
            return E_BAN_REFUSED
        self.ban_tokens -= 1
        self.hand[index][3] |= 1

    def _cmd_8(self, payload):                       # boost
        if self.assist_on and payload[1]:
            return E_ASSIST_ON

    def _cmd_9(self, payload):                       # set_camera
        if not (1 <= payload[1] <= 10 or payload[1] in (253, 254, 255)):
            return E_CAMERA_UNAVAILABLE


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--telemetry-host", default="127.0.0.1")
    parser.add_argument("--telemetry-port", type=int, default=29770)
    parser.add_argument("--command-host", default="127.0.0.1")
    parser.add_argument("--command-port", type=int, default=29771)
    parser.add_argument("--rate", type=int, default=20, help="telemetry rate in Hz")
    parser.add_argument("--token", default="", help="require this command token")
    parser.add_argument("--events", action="store_true", help="fire CARD, PITW and TIRE events every few seconds")
    parser.add_argument("--scenario", action="store_true",
                        help="laps every --lap-s seconds with tire wear, a CARD offer each lap, a puncture at lap 3")
    parser.add_argument("--lap-s", type=float, default=20.0)
    parser.add_argument("--wear", type=float, default=0.2, help="tire health lost per lap in --scenario")
    args = parser.parse_args()
    game = FakeGame(args.telemetry_host, args.telemetry_port, args.command_host, args.command_port, args.rate,
                    args.token).start()
    if args.scenario:
        game.start_scenario(lap_s=args.lap_s, wear_per_lap=args.wear)
    print("fake game: telemetry -> %s:%d, commands on %s:%d (Ctrl+C to stop)"
          % (args.telemetry_host, args.telemetry_port, args.command_host, game.command_port))
    seen = 0
    step = 0
    try:
        while True:
            time.sleep(1)
            for when, command_id, payload, request_id in game.received()[seen:]:
                if command_id not in (5, 8):
                    print("command %d payload %s request %d" % (command_id, payload.hex(), request_id))
            seen = len(game.received())
            step += 1
            if args.events and step % 6 == 0:
                game.lap_event(step // 6)
                game.pit_window_event(step // 6)
                if game.points == 0:
                    game.points, game.hand = 1, [[8, 3, 0, 0], [7, 2, 0, 0], [15, 1, 0, 0]]
                game.card_event()
            if args.events and step % 20 == 0:
                game.tire_event()
            if args.scenario:
                if game.lap == 3 and game.puncture is None:
                    game.puncture = True
                if game.pit_planned and game.lap is not None and step % 5 == 0:
                    game.pit_stop()
                    game.tire_event()
    except KeyboardInterrupt:
        game.stop()


if __name__ == "__main__":
    main()
