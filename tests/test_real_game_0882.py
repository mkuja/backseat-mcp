"""Regression tests for bugs seen against the REAL game (Backseat Champions 0.88.2, an 8-lap race at China).

Each test encodes one observation from that race (in-process tool calls with packets built from the captured
templates; commands are answered by a stub, no game needed). Engine-level tests for the same observations are in
test_triggers.py (lap counting, pit call verification).
"""
import asyncio
import json
import struct

from backseat_champions_mcp import protocol as P
from backseat_champions_mcp import reference as R
from backseat_champions_mcp import instructions as I
from backseat_champions_mcp.client import Config, failure
from backseat_champions_mcp.server import build_server

from test_review_regressions import HEADER, UID, _template, _text, call, permuted, pkt
from test_review_regressions import feed as _feed

LAP_CAR = 18
PLAYER = HEADER.size
TOTAL = 8


def feed(client, *packets):
    """Feed packets; every one reaches the trigger engine (the client samples Player / Lap Data at 10 Hz of wall
    clock, which an in-process test outruns)."""
    for data in packets:
        client._trigger_fed.clear()
        _feed(client, data)


def evt(frame, code, details=b"", st=None, seat=0):
    data = _template("event")
    fmt, pid, version = struct.unpack_from("<HBB", data)
    HEADER.pack_into(data, 0, fmt, pid, version, 0, 86, 2, 16, UID, frame, frame / 20.0 if st is None else st,
                     10, 255, 255, 255)
    data[HEADER.size:] = (code.encode() + bytes([seat]) + details.ljust(18, b"\0"))[:len(data) - HEADER.size]
    return bytes(data)


def lap_data(frame, my_index, my_lap, st=None, pit_planned=False):
    """Lap Data with index = position - 1 (as in 0.88.2) and my car's raw lap (the lap being driven)."""
    def patch(data):
        permuted("lap_data", list(range(16)), LAP_CAR)(data)
        off = HEADER.size + my_index * LAP_CAR
        struct.pack_into("<H", data, off + 2, my_lap)
        data[off + 4] = (data[off + 4] & ~(1 | 2 | 4)) | (4 if pit_planned else 0)
    return pkt("lap_data", frame, seat0=my_index, st=st, patch=patch)


def player(frame, st=None, laps_after_current=None, pit_planned=False, can_pit=True, energy=None, car=10,
           total=TOTAL, health=None, puncture=None):
    def patch(data):
        data[PLAYER + 1] = car
        data[PLAYER + 2] = 0                       # no points: no card hand in these tests
        data[PLAYER + 3] = 0
        if laps_after_current is not None:
            struct.pack_into("<HH", data, PLAYER + 22, total, laps_after_current)
        data[PLAYER + 26] = (1 if pit_planned else 0) | (2 if can_pit else 0)
        if puncture is not None:
            data[PLAYER + 29] = 1 if puncture else 0
        if health is not None:
            struct.pack_into("<f", data, PLAYER + 30, health)
        if energy is not None:
            struct.pack_into("<f", data, PLAYER + 41, energy)
    return pkt("player", frame, seat0=car, st=st, patch=patch)


def session(frame, st=None, total=TOTAL):
    def patch(data):
        struct.pack_into("<H", data, HEADER.size + 2, total)
    return pkt("session", frame, st=st, patch=patch)


def server():
    mcp = build_server(Config(command_timeout=0.05))
    return mcp, mcp.state.client


def stub(client, name, result, calls=None):
    async def fake(*args):
        if calls is not None:
            calls.append(args)
        return dict(result)
    setattr(client, name, fake)


# ------------------------------------------------------------------------------------------------ 1. lap counting

