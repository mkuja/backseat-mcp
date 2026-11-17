"""Wire codec for the Backseat Champions Game API (UDP, little-endian, packed, fixed-size packets).

Written from the field tables in the game's ``GameAPI/README.md``. Standard library only.

* :func:`peek_header` checks a datagram's length and format and reads only the 24-byte header, which is all the
  telemetry listener needs to file a packet in its cache.
* :func:`decode` returns ``(header, body)`` as plain dicts and lists: enum values are turned into their names and
  flag bytes into booleans.
* ``encode_*`` builds one command datagram each.

Nothing in here talks to a socket.
"""
import math
import struct

PACKET_FORMAT = 1
COMMAND_VERSION = 1
MAX_CARS = 16
MAX_HAND = 4
MAX_LINE_POINTS = 20
MAX_OBSERVATIONS = 339
NONE_U8 = 255            # "no such player" in seat_car, "whole session" in an event's seat, "no limit" in picks left
HEADER_SIZE = 24
COMMAND_HEADER_SIZE = 56
TOKEN_MIN_BYTES = 12
TOKEN_MAX_BYTES = 47
HOLD_TIMEOUT_S = 0.25    # drive and boost are let go after this long without a command

# ---------------------------------------------------------------------------------------------------- packets

PKT_MOTION = 0
PKT_CAR_TELEMETRY = 1
PKT_LAP_DATA = 2
PKT_CAR_STATUS = 3
PKT_PLAYER = 4
PKT_SESSION = 5
PKT_PARTICIPANTS = 6
PKT_EVENT = 7
PKT_AI_OBSERVATIONS = 8
PKT_ACK = 9
PKT_ERROR = 10
PKT_HELLO = 11
PKT_CAR_ABILITIES = 12
PKT_RACING_LINE = 13

# id -> (name, total datagram bytes, layout version). The README's packet table lists AI Observations as 1,428
# bytes, but its own field list adds up to 24 + 1,408 = 1,432, which is also what the game sends.
PACKETS = {
    PKT_MOTION: ("motion", 792, 1),
    PKT_CAR_TELEMETRY: ("car_telemetry", 952, 1),
    PKT_LAP_DATA: ("lap_data", 312, 1),
    PKT_CAR_STATUS: ("car_status", 536, 1),
    PKT_PLAYER: ("player", 1039, 1),
    PKT_SESSION: ("session", 123, 1),
    PKT_PARTICIPANTS: ("participants", 584, 1),
    PKT_EVENT: ("event", 47, 1),
    PKT_AI_OBSERVATIONS: ("ai_observations", 1432, 2),
    PKT_ACK: ("ack", 29, 1),
    PKT_ERROR: ("error", 30, 1),
    PKT_HELLO: ("hello", 53, 1),
    PKT_CAR_ABILITIES: ("car_abilities", 200, 1),
    PKT_RACING_LINE: ("racing_line", 747, 1),
}
PACKET_NAMES = {pid: spec[0] for pid, spec in PACKETS.items()}
PACKET_IDS = {spec[0]: pid for pid, spec in PACKETS.items()}
PACKET_SIZES = {pid: spec[1] for pid, spec in PACKETS.items()}
REPLY_PACKETS = (PKT_ACK, PKT_ERROR, PKT_HELLO)
TELEMETRY_PACKETS = tuple(pid for pid in PACKETS if pid not in REPLY_PACKETS)

# ---------------------------------------------------------------------------------------------------- commands

CMD_PING = 0
CMD_HELLO = 1
CMD_PICK_CARD = 2
CMD_SET_PIT = 3
CMD_PICK_TIRE = 4
CMD_DRIVE = 5
CMD_RELEASE_DRIVE = 6
CMD_BAN_CARD = 7
CMD_BOOST = 8
CMD_SET_CAMERA = 9

# id -> (name, total datagram bytes)
COMMANDS = {
    CMD_PING: ("ping", 56),
    CMD_HELLO: ("hello", 56),
    CMD_PICK_CARD: ("pick_card", 58),
    CMD_SET_PIT: ("set_pit", 58),
    CMD_PICK_TIRE: ("pick_tire", 58),
    CMD_DRIVE: ("drive", 65),
    CMD_RELEASE_DRIVE: ("release_drive", 57),
    CMD_BAN_CARD: ("ban_card", 58),
    CMD_BOOST: ("boost", 58),
    CMD_SET_CAMERA: ("set_camera", 58),
}
COMMAND_NAMES = {cid: spec[0] for cid, spec in COMMANDS.items()}
COMMAND_SIZES = {cid: spec[1] for cid, spec in COMMANDS.items()}
NO_COMMAND = 255         # Error.command_id when the datagram was too short to hold one

