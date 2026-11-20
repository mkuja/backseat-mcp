"""Regression tests for bugs found in the independent review (in-process tool calls, no game needed)."""
import asyncio
import json
import os
import struct

from backseat_champions_mcp import protocol as P
from backseat_champions_mcp.client import Config
from backseat_champions_mcp.server import _result, build_server


def _strict_json(text):
    def refuse(name):
        raise ValueError("non-standard JSON constant %s" % name)
    return json.loads(text, parse_constant=refuse)


def _text(result):
    if isinstance(result, tuple):          # (content, structured) on some SDK versions
        result = result[0]
    if hasattr(result, "content"):
        result = result.content
    return result[0].text


def test_tool_output_is_strict_json_even_with_nan():
    out = _result({"ok": True, "pitch_deg": float("nan"), "cars": [{"x": float("inf")}, 1.5]})
    data = _strict_json(out.content[0].text)
    assert data == {"ok": True, "pitch_deg": None, "cars": [{"x": None}, 1.5]}


def _telemetry(num_cars, seat0):
    head = struct.pack("<HBB3BBIIf4B", 1, P.PKT_CAR_TELEMETRY, 1, 0, 86, 2, num_cars, 7, 1, 1.0, seat0, 255, 255, 255)
    return head + b"\0" * (952 - 24)


def test_my_car_outside_the_packet_is_a_clean_error_not_an_index_error():
    mcp = build_server(Config())
    client = mcp.state.client
    client._on_telemetry(_telemetry(num_cars=2, seat0=5), ("127.0.0.1", 1))   # malformed: seat 0's car 5 of 2
    text = _text(asyncio.run(mcp.call_tool("get_car_telemetry", {})))
    data = _strict_json(text)
    assert data["ok"] is False and data["error_reason"] == "bad_argument"
    client._on_telemetry(_telemetry(num_cars=8, seat0=5), ("127.0.0.1", 1))
    data = _strict_json(_text(asyncio.run(mcp.call_tool("get_car_telemetry", {}))))
    assert data["ok"] is True and data["cars"][0]["car"] == 5


def test_list_race_results_reads_only_the_headers(tmp_path):
    folder = tmp_path / "results"
    folder.mkdir()
    stamp = "20260101-120000-000001"
    (folder / (stamp + ".results.json")).write_text("{ not json")          # never parsed by the listing
    (folder / (stamp + ".headers.json")).write_text(json.dumps({"x-bc-game-version": "0.86.2",
                                                                "x-bc-session-count": "2"}))
    mcp = build_server(Config(results_port=0, results_dir=str(folder)))
    data = _strict_json(_text(asyncio.run(mcp.call_tool("list_race_results", {}))))
    assert data["ok"] is True
    entry = data["results"][0]
    assert entry["id"] == stamp and entry["bytes"] == os.path.getsize(folder / (stamp + ".results.json"))
    assert entry["game_version"] == "0.86.2" and entry["session_count"] == 2 and entry["stored_as"] == "json"
    assert isinstance(entry["session_count"], int) and entry["compressed"] is False and entry["problems"] == []
    assert entry["received_utc"]          # an old local-time id: received time derived from it


# ------------------------------------------------------------------------------------------------ in-process packets

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HEADER = struct.Struct("<HBB3BBIIf4B")
UID = 7


def _template(name):
    with open(os.path.join(DATA, name + ".bin"), "rb") as f:
        return bytearray(f.read())


def pkt(name, frame, seat0=10, seats=None, num_cars=16, st=None, body=None, patch=None):
    """A packet from the captured template with a new header (session uid 7) and optional body changes."""
    data = _template(name)
    fmt, pid, version = struct.unpack_from("<HBB", data)
    seats = seats or (seat0, 255, 255, 255)
    HEADER.pack_into(data, 0, fmt, pid, version, 0, 86, 2, num_cars, UID, frame,
                     frame / 20.0 if st is None else st, *seats)
    if body is not None:
        data[HEADER.size:HEADER.size + len(body)] = body
    if patch:
        patch(data)
    return bytes(data)


def permuted(name, perm, size):
    """Per-car slots re-ordered: index i shows template slot perm[i]."""
    def patch(data):
        src = bytes(data)
        for i, slot in enumerate(perm):
            a = HEADER.size + slot * size
            data[HEADER.size + i * size:HEADER.size + (i + 1) * size] = src[a:a + size]
        if name == "lap_data":
            for i in range(16):
                data[HEADER.size + i * 18] = i + 1
    return patch