def test_laps_end_of_race_not_9_of_8():
    """Observed: end-of-race context showed laps_completed 9/8 (Lap Data lap reads 9 after the flag of an 8-lap race;
    LAPC 9 at the flag)."""
    mcp, client = server()
    feed(client, session(1, st=390.0), lap_data(2, 7, 1, st=390.1), player(2, st=390.1, laps_after_current=7),
         evt(3, "LAPC", struct.pack("<HB", 1, 8), st=392.6))
    snap = call(mcp, "get_race_snapshot")
    assert (snap["current_lap"], snap["laps_completed"], snap["laps_remaining"]) == (1, 0, 8), snap
    # lap 7 of 8: two laps still to finish (0.2.0 said 7 completed, 1 remaining)
    feed(client, evt(4, "LAPC", struct.pack("<HB", 7, 8), st=932.0), lap_data(5, 7, 7, st=932.1),
         player(5, st=932.1, laps_after_current=1))
    snap = call(mcp, "get_race_snapshot")
    assert (snap["current_lap"], snap["laps_completed"], snap["laps_remaining"]) == (7, 6, 2), snap
    assert "final_lap" not in snap
    ps = call(mcp, "get_player_state")
    assert ps["laps_after_current"] == 1 and ps["current_lap"] == 7 and ps["laps_completed"] == 6
    assert ps["laps_remaining"] == 2
    # the final lap
    feed(client, evt(6, "LAPC", struct.pack("<HB", 8, 8), st=1022.0), lap_data(7, 7, 8, st=1022.1),
         player(7, st=1022.1, laps_after_current=0))
    snap = call(mcp, "get_race_snapshot")
    assert (snap["current_lap"], snap["laps_completed"], snap["laps_remaining"]) == (8, 7, 1), snap
    assert snap["final_lap"] is True
    st = call(mcp, "get_standings")
    me = [r for r in st["standings"] if r.get("is_me")][0]
    assert me["lap"] == 8 and me["laps_completed"] == 7 and "laps" not in me
    # the flag
    feed(client, evt(8, "LAPC", struct.pack("<HB", 9, 8), st=1112.0), evt(9, "FINI", bytes([8]), st=1112.0),
         lap_data(10, 7, 9, st=1112.1))
    snap = call(mcp, "get_race_snapshot")
    assert snap["laps_completed"] == 8 and snap["total_laps"] == 8 and snap["laps_remaining"] == 0, snap
    assert snap["current_lap"] is None
    cfg = call(mcp, "get_trigger_config")
    assert "observed in 0.88.2" in cfg["laps"]["lap_semantics"]
    assert "lap now being driven" in cfg["state"]["lapc_lap_meaning"]


# ------------------------------------------------------------------------------------------------ 2. lost pit call

def test_set_pit_ack_is_not_a_confirmation_and_lost_call_is_surfaced():
    """Observed: set_pit(true) Ack at session time 460.5, ~1.5 s before the lap-2 boundary (LAPC / PITW ~462);
    afterwards Player pit_planned was false. A second set_pit(true) after the boundary stuck. Later pit_planned became
    true with can_pit false without any API call (the game's own strategy)."""
    mcp, client = server()
    sent = []
    stub(client, "set_pit", {"ok": True, "command": "set_pit", "request_id": 1}, sent)
    feed(client, session(1, st=455.0), lap_data(2, 7, 1, st=455.0), player(2, st=455.0, laps_after_current=7),
         player(3, st=460.5, laps_after_current=7))
    out = call(mcp, "set_pit", {"pit": True})
    assert out["ok"] is True and out["confirmed"] is False and "not confirmed" in out["note"], out
    assert out["pit_planned_now"] is False
    pend = call(mcp, "get_pending_decisions")
    assert pend["pit"]["api_call"]["state"] == "pending" and pend["pit"]["pit_call_lost"] is False, pend
    # the lap boundary; the game drops the call
    feed(client, evt(4, "LAPC", struct.pack("<HB", 2, 8), st=462.0),
         evt(5, "PITW", struct.pack("<HBB", 2, 0, 1), st=462.0))
    frame = 6
    for i in range(15):
        feed(client, lap_data(frame, 7, 2, st=462.1 + i * 0.1), player(frame, st=462.1 + i * 0.1,
                                                                       laps_after_current=6))
        frame += 1
    pend = call(mcp, "get_pending_decisions")
    assert pend["pit"]["pit_call_lost"] is True and pend["pit"]["api_call"]["state"] == "lost", pend
    assert pend["pit"]["api_call"]["called_at_session_time_s"] == 460.5
    assert pend["pit"]["pit_planned"] is False
    assert call(mcp, "get_race_snapshot")["pit_call_lost"] is True
    r = call(mcp, "wait_for_trigger", {"timeout_s": 0.05})
    assert r["context"]["pit_call_lost"] is True
    assert any("pit_call_lost" in n and "does not re-send" in n for n in r.get("notes", [])), r
    r2 = call(mcp, "wait_for_trigger", {"timeout_s": 0.05})
    assert not any("pit_call_lost:" in n for n in r2.get("notes", [])), r2         # the note comes once
    assert len(sent) == 1                                                           # never re-sent by the server
    # the game's own strategy plans the stop (no API call): pit_planned_by "game", lost is over
    feed(client, player(frame, st=470.0, laps_after_current=6, pit_planned=True, can_pit=False))
    pend = call(mcp, "get_pending_decisions")
    assert pend["pit"]["pit_planned_by"] == "game" and pend["pit"]["pit_call_lost"] is False, pend
    snap = call(mcp, "get_race_snapshot")
    assert snap["pit_planned_by"] == "game" and "pit_call_lost" not in snap
    assert len(sent) == 1