BOOST_SPEED = 1
BOOST_CORNERING = 2
BOOST_PARTY_MODE = 4

CAMERA_PREVIOUS = 253
CAMERA_NEXT = 254
CAMERA_AUTOMATIC = 255
CAMERA_SPECIALS = {"previous": CAMERA_PREVIOUS, "prev": CAMERA_PREVIOUS, "next": CAMERA_NEXT,
                   "automatic": CAMERA_AUTOMATIC, "auto": CAMERA_AUTOMATIC}

# ---------------------------------------------------------------------------------------------------- tables
# "The values are the game's own, and never change meaning; new ones are only ever added at the end."

COMPOUNDS = ("Standard", "Boost", "Experimental", "Fast", "Durable", "Wet", "Golden")
UPGRADES = ("EnginePower", "DragReduction", "TireGrip", "TemporaryUpgradeDuration", "TractionControl", "Downforce",
            "TireSaving", "PitSpeed", "BoostCapacity", "WeightReduction", "BoostPerSector", "BoostRechargeSpeed",
            "ExperienceGain", "SensorQuality", "TurboCharger", "Suspension", "Cooling", "BoostOverspeed",
            "FuelCapacity", "FuelEfficiency", "FuelPerformance", "RefuelSpeed", "EngineBraking", "Gearbox",
            "FuelWeight", "KineticRegen", "Reroll")
FACILITIES = ("Speed", "Cornering", "Performance", "Boost", "Fuel", "All", "Redline", "Augment")
SESSION_TYPES = ("AITraining", "Practice", "Qualifying", "Race")
CAR_TYPES = ("Formula", "ClassicFormula", "ModernFormula", "Endurance", "GT2", "Stock", "FormulaOldskool",
             "FormulaLegacy")
PERFORMANCE_AREAS = ("Suspension", "EngineTorque", "Downforce", "Acceleration", "Drag", "AngularDrag",
                     "TractionControl", "Weight")
CAMERA_TYPES = ("TrackOverview", "Helicopter", "TrackSide", "Drone", "Onboard_TCam", "Onboard_Rear",
                "Onboard_Front", "Onboard_Driver", "Onboard_TVAngle", "Chase", "Onboard_BattleCam")
PARTICIPANT_KINDS = ("ai", "local", "remote")
ERROR_REASONS = ("none", "invalid_packet", "unsupported_version", "unknown_command", "public_lobby",
                 "input_disabled", "not_your_car", "no_player", "replay_playing", "no_points", "invalid_index",
                 "cards_unavailable", "card_banned", "driver_decided", "refused", "invalid_compound",
                 "tire_select_closed", "compound_refused", "not_host", "invalid_axis", "internal_error",
                 "ban_refused", "assist_on", "camera_unavailable")
EVENT_CODES = ("SSTA", "SEND", "CARD", "TIRE", "LAPC", "PITW", "PITI", "PITO", "FINI")


class PacketError(ValueError):
    """The datagram is not a well-formed Game API packet."""


def enum_name(table, value):
    """The table's name for a value; ``unknown_<n>`` for one a newer game added past the end of the table."""
    return table[value] if 0 <= value < len(table) else "unknown_%d" % value


def _lookup(table, value, what, specials=None):
    """Index for a name (any case, ``_``/``-``/space ignored) or a number given as int or digit string."""
    if isinstance(value, bool):
        raise ValueError("%s must be a name or an index, not a boolean" % what)
    if isinstance(value, int):
        index = value
    else:
        text = str(value).strip()
        if text.isdigit():
            index = int(text)
        else:
            key = text.lower().replace("_", "").replace("-", "").replace(" ", "")
            if specials and key in specials:
                return specials[key]
            for i, name in enumerate(table):
                if name.lower().replace("_", "") == key:
                    return i
            names = ", ".join(table) + ("" if not specials else ", " + ", ".join(sorted(set(specials))))
            raise ValueError("unknown %s %r; one of: %s" % (what, value, names))
    if not (0 <= index < len(table)) and not (specials and index in specials.values()):
        raise ValueError("%s index %d is out of range 0..%d" % (what, index, len(table) - 1))
    return index


