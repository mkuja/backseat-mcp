"""The codec against the captured packets and against the game's reference codec bc_api.py."""
import filecmp
import math
import os
import struct

import pytest

from conftest import DATA, UPSTREAM, load_bin
from backseat_champions_mcp import protocol as P

NAMES = [P.PACKET_NAMES[pid] for pid in sorted(P.PACKETS)]


def test_testdata_complete():
    assert sorted(f[:-4] for f in os.listdir(DATA) if f.endswith(".bin")) == sorted(NAMES)


def test_testdata_identical_to_upstream():
    upstream = os.path.join(UPSTREAM, "testdata")
    if not os.path.isdir(upstream):
        pytest.skip("upstream testdata not found")
    for name in NAMES:
        assert filecmp.cmp(os.path.join(DATA, name + ".bin"), os.path.join(upstream, name + ".bin"), shallow=False)


@pytest.mark.parametrize("name", NAMES)
def test_decode_every_capture(name):
    data = load_bin(name)
    header, body = P.decode(data)
    assert header["packet"] == name
    assert len(data) == P.PACKET_SIZES[header["packet_id"]]
    assert header["packet_version"] == (2 if name == "ai_observations" else 1)
    assert isinstance(body, dict)


def test_ai_observations_size_is_1432():
    assert P.PACKET_SIZES[P.PKT_AI_OBSERVATIONS] == 1432 == len(load_bin("ai_observations"))


def test_known_values():
    _, player = P.decode(load_bin("player"))
    assert player["car"] == 10 and player["points"] == 14 and player["total_laps"] == 24
    assert player["laps_remaining"] == 22 and player["tire_compound"] == "Wet"
    assert [c["upgrade"] for c in player["hand"]] == ["SensorQuality", "EnginePower", "BoostRechargeSpeed"]
    assert player["compound_picks_left"]["Standard"] is None and player["compound_picks_left"]["Boost"] == 2
    assert len(player["strategy"]["opponents"]) == 194 and len(player["strategy"]["near"]) == 42
    _, event = P.decode(load_bin("event"))
    assert event["code"] == "CARD" and event["seat"] == 0 and event["points"] == 1
    assert [c["upgrade"] for c in event["hand"]] == ["BoostCapacity", "PitSpeed", "Suspension"]
    _, session = P.decode(load_bin("session"))
    assert session["session_type"] == "Race" and session["scene"] == "JapanShort"
    assert session["feature_schema_hash"] == "0x40A5F76F"
    _, err = P.decode(load_bin("error"))
    assert err["command"] == "pick_card" and err["reason"] == "invalid_index"
    _, lap = P.decode(load_bin("lap_data"))
    assert sorted(c["position"] for c in lap["cars"]) == list(range(1, 17))


def test_rejects_garbage():
    for bad in (b"", b"x" * 10, load_bin("player")[:-1], b"\x02\x00" + load_bin("player")[2:]):
        with pytest.raises(P.PacketError):
            P.decode(bad)
    data = bytearray(load_bin("ping" if False else "ack"))
    data[2] = 99
    with pytest.raises(P.PacketError):
        P.decode(bytes(data))


def test_unknown_event_code_is_kept():
    data = bytearray(load_bin("event"))
    data[24:28] = b"ZZZZ"
    _, body = P.decode(bytes(data))
    assert body["code"] == "ZZZZ" and "details_hex" in body


def _norm(value):
    """bc_api uses tuples and raw enum ints for unknown values; compare structurally with float tolerance."""
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    return value


def _close(a, b, path="body"):
    if isinstance(a, dict):
        assert isinstance(b, dict), path
        for key in b:
            assert key in a, "%s.%s missing" % (path, key)
            _close(a[key], b[key], path + "." + key)
    elif isinstance(a, list):
        assert len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b)):
            _close(x, y, "%s[%d]" % (path, i))
    elif isinstance(a, float) or isinstance(b, float):
        assert math.isclose(a, b, rel_tol=0, abs_tol=0) or (math.isnan(a) and math.isnan(b)), path
    else:
        assert a == b, "%s: %r != %r" % (path, a, b)