def event(frame, code, details=b"", seat=0):
    data = _template("event")
    fmt, pid, version = struct.unpack_from("<HBB", data)
    HEADER.pack_into(data, 0, fmt, pid, version, 0, 86, 2, 16, UID, frame, frame / 20.0, 10, 255, 255, 255)
    data[HEADER.size:] = (code.encode() + bytes([seat]) + details.ljust(18, b"\0"))[:len(data) - HEADER.size]
    return bytes(data)


def hand_bytes(points, hand):
    raw = bytearray(17)
    raw[0] = len(hand)
    for i, card in enumerate(hand):
        raw[1 + i * 4:5 + i * 4] = bytes(card)
    return bytes([points]) + bytes(raw)


def call(mcp, name, args=None):
    async def go():
        return await mcp.call_tool(name, args or {})
    try:
        result = asyncio.run(go())
    except Exception as e:                    # argument validation errors surface as exceptions in-process
        return {"_exception": str(e)}
    text = _text(result)
    try:
        return _strict_json(text)
    except ValueError:
        return {"_text": text}


def feed(client, *packets):
    if client._tele_transport is None:
        client._tele_transport = object()          # tools that check the listener: pretend it is bound
    for data in packets:
        client._on_telemetry(data, ("127.0.0.1", 1))


TEMPLATE_NAMES = ["Thruster", "Pole", "Circler", "Arizona", "DeFranco", "Valkyrie", "Mystic", "Strider", "Jaguar",
                  "Drift", "f13rce", "Nacho", "The Serpent", "Neon", "Eagle", "Striker"]


# ------------------------------------------------------------------------------------------------ D11

def test_annotations():
    mcp = build_server(Config(allow_drive=True))
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name, t in tools.items():
        expected = name not in ("get_reference", "get_trigger_config")
        assert t.annotations.open_world_hint is expected, name
    for name in ("set_pit", "boost", "pick_card", "ban_card", "pick_tire", "drive"):
        assert tools[name].annotations.destructive_hint is True, name
    for name in ("stop_boost", "set_camera", "release_drive"):
        assert tools[name].annotations.destructive_hint is False, name


# ------------------------------------------------------------------------------------------------ C7 (strict types)

def test_lax_types_refused():
    mcp = build_server(Config(allow_drive=True, command_timeout=0.05))
    for name, args in (("pick_tire", {"compound": True}), ("pick_card", {"index": True}),
                       ("set_pit", {"pit": "yes"}), ("drive", {"pedal": True, "steer": 0.0}),
                       ("ban_card", {"index": "1.0"}), ("wait_for_trigger", {"timeout_s": True}),
                       ("pick_card", {"index": 0, "seq": "3x"}), ("pick_card", {"index": 0, "seq": True})):
        # (numeric strings such as seq="3" are accepted since 0.2.1: a stale client schema sent them in a real race)
        out = call(mcp, name, args)
        assert "_exception" in out or out.get("_is_error") or "validation" in json.dumps(out).lower(), (name, out)
        assert out.get("ok") is not True, (name, out)
    out = call(mcp, "drive", {"pedal": 1, "steer": 0, "duration_s": 0.1})    # JSON 1 is a valid float
    assert "_exception" not in out and "ok" in out, out


# ------------------------------------------------------------------------------------------------ T4, T5, T6, D9, D10

def test_session_num_cars_capped():
    mcp = build_server(Config())
    feed(mcp.state.client, pkt("session", 1, num_cars=200))
    out = call(mcp, "get_session")
    assert out["ok"] and out["num_cars"] == 16


def test_ai_observations_me_by_own_header_and_no_data():
    mcp = build_server(Config())
    client = mcp.state.client
    out = call(mcp, "get_ai_observations")
    assert out["ok"] is False and out["error_reason"] != "bad_argument"            # D9: no_data, not bad_argument
    feed(client, pkt("ai_observations", 1, seat0=3, body=bytes([3])),
         pkt("ai_observations", 2, seat0=7, body=bytes([5])),               # car 5, while my car was 7: not me
         pkt("lap_data", 3, seat0=9))                                       # client.my_car() is now 9
    out = call(mcp, "get_ai_observations")
    assert out["ok"] and [c["car"] for c in out["cars"]] == [3], out
    assert out["cars"][0].get("is_me") is True