def compound_index(value):
    """Compound index 0..6 for a name such as ``"Fast"`` (any case) or an index."""
    return _lookup(COMPOUNDS, value, "tire compound")


def camera_index(value):
    """Camera index 0..10 for a name such as ``"Chase"`` or an index; ``next``/``previous``/``automatic`` give
    254/253/255."""
    return _lookup(CAMERA_TYPES, value, "camera", CAMERA_SPECIALS)


# ---------------------------------------------------------------------------------------------------- decoding

_HEADER = struct.Struct("<H B B 3B B I I f 4B")
assert _HEADER.size == HEADER_SIZE


class _Cursor:
    """Reads packed little-endian fields one after another from a buffer."""

    __slots__ = ("buf", "pos")

    def __init__(self, buf, pos=0):
        self.buf = buf
        self.pos = pos

    def u8(self):
        value = self.buf[self.pos]
        self.pos += 1
        return value

    def flag(self):
        return self.u8() != 0

    def u16(self):
        value = struct.unpack_from("<H", self.buf, self.pos)[0]
        self.pos += 2
        return value

    def u32(self):
        value = struct.unpack_from("<I", self.buf, self.pos)[0]
        self.pos += 4
        return value

    def f32(self):
        value = struct.unpack_from("<f", self.buf, self.pos)[0]
        self.pos += 4
        return value

    def floats(self, count):
        values = struct.unpack_from("<%df" % count, self.buf, self.pos)
        self.pos += 4 * count
        return list(values)

    def raw(self, count):
        value = bytes(self.buf[self.pos:self.pos + count])
        self.pos += count
        return value

    def text(self, count):
        return self.raw(count).split(b"\0", 1)[0].decode("utf-8", "replace")

    def skip_to(self, pos):
        self.pos = pos


def _seat(value):
    return None if value == NONE_U8 else value


def _header_dict(fields):
    pid = fields[1]
    return {
        "packet_format": fields[0],
        "packet_id": pid,
        "packet": PACKET_NAMES[pid],
        "packet_version": fields[2],
        "game_version": "%d.%d.%d" % (fields[3], fields[4], fields[5]),
        "num_cars": fields[6],
        "session_uid": fields[7],
        "frame": fields[8],
        "session_time": fields[9],
        "seat_car": [_seat(c) for c in fields[10:14]],
    }


def peek_header(data):
    """Validate a datagram (length, format, known id, fixed size) and return its header dict without the body.

    Raises :class:`PacketError` for anything else; a listening UDP socket can receive anything.
    """
    size = len(data)
    if size < HEADER_SIZE:
        raise PacketError("%d bytes is shorter than the 24-byte header" % size)
    fields = _HEADER.unpack_from(data)
    if fields[0] != PACKET_FORMAT:
        raise PacketError("packet_format %d, this codec reads format %d" % (fields[0], PACKET_FORMAT))
    spec = PACKETS.get(fields[1])
    if spec is None:
        raise PacketError("unknown packet id %d" % fields[1])
    if size != spec[1]:
        raise PacketError("%s is %d bytes, expected %d" % (spec[0], size, spec[1]))
    return _header_dict(fields)


def _per_car(body, num_cars, stride, read):
    cars = []
    for i in range(min(num_cars, MAX_CARS)):
        cars.append(read(_Cursor(body, i * stride)))
    return cars


def _motion_car(c):
    return {"position": c.floats(3), "velocity": c.floats(3), "angular_velocity": c.floats(3),
            "pitch": c.f32(), "roll": c.f32(), "yaw": c.f32()}


def _wheel(c):
    flags = c.u8()
    return {"on_track": bool(flags & 1), "grounded": bool(flags & 2),
            "slip_forward": c.f32(), "slip_sideways": c.f32()}


def _telemetry_car(c):
    out = {"speed_kmh": c.f32(), "pedal": c.f32(), "steer": c.f32()}
    out["wheels"] = [_wheel(c) for _ in range(4)]
    out["tire_compound"] = enum_name(COMPOUNDS, c.u8())
    out["puncture"] = bool(c.u8() & 1)
    out["tire_health"] = c.f32()
    out["energy"] = c.f32()
    return out


def _lap_car(c):
    out = {"position": c.u8(), "class_position": c.u8(), "lap": c.u16()}
    flags = c.u8()
    out.update(in_pits=bool(flags & 1), finished=bool(flags & 2), pit_planned=bool(flags & 4),
               api_driving=bool(flags & 8))
    out["pit_stops"] = c.u8()
    out["gap_to_leader"] = c.f32()
    out["interval_ahead"] = c.f32()
    out["interval_behind"] = c.f32()
    return out