def test_set_pit_confirmed_when_a_sample_shows_pit_planned():
    """The second call (after the boundary) stuck: a sample with pit_planned confirms it, pit_planned_by "api"."""
    mcp, client = server()
    stub(client, "set_pit", {"ok": True, "command": "set_pit", "request_id": 2})
    feed(client, session(1, st=464.0), lap_data(2, 7, 2, st=464.0), player(2, st=464.0, laps_after_current=6))

    async def go():
        async def sample():
            await asyncio.sleep(0.1)
            feed(client, player(3, st=464.2, laps_after_current=6, pit_planned=True))
        task = asyncio.ensure_future(sample())
        result = await mcp.call_tool("set_pit", {"pit": True})
        await task
        return json.loads(_text(result))
    out = asyncio.run(go())
    assert out["ok"] and out["confirmed"] is True and out["pit_planned_now"] is True, out
    pend = call(mcp, "get_pending_decisions")
    assert pend["pit"]["pit_planned_by"] == "api" and pend["pit"]["api_call"]["state"] == "confirmed"


# ------------------------------------------------------------------------------------------------ 3. cards_unavailable

WRONG_HINTS = ("after the car leaves", "PITO", "retry after the pit", "after the pit lane", "pit exit (PITO)")


def test_cards_unavailable_hint_says_retry_not_wait_for_pit_exit():
    """Observed: a hand arrived in the pit box (after pick_tire), the game paused, pick_card -> cards_unavailable;
    waiting for PITO deadlocked (no PITO while paused); retrying pick_card ~30 s later succeeded."""
    mcp, client = server()
    stub(client, "pick_card", failure("cards_unavailable", "pick_card"))
    out = call(mcp, "pick_card", {"index": 0})
    assert out["ok"] is False and out["hand_still_open"] is True
    for text in (out["hint"], R.explain_error("cards_unavailable")[1], R.CARDS_UNAVAILABLE_HINT):
        assert "retry pick_card after a few seconds" in text and "do not wait for the pit exit" in text, text
        assert not any(w in text for w in WRONG_HINTS), text
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in ("pick_card", "wait_for_trigger"):
        desc = tools[name].description
        assert "retry pick_card after a few seconds" in " ".join(desc.split()), name
        assert "retry after the pit lane" not in desc and "pick the tire first" not in desc, name
    for text in (I.INSTRUCTIONS, I.race_engineer_prompt(None)):
        flat = " ".join(text.split())
        assert "cards_unavailable" in flat and "retry pick_card after a few seconds" in flat
        assert "answer the tire screen first, then pick again" not in flat
    assert "retry pick_card after a few seconds" in json.dumps(R.OBSERVED)


# ------------------------------------------------------------------------------------------------ 4. context car

def test_context_my_car_and_car_ahead_from_the_same_lap_data_packet():
    """Observed: a wait_for_trigger context showed my_car 6 and car_ahead.car 6 (position 7 vs my position 8): my_car
    came from the newest packet of any type, car_ahead from the Lap Data packet's own order."""
    mcp, client = server()
    feed(client, session(1), lap_data(2, 7, 3))           # Lap Data: I am index 7, position 8
    # a newer packet of another type after the game re-ordered: seat_car[0] is 6 there
    feed(client, pkt("car_telemetry", 3, seat0=6))
    assert client.my_car() == 6
    for ctx in (call(mcp, "get_race_snapshot"), call(mcp, "wait_for_trigger", {"timeout_s": 0.05})["context"]):
        assert ctx["position"] == 8 and ctx["my_car"] == 7, ctx
        assert ctx["car_ahead"]["car"] == 6 and ctx["car_ahead"]["position"] == 7, ctx
        assert ctx["car_ahead"]["car"] != ctx["my_car"]
        assert ctx["car_behind"]["car"] == 8 and ctx["car_behind"]["position"] == 9