def test_guest_seat_abilities_private_fields():
    mcp = build_server(Config())
    feed(mcp.state.client, pkt("car_abilities", 1, seats=(2, 6, 255, 255)))
    out = call(mcp, "get_car_abilities", {"car": "all"})
    rows = {r["car"]: r for r in out["cars"]}
    assert "ban_tokens" in rows[2] and "ban_tokens" in rows[6]
    assert "ban_tokens" not in rows[4] and "unlocked" not in rows[10]      # 10 is "local" in Participants: no matter


def test_field_median_is_the_true_median():
    import statistics
    mcp = build_server(Config())
    data = pkt("car_status", 1)
    feed(mcp.state.client, data)
    out = call(mcp, "get_car_status", {"car": "all"})
    values = [c["performance"]["Downforce"] for c in out["cars"]]
    assert abs(out["field_median"]["Downforce"] - statistics.median(values)) < 1e-3


# ------------------------------------------------------------------------------------------------ D8

def test_unknown_event_code_warned_malformed_refused():
    mcp = build_server(Config())
    feed(mcp.state.client, pkt("session", 1))
    out = call(mcp, "get_events", {"codes": ["card", "ZZZZ"]})
    assert out["ok"] and "ZZZZ" in out["warning"]
    out = call(mcp, "get_events", {"codes": ["CARDS"]})
    assert out["ok"] is False and out["error_reason"] == "bad_argument"


# ------------------------------------------------------------------------------------------------ X7 names

def test_names_only_from_the_same_frame():
    mcp = build_server(Config())
    client = mcp.state.client
    feed(client, pkt("participants", 100), pkt("lap_data", 100))
    out = call(mcp, "get_standings")
    named = [r for r in out["standings"] if r.get("name")]
    assert len(named) == 16 and "names_note" not in out
    # other cars swap (0 <-> 1) while my index stays 10; Participants is not re-sent
    perm = [1, 0] + list(range(2, 16))
    feed(client, pkt("lap_data", 101, patch=permuted("lap_data", perm, 18)),
         pkt("car_status", 101, patch=permuted("car_status", perm, 32)),
         pkt("car_telemetry", 101, patch=permuted("car_telemetry", perm, 58)))
    for name, args, key in (("get_standings", {}, "standings"), ("get_car_status", {"car": "all"}, "cars"),
                            ("get_car_telemetry", {"car": "all"}, "cars")):
        out = call(mcp, name, args)
        for row in out[key]:
            if row.get("name"):
                assert row["name"] == TEMPLATE_NAMES[perm[row["car"]]], (name, row)
        mine = [r for r in out[key] if r.get("is_me")]
        assert len(mine) == 1 and mine[0]["car"] == 10 and mine[0]["name"] == "f13rce", (name, mine)
        assert "names_note" in out
    snap = call(mcp, "get_race_snapshot")
    for key in ("car_ahead", "car_behind"):
        row = snap[key]
        assert row.get("name") in (None, TEMPLATE_NAMES[perm[row["car"]]]), row


def test_my_name_follows_my_index_after_reorder():
    mcp = build_server(Config())
    client = mcp.state.client
    perm = list(range(16))[::-1]                  # my car (slot 10) is now index 5
    feed(client, pkt("participants", 100), pkt("lap_data", 101, seat0=5, patch=permuted("lap_data", perm, 18)))
    out = call(mcp, "get_standings")
    mine = [r for r in out["standings"] if r.get("is_me")]
    assert mine[0]["car"] == 5 and mine[0]["name"] == "f13rce"
    assert [r for r in out["standings"] if r.get("name")] == mine


# ------------------------------------------------------------------------------------------------ X8 selectors

def test_selectors_ahead_behind_position():
    mcp = build_server(Config())
    client = mcp.state.client
    perm = [1, 0] + list(range(2, 9)) + [10, 9] + list(range(11, 16))     # I overtook: my car is index 9 (P10)
    feed(client, pkt("lap_data", 50, seat0=9, patch=permuted("lap_data", perm, 18)),
         pkt("car_status", 52, seat0=9, patch=permuted("car_status", perm, 32)))
    out = call(mcp, "get_car_status", {"car": "ahead"})
    assert out["ok"] and out["cars"][0]["car"] == 8 and out["resolved_car_index"] == 8
    assert out["resolved_from_lap_data_frame_delta"] == 2
    out = call(mcp, "get_car_status", {"car": "behind"})
    assert out["cars"][0]["car"] == 10 and out["resolved_position"] == 11
    out = call(mcp, "get_car_status", {"car": "P1"})
    assert out["cars"][0]["car"] == 0
    out = call(mcp, "get_car_status", {"car": "me"})
    assert out["cars"][0]["car"] == 9 and out["cars"][0]["is_me"]
    out = call(mcp, "get_car_status", {"car": "P17"})
    assert out["ok"] is False and out["error_reason"] == "bad_argument"
    snap = call(mcp, "get_race_snapshot")
    assert snap["car_ahead"]["selector"] == "ahead" and snap["car_ahead"]["car"] == 8
    assert snap["car_behind"]["car"] == 10