def _status_car(c):
    return {"performance": dict(zip(PERFORMANCE_AREAS, c.floats(len(PERFORMANCE_AREAS))))}


def _hand(c):
    """Hand (17 bytes): count, then four 4-byte cards. Only the first ``count`` cards are returned."""
    count = min(c.u8(), MAX_HAND)
    cards = []
    for index in range(MAX_HAND):
        upgrade, facility, tier, flags = c.u8(), c.u8(), c.u8(), c.u8()
        if index < count:
            cards.append({"index": index, "upgrade": enum_name(UPGRADES, upgrade),
                          "facility": enum_name(FACILITIES, facility), "tier": tier, "banned": bool(flags & 1)})
    return cards


def _picks_left(values):
    """Per compound name: picks left, or None for no limit (255 on the wire)."""
    return {name: (None if values[i] == NONE_U8 else values[i]) for i, name in enumerate(COMPOUNDS)}


def _allowed(mask):
    return [name for i, name in enumerate(COMPOUNDS) if mask & (1 << i)]


def _motion(body, num_cars):
    return {"cars": _per_car(body, num_cars, 48, _motion_car)}


def _car_telemetry(body, num_cars):
    return {"cars": _per_car(body, num_cars, 58, _telemetry_car)}


def _lap_data(body, num_cars):
    return {"cars": _per_car(body, num_cars, 18, _lap_car)}


def _car_status(body, num_cars):
    return {"cars": _per_car(body, num_cars, 32, _status_car)}


def _player(body, num_cars):
    c = _Cursor(body)
    out = {"seat": c.u8(), "car": c.u8(), "points": c.u8(), "hand": _hand(c), "upgrades_applied": c.u16(),
           "total_laps": c.u16(), "laps_remaining": c.u16()}
    pit = c.u8()
    out.update(pit_planned=bool(pit & 1), can_pit=bool(pit & 2), in_pits=bool(pit & 4))
    out["pit_stops"] = c.u8()
    out["tire_compound"] = enum_name(COMPOUNDS, c.u8())
    out["puncture"] = bool(c.u8() & 1)
    out["tire_health"] = c.f32()
    out["compound_picks_left"] = _picks_left(c.raw(len(COMPOUNDS)))
    out["energy"] = c.f32()
    if c.flag():
        strategy = {"start_grid_position": c.u8(), "laps_since_pit": c.u16(), "tire_age_laps": c.u16(),
                    "cars_within_5s_ahead": c.u8(), "cars_within_5s_behind": c.u8(), "class_position": c.u8(),
                    "class_size": c.u8(), "class_gap_to_leader": c.f32(), "class_gap_to_ahead": c.f32(),
                    "class_gap_to_behind": c.f32(), "reference_lap_s": c.f32()}
        strategy["opponents"] = c.floats(194)
        strategy["near"] = c.floats(42)
        out["strategy"] = strategy
    else:
        out["strategy"] = None
    return out


def _session(body, num_cars):
    c = _Cursor(body)
    out = {"session_type": enum_name(SESSION_TYPES, c.u8()), "energy": "fuel" if c.flag() else "boost",
           "total_laps": c.u16()}
    flags = c.u8()
    out.update(player_input=bool(flags & 1), driver_input=bool(flags & 2), public_lobby=bool(flags & 4))
    out["weather"] = c.f32()
    out["feature_schema_version"] = c.u16()
    out["feature_schema_hash"] = "0x%08X" % c.u32()
    out["scene"] = c.text(32)
    if c.flag():
        out["track"] = {
            "length_km": c.f32(), "corner_count": c.u16(), "avg_corner_speed": c.f32(), "straight_ratio": c.f32(),
            "elevation_change": c.f32(), "base_tire_wear_rate": c.f32(), "overtake_difficulty": c.f32(),
            "pit_lane_time_loss_s": c.f32(), "width_mean": c.f32(), "width_min": c.f32(), "barrier_ratio": c.f32(),
            "drs_zone_count": c.u8(), "drs_coverage": c.f32(), "lap_time_est_s": c.f32()}
    else:
        out["track"] = None
    return out


def _participant(c):
    return {"player_id": c.u8(), "car_type": enum_name(CAR_TYPES, c.u8()),
            "kind": enum_name(PARTICIPANT_KINDS, c.u8()), "name": c.text(32)}