# ------------------------------------------------------------------------------------------------ 5. seq as string

def test_pick_card_and_ban_card_accept_numeric_strings():
    """Observed: an MCP client with a stale cached tool schema sent pick_card seq="1" and strict validation rejected
    the call mid-race ("Input should be a valid integer")."""
    mcp, client = server()
    picked, banned = [], []
    stub(client, "pick_card", {"ok": True, "command": "pick_card", "request_id": 3}, picked)
    stub(client, "ban_card", {"ok": True, "command": "ban_card", "request_id": 4}, banned)
    hand = bytes([2, 3, 13, 6, 0, 0, 0, 0, 0, 0, 11, 3, 0, 0]).ljust(18, b"\0")
    feed(client, session(1), evt(2, "CARD", hand))
    seq = mcp.state.client.triggers.seq
    assert seq >= 1
    out = call(mcp, "ban_card", {"index": "1", "seq": str(seq)})
    assert out.get("ok") is True and banned == [(1,)], out
    out = call(mcp, "pick_card", {"index": "0", "seq": str(seq)})
    assert out.get("ok") is True and picked == [(0,)], out
    out = call(mcp, "pick_card", {"index": 0, "seq": "999"})
    assert out["ok"] is False and out["error_reason"] == "hand_changed", out   # converted, then checked
    out = call(mcp, "pick_card", {"index": "7"})
    assert out["ok"] is False and out["error_reason"] == "bad_argument", out
    # still refused: bools, floats, non-numeric strings
    for args in ({"index": True}, {"index": 1.5}, {"index": "1.0"}, {"index": " 1"}, {"index": "x"},
                 {"index": "１"}, {"index": 0, "seq": True}, {"index": 0, "seq": 3.0}, {"index": 0, "seq": "3a"},
                 {"index": 0, "seq": "-1"}, {"index": -1}, {"index": 4}):
        for name in ("pick_card", "ban_card"):
            out = call(mcp, name, args)
            assert out.get("ok") is not True, (name, args, out)
    assert len(picked) == 1 and len(banned) == 1


# ------------------------------------------------------------------------------------------------ 6. energy > 1

def test_energy_above_one_is_reported_and_documented():
    """Observed: energy (boost) up to 3.69 (BoostCapacity cards raise the cap)."""
    mcp, client = server()
    feed(client, session(1), player(2, energy=3.69, laps_after_current=5))
    assert call(mcp, "get_player_state")["energy"] == 3.69
    assert call(mcp, "get_race_snapshot")["energy"] == 3.69
    tools = {t.name: " ".join(t.description.split()) for t in asyncio.run(mcp.list_tools())}
    for name in ("get_car_telemetry", "get_player_state", "get_race_snapshot"):
        assert "can exceed 1 with capacity upgrades" in tools[name], name
        assert "energy (0..1)" not in tools[name] and "0 to 1;" not in tools[name], name
    assert "energy_above_one" in R.OBSERVED


# ------------------------------------------------------------------------------------------------ 7. health 0 punctures

def test_worn_out_tire_puncture_documented_and_reported():
    """Observed: tire_health reaching 0.0 set the puncture flag."""
    mcp, client = server()
    feed(client, session(1), player(2, health=0.0, puncture=True, laps_after_current=3),
         player(3, health=0.0, puncture=True, laps_after_current=3))
    trig = [t for t in client.triggers.triggers if t["type"] == "car_damaged"]
    assert trig and trig[0]["signal"] == "puncture" and trig[0]["tire_health"] == 0.0
    tools = {t.name: " ".join(t.description.split()) for t in asyncio.run(mcp.list_tools())}
    assert "worn-out tires (health 0.0) puncture" in tools["wait_for_trigger"]
    assert "a tire worn to health 0.0 punctures" in tools["wait_for_trigger"]
    ref = json.dumps(R.reference("triggers"))
    assert "health 0.0, puncture" in ref and "health 0.0 punctures" in ref
    assert "worn_tire_punctures" in R.OBSERVED