# ------------------------------------------------------------------------------------------------ X11 hand_changed

def test_pick_card_with_stale_seq_refused_hand_changed():
    mcp = build_server(Config(command_timeout=0.05))
    client = mcp.state.client
    feed(client, pkt("session", 1), event(2, "CARD", hand_bytes(2, [[13, 6, 0, 0], [0, 0, 0, 0], [11, 3, 0, 0]])))
    first = [t for t in client.triggers.triggers if t["type"] == "card_offer"][-1]["seq"]
    feed(client, event(3, "CARD", hand_bytes(1, [[2, 1, 0, 0], [7, 2, 0, 0], [15, 1, 0, 0]])))
    second = [t for t in client.triggers.triggers if t["type"] == "card_offer"][-1]["seq"]
    assert second > first
    for tool_name in ("pick_card", "ban_card"):
        out = call(mcp, tool_name, {"index": 0, "seq": first})
        assert out["ok"] is False and out["error_reason"] == "hand_changed", out
        assert out["current_hand"]["seq"] == second and len(out["current_hand"]["hand"]) == 3
    out = call(mcp, "pick_card", {"index": 0, "seq": 999})
    assert out["error_reason"] == "hand_changed"


# ------------------------------------------------------------------------------------------------ X3 wait cap

def test_wait_cap_never_above_60():
    from backseat_champions_mcp.server import WAIT_HARD_MAX_S
    config = Config.from_env({"BC_WAIT_MAX_S": "240"}).validate()
    assert config.wait_max_s <= 60 and WAIT_HARD_MAX_S == 60
    assert Config.from_env({}).validate().wait_max_s == 55
    mcp = build_server(config)
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert "about 80 s" in tools["wait_for_trigger"].description or "80 s" in json.dumps(
        tools["wait_for_trigger"].input_schema)


# ------------------------------------------------------------------------------------------------ results (R5, R7, R8)

def test_corrupt_stored_file_structured_error(tmp_path):
    folder = tmp_path / "results"
    folder.mkdir()
    stamp = "20260101-120000-000001Z"
    (folder / (stamp + ".results.json")).write_text("{ not json")
    (folder / (stamp + ".headers.json")).write_text(json.dumps({"x-bc-format": "2"}))
    mcp = build_server(Config(results_port=0, results_dir=str(folder)))
    out = call(mcp, "get_race_result", {"result_id": stamp})
    assert out["ok"] is False and out["error_reason"] == "stored_file_unreadable" and out["explanation"]
    out = call(mcp, "get_race_result", {"result_id": "nope"})
    assert out["ok"] is False and out["error_reason"] == "bad_argument"


def test_race_result_steam_ids_only_on_request(tmp_path):
    folder = tmp_path / "results"
    folder.mkdir()
    stamp = "20260101-120000-000002Z"
    doc = {"format": 2, "game_version": "0.88.2", "host_steam_id": 76561198000000001, "sessions": [
        {"kind": "race", "classification": [{"position": 1, "name": "A", "steam_id": 76561198000000002,
                                             "gap_to_leader_s": 1.23456789}]}]}
    (folder / (stamp + ".results.json")).write_text(json.dumps(doc))
    (folder / (stamp + ".headers.json")).write_text(json.dumps({"x-bc-format": "2"}))
    mcp = build_server(Config(results_port=0, results_dir=str(folder)))
    out = call(mcp, "get_race_result", {})
    row = out["sessions"][0]["classification"][0]
    assert "steam_id" not in row and "host_steam_id" not in out
    assert row["gap_to_leader_s"] == 1.23456789                          # not rounded (R8)
    out = call(mcp, "get_race_result", {"include_steam_ids": True})
    assert out["host_steam_id"] == "76561198000000001"
    assert out["sessions"][0]["classification"][0]["steam_id"] == "76561198000000002"