def _participants(body, num_cars):
    return {"cars": _per_car(body, num_cars, 35, _participant)}


def _event(body, num_cars):
    c = _Cursor(body)
    code = c.raw(4).decode("ascii", "replace")
    out = {"code": code, "seat": _seat(c.u8())}
    if code == "CARD":
        out["points"] = c.u8()
        out["hand"] = _hand(c)
    elif code == "TIRE":
        out["pre_race"] = c.flag()
        out["current"] = enum_name(COMPOUNDS, c.u8())
        out["allowed"] = _allowed(c.u8())
        out["picks_left"] = _picks_left(c.raw(len(COMPOUNDS)))
    elif code == "LAPC":
        out["lap"] = c.u16()
        out["position"] = c.u8()
    elif code == "PITW":
        out["lap"] = c.u16()
        out["pit_planned"] = c.flag()
        out["can_pit"] = c.flag()
    elif code == "FINI":
        out["position"] = c.u8()
    elif code not in EVENT_CODES:
        out["details_hex"] = c.raw(18).hex()     # a code newer than this server: kept, not interpreted
    return out


def _ai_observations(body, num_cars):
    c = _Cursor(body)
    out = {"car": c.u8(), "api_driving": bool(c.u8() & 1), "policy": c.text(13), "policy_name": c.text(23),
           "obs_frame": c.u32(), "action": c.floats(2)}
    count = min(c.u16(), MAX_OBSERVATIONS)
    out["obs"] = c.floats(MAX_OBSERVATIONS)[:count]
    return out


def _ack(body, num_cars):
    c = _Cursor(body)
    return {"command": _command_name(c.u8()), "request_id": c.u32()}


def _error(body, num_cars):
    c = _Cursor(body)
    out = {"command": _command_name(c.u8()), "request_id": c.u32()}
    code = c.u8()
    out["reason"] = enum_name(ERROR_REASONS, code)
    out["reason_code"] = code
    return out


def _hello(body, num_cars):
    c = _Cursor(body)
    out = {"request_id": c.u32(), "game_version": c.text(16)}
    api, broadcast = c.u8(), c.u8()
    out.update(player_input=bool(api & 1), driver_input=bool(api & 2), public_lobby=bool(api & 4),
               driver_data=bool(broadcast & 1), player_data=bool(broadcast & 2),
               decision_events=bool(broadcast & 4), ai_observations=bool(broadcast & 8))
    out["rate_hz"] = c.u8()
    out["feature_schema_version"] = c.u16()
    out["feature_schema_hash"] = "0x%08X" % c.u32()
    return out


def _abilities_car(c):
    active, unlocked = c.u8(), c.u8()
    return {"speed_boost": bool(active & 1), "cornering_boost": bool(active & 2), "super_boost": bool(active & 4),
            "party_mode": bool(active & 8), "drs": bool(active & 16), "boost_overspeed": bool(active & 32),
            "recharging": bool(active & 64), "super_boost_unlocked": bool(unlocked & 1),
            "party_mode_unlocked": bool(unlocked & 2), "drs_unlocked": bool(unlocked & 4),
            "api_boosting": bool(unlocked & 8), "ban_tokens": c.u8(), "slipstream": c.f32(),
            "recharge_left": c.f32()}


def _car_abilities(body, num_cars):
    return {"cars": _per_car(body, num_cars, 11, _abilities_car)}


def _racing_line(body, num_cars):
    c = _Cursor(body)
    out = {"seat": c.u8(), "car": c.u8()}
    count = min(c.u8(), MAX_LINE_POINTS)
    out["points"] = [{"racing_line": c.floats(3), "left": c.floats(3), "right": c.floats(3)} for _ in range(count)]
    return out


def _command_name(command_id):
    if command_id == NO_COMMAND:
        return None
    return COMMAND_NAMES.get(command_id, "unknown_%d" % command_id)


_BODY_DECODERS = {
    PKT_MOTION: _motion, PKT_CAR_TELEMETRY: _car_telemetry, PKT_LAP_DATA: _lap_data, PKT_CAR_STATUS: _car_status,
    PKT_PLAYER: _player, PKT_SESSION: _session, PKT_PARTICIPANTS: _participants, PKT_EVENT: _event,
    PKT_AI_OBSERVATIONS: _ai_observations, PKT_ACK: _ack, PKT_ERROR: _error, PKT_HELLO: _hello,
    PKT_CAR_ABILITIES: _car_abilities, PKT_RACING_LINE: _racing_line,
}