@pytest.mark.parametrize("name", NAMES)
def test_matches_reference_decoder(bc_api, name):
    data = load_bin(name)
    ref_header, ref_body = bc_api.decode(data)
    header, body = P.decode(data)
    for key in ("packet_format", "packet_id", "packet_version", "game_version", "num_cars", "session_uid",
                "frame", "session_time", "packet"):
        assert header[key] == ref_header[key], key
    assert header["seat_car"] == [None if s == 255 else s for s in ref_header["seat_car"]]
    ref_body = _norm(ref_body)
    if name == "error":
        ref_body["reason"] = ref_body["reason"]
    _close(_norm(body), ref_body, name)


def test_command_encoders_match_reference(bc_api):
    cases = [
        (P.encode_ping(7), bc_api.ping(7)),
        (P.encode_hello(8), bc_api.hello_command(8)),
        (P.encode_pick_card(2, request_id=9), bc_api.pick_card(2, request_id=9)),
        (P.encode_set_pit(True, request_id=10), bc_api.set_pit(True, request_id=10)),
        (P.encode_set_pit(False, request_id=10), bc_api.set_pit(False, request_id=10)),
        (P.encode_pick_tire("Fast", request_id=11), bc_api.pick_tire("Fast", request_id=11)),
        (P.encode_pick_tire("wet", request_id=11), bc_api.pick_tire(5, request_id=11)),
        (P.encode_drive(0.5, -0.25, request_id=12), bc_api.drive(0.5, -0.25, request_id=12)),
        (P.encode_release_drive(request_id=13), bc_api.release_drive(request_id=13)),
        (P.encode_ban_card(1, request_id=14), bc_api.ban_card(1, request_id=14)),
        (P.encode_boost(P.boost_buttons(True, True, False), request_id=15),
         bc_api.boost(True, True, False, request_id=15)),
        (P.encode_boost(0, request_id=16), bc_api.boost(request_id=16)),
        (P.encode_set_camera("Chase", request_id=17), bc_api.set_camera("Chase", request_id=17)),
        (P.encode_set_camera("next", request_id=18), bc_api.set_camera(bc_api.CAMERA_NEXT, request_id=18)),
        (P.encode_ping(19, token="a-long-enough-token"), bc_api.ping(19, token="a-long-enough-token")),
    ]
    for ours, theirs in cases:
        assert ours == theirs
    for cid, size in P.COMMAND_SIZES.items():
        assert bc_api.COMMAND_SIZES[cid] == size
    for pid, size in P.PACKET_SIZES.items():
        assert bc_api.PACKET_SIZES[pid] == size


def test_tables_match_reference(bc_api):
    assert list(P.COMPOUNDS) == bc_api.COMPOUNDS
    assert list(P.UPGRADES) == bc_api.UPGRADES
    assert list(P.FACILITIES) == bc_api.FACILITIES
    assert list(P.CAR_TYPES) == bc_api.CAR_TYPES
    assert list(P.CAMERA_TYPES) == bc_api.CAMERA_TYPES
    assert list(P.PERFORMANCE_AREAS) == bc_api.PERFORMANCE_AREAS
    assert list(P.ERROR_REASONS) == bc_api.ERRORS


def test_command_sizes_and_token_rules():
    assert len(P.encode_drive(1, 0)) == 65 and len(P.encode_release_drive()) == 57
    assert struct.unpack_from("<HBBI", P.encode_pick_card(3, request_id=0xFFFFFFFF))[3] == 0xFFFFFFFF
    with pytest.raises(ValueError):
        P.check_token("short")
    with pytest.raises(ValueError):
        P.check_token("x" * 48)
    assert P.check_token("") == b""
    assert P.check_token("ä" * 6) == "ä".encode() * 6          # 12 bytes
    with pytest.raises(ValueError):
        P.encode_drive(float("nan"), 0)


def test_name_lookup():
    assert P.compound_index("GOLDEN") == 6 and P.compound_index(0) == 0 and P.compound_index("3") == 3
    assert P.camera_index("onboard_battlecam") == 10 and P.camera_index("Onboard-Driver") == 7
    assert P.camera_index("auto") == 255 and P.camera_index(253) == 253
    for bad in ("Slick", 7, -1, True):
        with pytest.raises(ValueError):
            P.compound_index(bad)