# ------------------------------------------------------------------------------------------------ X4 / pause

def test_paused_status_carries_card_state():
    mcp = build_server(Config())
    client = mcp.state.client
    out = call(mcp, "get_pending_decisions")
    assert out["ok"] and out["cards"]["open"] is False and out["tire_screen"]["open"] is False


# ------------------------------------------------------------------------------------------------ round 2

def _player(frame, points, hand, seat0=10):
    return pkt("player", frame, seat0=seat0, body=bytes([0, seat0]) + hand_bytes(points, hand))


def test_points_zero_after_the_last_point_is_spent():
    """V-D8: points follow the newer of the Player sample and the CARD offer; 0 once Player says 0."""
    mcp = build_server(Config(command_timeout=0.05))
    client = mcp.state.client
    hand = [[13, 6, 0, 0], [0, 0, 0, 0], [11, 3, 0, 0]]
    feed(client, pkt("session", 1), pkt("lap_data", 2), _player(3, 1, hand), event(4, "CARD", hand_bytes(1, hand)))
    assert call(mcp, "get_race_snapshot")["points"] == 1
    assert call(mcp, "get_pending_decisions")["cards"]["points"] == 1
    feed(client, _player(6, 0, []), _player(7, 0, []))                       # the last point was spent
    snap = call(mcp, "get_race_snapshot")
    assert snap["ok"] and snap["points"] == 0, snap
    assert call(mcp, "get_pending_decisions")["cards"]["points"] == 0
    assert call(mcp, "get_player_state")["points"] == 0
    out = call(mcp, "wait_for_trigger", {"timeout_s": 0.2})
    assert out["ok"] and out["context"]["points"] == 0, out
    # a newer CARD offer wins over an older Player sample
    feed(client, event(9, "CARD", hand_bytes(2, [[2, 1, 0, 0], [7, 2, 0, 0], [15, 1, 0, 0]])))
    assert call(mcp, "get_race_snapshot")["points"] == 2


def test_num_cars_zero_index_is_bad_argument():
    """V-T6: an index with num_cars 0 is a bad_argument, never an IndexError / server_error."""
    mcp = build_server(Config())
    client = mcp.state.client
    feed(client, _telemetry(num_cars=0, seat0=255))
    for car in (0, "0", 15):
        out = call(mcp, "get_car_telemetry", {"car": car})
        assert out["ok"] is False and out["error_reason"] == "bad_argument", out
        assert "no cars" in out["explanation"]
    out = call(mcp, "get_car_telemetry", {"car": "all"})
    assert out["ok"] is True and out["cars"] == []


def test_selectors_are_ascii_only():
    """V-T10: Unicode digits are not a car index or a position."""
    mcp = build_server(Config())
    client = mcp.state.client
    feed(client, pkt("lap_data", 50), pkt("car_status", 50))
    for car in ("P１", "p١", "٣", "１", "P1²"):
        out = call(mcp, "get_car_status", {"car": car})
        assert out["ok"] is False and out["error_reason"] == "bad_argument", (car, out)
    assert call(mcp, "get_car_status", {"car": "P1"})["ok"] is True
    assert call(mcp, "get_car_status", {"car": " 3 "})["cars"][0]["car"] == 3


def test_standings_participants_notes():
    """V-T8: "No Participants packet yet" only when none arrived; a 0-car Participants packet is named as such."""
    mcp = build_server(Config())
    client = mcp.state.client
    feed(client, pkt("lap_data", 10))
    out = call(mcp, "get_standings")
    assert out["ok"] and "No Participants packet yet" in out["note"]
    feed(client, pkt("participants", 11, num_cars=0))
    out = call(mcp, "get_standings")
    assert "No Participants packet yet" not in out.get("note", "") and "lists no cars" in out["note"], out
    feed(client, pkt("participants", 12), pkt("lap_data", 12))
    assert "note" not in call(mcp, "get_standings")


def _ai_obs(frame, car, seat0=10, num_cars=16):
    def patch(data):
        data[HEADER.size] = car
    return pkt("ai_observations", frame, seat0=seat0, num_cars=num_cars, patch=patch)