def decode_body(data, header):
    """The body dict of a datagram whose header :func:`peek_header` already returned."""
    return _BODY_DECODERS[header["packet_id"]](memoryview(data)[HEADER_SIZE:], header["num_cars"])


def decode(data):
    """``(header, body)`` for one datagram, both plain dicts. Raises :class:`PacketError` if it is not a packet."""
    header = peek_header(data)
    return header, decode_body(data, header)


def peek_keys(data):
    """The cache key bytes of the packets that are sent once per seat or per car.

    Player and Racing Line start their body with ``seat``; AI Observations with ``car``.
    """
    return data[HEADER_SIZE]


# ---------------------------------------------------------------------------------------------------- encoding

_COMMAND_HEADER = struct.Struct("<H B B I 48s")
assert _COMMAND_HEADER.size == COMMAND_HEADER_SIZE


def check_token(token):
    """The token as UTF-8 bytes. Empty is fine (loopback needs none); otherwise 12..47 bytes, as the game requires."""
    raw = (token or "").encode("utf-8")
    if raw and not (TOKEN_MIN_BYTES <= len(raw) <= TOKEN_MAX_BYTES):
        raise ValueError("the command token must be %d to %d UTF-8 bytes, got %d"
                         % (TOKEN_MIN_BYTES, TOKEN_MAX_BYTES, len(raw)))
    if b"\0" in raw:
        raise ValueError("the command token must not contain a zero byte")
    return raw


def encode_command(command_id, payload=b"", request_id=0, token=""):
    """One command datagram: the 56-byte header (format, id, version, request id, token) and its payload."""
    raw = token if isinstance(token, bytes) else (token or "").encode("utf-8")
    if len(raw) > TOKEN_MAX_BYTES:
        raise ValueError("the command token is at most %d bytes" % TOKEN_MAX_BYTES)
    data = _COMMAND_HEADER.pack(PACKET_FORMAT, command_id, COMMAND_VERSION, request_id & 0xFFFFFFFF, raw) + payload
    expected = COMMAND_SIZES.get(command_id)
    if expected is not None and len(data) != expected:
        raise ValueError("%s would be %d bytes, the game expects %d" % (COMMAND_NAMES[command_id], len(data), expected))
    return data


def encode_ping(request_id=0, token=""):
    return encode_command(CMD_PING, b"", request_id, token)


def encode_hello(request_id=0, token=""):
    return encode_command(CMD_HELLO, b"", request_id, token)


def encode_pick_card(index, seat=0, request_id=0, token=""):
    return encode_command(CMD_PICK_CARD, struct.pack("<BB", seat, index), request_id, token)


def encode_set_pit(pit, seat=0, request_id=0, token=""):
    return encode_command(CMD_SET_PIT, struct.pack("<BB", seat, 1 if pit else 0), request_id, token)


def encode_pick_tire(compound, seat=0, request_id=0, token=""):
    return encode_command(CMD_PICK_TIRE, struct.pack("<BB", seat, compound_index(compound)), request_id, token)


def encode_drive(pedal, steer, seat=0, request_id=0, token=""):
    if not (math.isfinite(pedal) and math.isfinite(steer)):
        raise ValueError("pedal and steer must be finite numbers")
    return encode_command(CMD_DRIVE, struct.pack("<Bff", seat, pedal, steer), request_id, token)


def encode_release_drive(seat=0, request_id=0, token=""):
    return encode_command(CMD_RELEASE_DRIVE, struct.pack("<B", seat), request_id, token)


def encode_ban_card(index, seat=0, request_id=0, token=""):
    return encode_command(CMD_BAN_CARD, struct.pack("<BB", seat, index), request_id, token)


def boost_buttons(speed=False, cornering=False, party_mode=False):
    """The ``buttons`` bit mask: bit 0 speed boost, bit 1 cornering boost, bit 2 party mode."""
    return ((BOOST_SPEED if speed else 0) | (BOOST_CORNERING if cornering else 0)
            | (BOOST_PARTY_MODE if party_mode else 0))


def encode_boost(buttons, seat=0, request_id=0, token=""):
    return encode_command(CMD_BOOST, struct.pack("<BB", seat, buttons & 0xFF), request_id, token)


def encode_set_camera(camera, seat=0, request_id=0, token=""):
    return encode_command(CMD_SET_CAMERA, struct.pack("<BB", seat, camera_index(camera)), request_id, token)