def test_ai_observations_indices_selectors_and_no_data_wording():
    """V-T9 / V-D12: indices checked against num_cars, frame delta for selectors, precise no-data wording."""
    mcp = build_server(Config())
    client = mcp.state.client
    feed(client, _ai_obs(20, 3, num_cars=8))
    assert client.keys(P.PKT_AI_OBSERVATIONS) == [3]
    out = call(mcp, "get_ai_observations", {"car": 12})
    assert out["ok"] is False and out["error_reason"] == "bad_argument", out
    out = call(mcp, "get_ai_observations", {"car": 5})
    assert out["ok"] is False and out["error_reason"] == "no_data" and out["cars_with_data"] == [3], out
    assert "car index 5" in out["explanation"] and "has been received" not in out["explanation"]
    assert "online" not in out["hint"]
    out = call(mcp, "get_ai_observations")                                  # "me": seat 0's car 10 has no packet
    assert out["ok"] is False and out["cars_with_data"] == [3] and "your car" in out["explanation"], out
    out = call(mcp, "get_ai_observations", {"car": 3})
    assert out["ok"] and out["cars"][0]["car"] == 3
    # P4 is car index 3 in this Lap Data packet (template order: index i is position i + 1), 5 frames earlier
    feed(client, pkt("lap_data", 15, num_cars=8, patch=permuted("lap_data", list(range(16)), 18)))
    out = call(mcp, "get_ai_observations", {"car": "P4"})
    assert out["ok"] and out["resolved_car_index"] == 3 and out["resolved_from_lap_data_frame_delta"] == 5, out
    assert "selector_note" in out


def test_pause_and_session_end_wording():
    """V-T4: a hand is blamed only when one is pending; an ended session is not called a pause."""
    mcp = build_server(Config())
    client = mcp.state.client
    feed(client, pkt("session", 1), pkt("lap_data", 2), pkt("car_telemetry", 2))
    client.stale_after = lambda pid: -1.0                                    # every packet counts as stale
    client.paused = lambda: {"paused": True, "session_time_frozen_for_s": 5.0, "note": None}
    out = call(mcp, "get_car_telemetry")
    assert out["paused"] is True and "game is paused" in out["stale_note"]
    assert "card hand waits" not in out["stale_note"] and "no open card hand" in out["stale_note"]
    assert "frozen while the game is paused" in call(mcp, "get_race_snapshot")["laps_note"]
    feed(client, event(3, "CARD", hand_bytes(1, [[13, 6, 0, 0], [0, 0, 0, 0]])))
    assert "a card hand waits for pick_card" in call(mcp, "get_car_telemetry")["stale_note"]
    client.paused = lambda: {"paused": True, "session_ended": True, "session_time_frozen_for_s": 5.0, "note": None}
    out = call(mcp, "get_car_telemetry")
    assert "session has ended" in out["stale_note"] and "paused" not in out, out
    snap = call(mcp, "get_race_snapshot")
    assert snap["session_ended"] is True and "paused" not in snap and "frozen" not in snap["laps_note"], snap
    out = call(mcp, "get_pending_decisions")
    assert "paused" not in out


def test_hold_durations_have_a_minimum():
    """V-T7: boost / drive hold at least 0.05 s (schema), refused cleanly below."""
    mcp = build_server(Config(allow_drive=True))
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in ("boost", "drive"):
        assert tools[name].input_schema["properties"]["duration_s"]["minimum"] == 0.05
    out = call(mcp, "boost", {"speed": True, "duration_s": 0.01})
    assert "_exception" in out and "0.05" in out["_exception"]


def test_descriptions_are_dedented():
    """V-D5: tool / prompt descriptions are cleandoc'ed, identical on every Python version."""
    import inspect
    mcp = build_server(Config(allow_drive=True, results_port=0, results_dir=os.devnull))
    for tool in asyncio.run(mcp.list_tools()):
        assert tool.description == inspect.cleandoc(tool.description), tool.name
        assert not any(line.startswith("        ") for line in tool.description.splitlines()), tool.name
    for prompt in asyncio.run(mcp.list_prompts()):
        assert prompt.description == inspect.cleandoc(prompt.description)


def test_session_end_is_not_a_pause_with_the_real_client():
    """V-T4 with client.paused(): after SEND the snapshot says the session ended, not paused / frozen."""
    mcp = build_server(Config())
    client = mcp.state.client
    feed(client, pkt("session", 1), pkt("lap_data", 2), event(3, "SEND"))
    snap = call(mcp, "get_race_snapshot")
    assert snap["session_ended"] is True and "paused" not in snap, snap
    assert "ended" in snap["laps_note"]
