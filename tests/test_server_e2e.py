"""End to end: the real MCP client talks to the server over stdio; the server talks UDP to tests/fake_game.py."""
import asyncio
import contextlib
import json
import os
import socket
import sys
import time
import urllib.request

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

from conftest import ROOT, SRC, free_udp_port
from fake_game import FakeGame

READ_TOOLS = ["get_api_status", "ping", "get_session", "get_participants", "get_standings", "get_car_telemetry",
              "get_car_motion", "get_car_status", "get_car_abilities", "get_player_state", "get_pending_decisions",
              "get_racing_line", "get_ai_observations", "get_events", "wait_for_event", "get_race_snapshot",
              "get_reference", "wait_for_trigger", "get_triggers", "get_trigger_config"]
WRITE_TOOLS = ["pick_card", "ban_card", "set_pit", "pick_tire", "boost", "stop_boost", "set_camera"]


def free_tcp_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Session:
    def __init__(self, session, progress, messages=None, init=None):
        self.s = session
        self.progress = progress
        self.messages = messages if messages is not None else []     # every server notification / request
        self.init = init

    async def call(self, name, args=None, timeout=30):
        result = await self.s.call_tool(name, args or {}, read_timeout_seconds=timeout,
                                        progress_callback=self._progress(name))
        text = result.content[0].text
        try:
            data = json.loads(text)
        except ValueError:
            data = {"_text": text}
        data["_is_error"] = result.is_error
        return data

    def _progress(self, name):
        async def cb(progress, total, message):
            self.progress.append((name, progress, total, message))
        return cb


@contextlib.asynccontextmanager
async def mcp_server(telemetry_port, command_port, **env_extra):
    env = {"PYTHONPATH": SRC, "BC_TELEMETRY_PORT": str(telemetry_port), "BC_COMMAND_PORT": str(command_port),
           "BC_COMMAND_TIMEOUT": "0.5", "BC_PROGRESS_INTERVAL_S": "0.3", "BC_LOG_LEVEL": "WARNING"}
    env.update({k: str(v) for k, v in env_extra.items()})
    params = StdioServerParameters(command=sys.executable, args=["-m", "backseat_champions_mcp"], env=env, cwd=ROOT)
    progress = []
    messages = []

    async def on_message(message):
        messages.append(message)

    async def on_log(params):
        messages.append(("log", params))

    with open(os.environ.get("BC_TEST_ERRLOG") or os.devnull, "a") as errlog:
        async with stdio_client(params, errlog=errlog) as (r, w):
            async with ClientSession(r, w, message_handler=on_message, logging_callback=on_log) as session:
                init = await session.initialize()
                yield Session(session, progress, messages, init)


@contextlib.contextmanager
def fake_game(**kw):
    port = free_udp_port()
    game = FakeGame(telemetry_port=port, rate_hz=kw.pop("rate_hz", 25), **kw).start()
    try:
        yield game, port
    finally:
        game.stop()


async def until(fn, timeout=5.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await fn()
        if value:
            return value
        await asyncio.sleep(step)
    raise AssertionError("condition not met within %.1f s" % timeout)


async def wait_receiving(s):
    async def ok():
        st = await s.call("get_api_status")
        return st["telemetry"]["receiving"] and st["telemetry"]["packets_by_type"].get("participants")
    await until(ok)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 120))


# ------------------------------------------------------------------------------------------------ tools

def test_tools_listed_and_every_read_tool_works():
    async def main():
        with fake_game() as (game, port):
            async with mcp_server(port, game.command_port) as s:
                tools = {t.name: t for t in (await s.s.list_tools()).tools}
                assert set(READ_TOOLS + WRITE_TOOLS) == set(tools)
                assert "drive" not in tools and "list_race_results" not in tools
                for name in READ_TOOLS:
                    assert tools[name].annotations.read_only_hint is True, name
                    assert len(tools[name].description) > 150, name
                for name in WRITE_TOOLS:
                    assert tools[name].annotations.read_only_hint is False, name
                await wait_receiving(s)
                status = await s.call("get_api_status")
                assert status["game"]["reachable"] and status["game"]["game_version"] == "0.86.2"
                assert status["telemetry"]["my_car"] == 10 and status["game"]["switches_off"] == []
                game.card_event()
                await asyncio.sleep(0.2)
                args = {"wait_for_event": {"timeout_s": 0.3, "since_seq": 0}, "wait_for_trigger": {"timeout_s": 0.5},
                        "get_reference": {"topic": "error_reasons"}}
                results = {}
                for name in READ_TOOLS:
                    out = await s.call(name, args.get(name))
                    assert out["ok"] is True and not out["_is_error"], (name, out)
                    results[name] = out
                assert results["get_standings"]["standings"][0]["position"] == 1
                me = [r for r in results["get_standings"]["standings"] if r.get("is_me")][0]
                assert me["car"] == 10 and me["name"]
                assert results["get_car_telemetry"]["cars"][0]["tire_compound"] == "Wet"
                assert results["get_player_state"]["points"] == 2
                assert "opponents" not in results["get_player_state"]["strategy"]
                full = await s.call("get_player_state", {"include_strategy_blocks": True})
                assert len(full["strategy"]["opponents"]) == 194
                assert results["get_pending_decisions"]["cards"]["open"] is True
                assert len(results["get_racing_line"]["points"]) == 20
                assert results["get_ai_observations"]["cars"][0]["policy_name"] == "AllCars.onnx"
                assert results["get_events"]["events"][-1]["code"] == "CARD"
                assert results["wait_for_trigger"]["triggered"] is True
                assert results["wait_for_trigger"]["triggers"][0]["type"] == "card_offer"
                every = await s.call("get_car_telemetry", {"car": "all", "include_wheels": True})
                assert len(every["cars"]) == 16 and len(every["cars"][0]["wheels"]) == 4
                one = await s.call("get_car_status", {"car": 3})
                assert one["cars"][0]["car"] == 3 and "field_median" in one
                bad = await s.call("get_car_motion", {"car": 42})
                assert bad["ok"] is False and bad["error_reason"] == "bad_argument" and bad["_is_error"]
                bad = await s.call("get_reference", {"topic": "nope"})
                assert bad["ok"] is False
                # resources and prompt
                res = await s.s.read_resource("bc://reference/compounds")
                assert json.loads(res.contents[0].text)[6]["name"] == "Golden"
                res = await s.s.read_resource("bc://triggers/latest")
                assert json.loads(res.contents[0].text)["trigger"]["type"] == "card_offer"
                prompt = await s.s.get_prompt("race_engineer")
                assert "wait_for_trigger" in prompt.messages[0].content.text
    run(main())


def test_write_tools_and_error_mapping():
    async def main():
        with fake_game() as (game, port):
            async with mcp_server(port, game.command_port) as s:
                await wait_receiving(s)
                out = await s.call("pick_card", {"index": 0})
                assert out["ok"] and out["picked"]["upgrade"] == "SensorQuality"
                out = await s.call("pick_card", {"index": 3})
                assert out["ok"] is False and out["error_reason"] == "invalid_index" and out["_is_error"]
                assert out["explanation"] and out["hint"]
                out = await s.call("pick_card", {"index": 9})          # schema validation: 0..3
                assert out["_is_error"]
                out = await s.call("ban_card", {"index": 1})
                assert out["ok"], out
                out = await s.call("ban_card", {"index": 2})            # last pickable card
                assert out["error_reason"] == "ban_refused"
                out = await s.call("set_pit", {"pit": True})
                assert out["ok"] and game.pit_planned
                game.can_pit = False
                out = await s.call("set_pit", {"pit": True})
                assert out["error_reason"] == "refused"
                out = await s.call("pick_tire", {"compound": "fast"})
                assert out["error_reason"] == "tire_select_closed" and "TIRE" in out["hint"]
                game.tire_event()
                await asyncio.sleep(0.1)
                pending = await s.call("get_pending_decisions")
                assert pending["tire_screen"]["open"] and pending["tire_screen"]["allowed"] == ["Standard", "Fast",
                                                                                                "Wet"]
                out = await s.call("pick_tire", {"compound": "Durable"})
                assert out["error_reason"] == "compound_refused"
                out = await s.call("pick_tire", {"compound": "FAST"})
                assert out["ok"] and out["compound"] == "Fast"
                out = await s.call("pick_tire", {"compound": "slicks"})
                assert out["error_reason"] == "bad_argument"
                out = await s.call("set_camera", {"camera": "chase"})
                assert out["ok"] and out["camera"] == "Chase"
                out = await s.call("set_camera", {"camera": "next"})
                assert out["ok"] and out["camera"] == "next"
                out = await s.call("set_camera", {"camera": "TrackOverview"})
                assert out["error_reason"] == "camera_unavailable"
                for cid, payload in ((2, b"\x00\x00"), (3, b"\x00\x01"), (4, b"\x00\x03"), (9, b"\x00\x09")):
                    assert any(c[2] == payload for c in game.received(cid)), cid
                game.player_input = False
                out = await s.call("set_pit", {"pit": False})
                assert out["error_reason"] == "input_disabled" and "Player Input" in out["hint"]
                game.player_input = True
                game.public_lobby = True
                out = await s.call("pick_card", {"index": 0})
                assert out["error_reason"] == "public_lobby"
                assert (await s.call("set_camera", {"camera": 1}))["ok"]
                game.public_lobby = False
                game.silent = True
                out = await s.call("ping")
                assert out["error_reason"] == "no_reply" and "Command Token" in out["hint"]
                game.silent = False
                status = await s.call("get_api_status")
                assert status["game"]["reachable"]
                game.player_input = False
                game.broadcast["ai_observations"] = False
                status = await s.call("get_api_status")
                off = {o["switch"]: o for o in status["game"]["switches_off"]}
                assert set(off) == {"Player Input", "AI Observations"}
                assert off["Player Input"]["required"] is True and off["AI Observations"]["required"] is False
                assert status["game"]["required_switches_off"] == ["Player Input"]          # D1
                game.player_input = True
                game.driver_input = False                     # drive tools not registered: not listed (D1)
                game.broadcast["player_data"] = False         # Car Abilities still has Driver Data (D3)
                status = await s.call("get_api_status")
                off = {o["switch"]: o for o in status["game"]["switches_off"]}
                assert "Driver Input" not in off and "get_car_abilities" not in off["Player Data"]["affects"]
                assert status["game"]["required_switches_off"] == ["Player Data"]
    run(main())


def test_boost_hold_repeats_then_stops():
    async def main():
        with fake_game() as (game, port):
            async with mcp_server(port, game.command_port) as s:
                await wait_receiving(s)
                t0 = time.monotonic()
                out = await s.call("boost", {"speed": True, "cornering": True, "duration_s": 1.0})
                assert out["ok"] and out["hold"]["active"] and "Presumed success" in out["note"]
                await asyncio.sleep(1.4)
                sent = game.received(8, since=t0)
                held = [c for c in sent if c[2] == b"\x00\x03"]
                released = [c for c in sent if c[2] == b"\x00\x00"]
                span = held[-1][0] - held[0][0]
                rate = (len(held) - 1) / span
                assert 40 <= rate <= 60, rate
                assert 0.85 <= span <= 1.15, span
                assert released and released[0][0] >= held[-1][0]
                n = len(game.received(8))
                await asyncio.sleep(0.3)
                assert len(game.received(8)) == n                   # nothing after the release
                status = await s.call("get_api_status")
                assert status["holds"]["boost"]["ended"] == "expired"
                # a running hold is replaced, and stop_boost ends it at once
                await s.call("boost", {"speed": True, "duration_s": 10})
                out = await s.call("boost", {"cornering": True, "duration_s": 10})
                assert out["ok"]
                out = await s.call("stop_boost")
                assert out["ok"] and out["was_holding"] is True
                n = len(game.received(8))
                await asyncio.sleep(0.3)
                assert len(game.received(8)) - n <= 1
                # an error reply during a hold ends it and is surfaced
                game.assist_on = True
                out = await s.call("boost", {"speed": True, "duration_s": 5})
                assert out["ok"] is False and out["error_reason"] == "assist_on"
                status = await s.call("get_api_status")
                assert status["holds"]["boost"]["ended"] == "error"
                out = await s.call("boost", {"duration_s": 1})
                assert out["error_reason"] == "bad_argument"
    run(main())


def test_drive_tools_are_gated():
    async def main():
        with fake_game() as (game, port):
            async with mcp_server(port, game.command_port, BC_ALLOW_DRIVE="1") as s:
                tools = {t.name: t for t in (await s.s.list_tools()).tools}
                assert "drive" in tools and "release_drive" in tools
                game.driver_input = False
                status = await s.call("get_api_status")
                off = {o["switch"]: o for o in status["game"]["switches_off"]}
                assert off["Driver Input"]["required"] is False and status["game"]["required_switches_off"] == []
                game.driver_input = True
                assert "achievements" in tools["drive"].description.lower()
                await wait_receiving(s)
                t0 = time.monotonic()
                out = await s.call("drive", {"pedal": 1.0, "steer": -0.5, "duration_s": 0.5})
                assert out["ok"], out
                await asyncio.sleep(0.8)
                drives = game.received(5, since=t0)
                assert 18 <= len(drives) <= 32
                assert game.received(6, since=t0)                     # release_drive at the end
                out = await s.call("release_drive")
                assert out["ok"] and out["was_holding"] is False
                game.host = False
                out = await s.call("drive", {"pedal": 0.5, "steer": 0, "duration_s": 1})
                assert out["error_reason"] == "not_host"
                out = await s.call("drive", {"pedal": 2, "steer": 0})
                assert out["_is_error"]
    run(main())


# ------------------------------------------------------------------------------------------------ waiting

def test_wait_for_event():
    async def main():
        with fake_game() as (game, port):
            async with mcp_server(port, game.command_port) as s:
                await wait_receiving(s)
                asyncio.get_running_loop().call_later(0.5, game.tire_event)
                out = await s.call("wait_for_event", {"codes": ["TIRE"], "timeout_s": 5})
                assert out["matched"] and out["events"][0]["code"] == "TIRE"
                assert out["events"][0]["picks_left"]["Standard"] is None
                out = await s.call("wait_for_event", {"codes": ["FINI"], "timeout_s": 0.5})
                assert out["matched"] is False
                out = await s.call("wait_for_event", {"timeout_s": 61})
                assert out["_is_error"]
    run(main())


def test_wait_for_trigger_card_damage_tires_timeout_and_session_end():
    async def main():
        with fake_game() as (game, port):
            async with mcp_server(port, game.command_port) as s:          # tire_screen is on by default
                game.points = 0
                await wait_receiving(s)
                # timeout (with progress notifications)
                t0 = time.monotonic()
                out = await s.call("wait_for_trigger", {"timeout_s": 1.0})
                assert out["triggered"] is False and out["status"] == "timeout"
                assert 0.9 <= time.monotonic() - t0 < 3
                assert any(p[0] == "wait_for_trigger" for p in s.progress)
                assert out["context"]["position"] == 11
                # card_offer: the CARD event arrives while waiting
                def offer():
                    game.points = 2
                    game.card_event()
                asyncio.get_running_loop().call_later(0.3, offer)
                out = await s.call("wait_for_trigger", {"timeout_s": 5})
                assert out["triggered"] and [t["type"] for t in out["triggers"]] == ["card_offer"]
                assert out["triggers"][0]["points"] == 2 and out["triggers"][0]["ban_tokens"] == 1
                last = out["last_seq"]
                # acknowledged: not returned again, but still listed as an open decision on every result
                out = await s.call("wait_for_trigger", {"timeout_s": 0.5, "since_seq": last})
                assert out["status"] == "timeout" and out["triggers"] == []
                assert [d["type"] for d in out["open_decisions"]] == ["card_offer"]
                assert out["open_decisions"][0]["open"] is True
                # once it is gone (the Player packet shows no hand) it is no longer open
                game.points = 0
                await asyncio.sleep(0.6)
                out = await s.call("wait_for_trigger", {"timeout_s": 0.5, "since_seq": last})
                assert out["triggered"] is False and out["open_decisions"] == []
                # car_damaged: puncture flag
                game.puncture = True
                out = await s.call("wait_for_trigger", {"timeout_s": 5})
                assert out["triggers"][0]["type"] == "car_damaged"
                assert out["triggers"][0]["signal"] == "puncture" and out["triggers"][0]["heuristic"] is False
                game.puncture = False
                # tires_wont_last: laps every 0.6 s, 0.2 health per lap
                game.pit_stop(health=0.95)                  # fresh tires come via PITI / PITO, as in the game
                game.start_scenario(lap_s=0.6, wear_per_lap=0.2, start_health=0.95, total_laps=30, lap=2)  # template: lap 2
                out = await s.call("wait_for_trigger", {"timeout_s": 10})
                trig = out["triggers"][0]
                assert trig["type"] == "tires_wont_last", out
                assert trig["reason"] == "prediction" and trig["predicted_health_end_of_next_lap"] <= 0.05
                assert 0.1 <= trig["wear_per_lap"] <= 0.35, json.dumps(trig)   # max(measured, extrapolated)
                assert trig["can_pit"] is True and trig["compound"] == "Wet"
                # the engineer calls the stop; the tire screen opens
                assert (await s.call("set_pit", {"pit": True}))["ok"]
                game.pit_stop()
                game.tire_event()
                out = await s.call("wait_for_trigger", {"timeout_s": 5})
                assert "tire_screen" in [t["type"] for t in out["triggers"]]
                assert any(d["type"] == "tire_screen" for d in out["open_decisions"])
                assert (await s.call("pick_tire", {"compound": "Standard"}))["ok"]
                config = await s.call("get_trigger_config")
                assert config["state"]["tires"]["stint_started_because"] == "pit exit"
                listed = await s.call("get_triggers")
                types = [t["type"] for t in listed["triggers"]]
                assert types[:4] == ["card_offer", "car_damaged", "tires_wont_last", "tire_screen"]
                # session end returns promptly, as a queue entry
                game.scenario = None
                asyncio.get_running_loop().call_later(0.3, game.end_session)
                t0 = time.monotonic()
                out = await s.call("wait_for_trigger", {"timeout_s": 10, "since_seq": out["last_seq"]})
                assert out["status"] == "session_ended" and time.monotonic() - t0 < 3
                assert out["session_end"]["code"] == "SEND" and out["triggers"][-1]["type"] == "session_ended"
                last = out["last_seq"]
                # a new session keeps the old entries (marked stale) but never returns them again
                game.new_session()
                await asyncio.sleep(0.3)
                listed = await s.call("get_triggers")
                assert listed["triggers"] and listed["acked_up_to_seq"] <= last
                out = await s.call("wait_for_trigger", {"timeout_s": 0.5, "since_seq": last})
                assert out["status"] == "timeout" and out["triggers"] == []
    run(main())


def test_pause_reported_not_stale():
    """L3: while the game is paused only Session / Car Status / Participants arrive; that is a pause, not a fault."""
    async def main():
        with fake_game() as (game, port):
            game.points = 0
            async with mcp_server(port, game.command_port) as s:
                await wait_receiving(s)
                game.pause()
                await asyncio.sleep(3.0)
                status = await s.call("get_api_status")
                tele = status["telemetry"]
                assert tele["paused"] and tele["paused"]["paused"] is True, tele
                assert "expected_not_arriving" not in tele
                assert not any("have not arrived recently" in w for w in status.get("warnings", []))
                out = await s.call("get_car_telemetry")
                assert out["ok"] and out.get("paused") is True and "paused" in out["stale_note"]
                assert "stopped" not in out["stale_note"]
                game.resume()
                await asyncio.sleep(0.5)
                status = await s.call("get_api_status")
                assert status["telemetry"]["paused"] is False
    run(main())


def test_wait_for_trigger_timeout_is_capped():
    async def main():
        with fake_game() as (game, port):
            game.points = 0
            async with mcp_server(port, game.command_port, BC_WAIT_MAX_S="1") as s:
                t0 = time.monotonic()
                out = await s.call("wait_for_trigger", {"timeout_s": 100})
                assert out["status"] == "timeout" and time.monotonic() - t0 < 3
    run(main())


# ------------------------------------------------------------------------------------------------ failure modes

def test_no_game_running():
    async def main():
        async with mcp_server(free_udp_port(), free_udp_port()) as s:
            t0 = time.monotonic()
            status = await s.call("get_api_status")
            assert status["ok"] and status["game"]["reachable"] is False
            assert status["game"]["error_reason"] in ("game_unreachable", "no_reply")
            out = await s.call("get_session")
            assert out["ok"] is False and out["error_reason"] == "no_data" and out["game_reachable"] is False
            assert "Settings > Telemetry" in out["hint"]
            out = await s.call("get_player_state")
            assert out["ok"] is False
            out = await s.call("pick_card", {"index": 0})
            assert out["ok"] is False and out["error_reason"] in ("game_unreachable", "no_reply")
            out = await s.call("boost", {"speed": True, "duration_s": 3})
            assert out["ok"] is False
            out = await s.call("wait_for_trigger", {"timeout_s": 0.5})
            assert out["status"] == "timeout" and "telemetry" in out
            out = await s.call("get_race_snapshot")
            assert out["ok"] is False
            assert time.monotonic() - t0 < 10
    run(main())


def test_switch_off_is_explained():
    async def main():
        with fake_game() as (game, port):
            game.broadcast["player_data"] = False
            async with mcp_server(port, game.command_port) as s:
                await wait_receiving(s)
                out = await s.call("get_player_state")
                assert out["error_reason"] == "switch_off" and "Player Data" in out["explanation"]
                game.broadcast["player_data"] = True
                game.broadcast["decision_events"] = False
                await asyncio.sleep(0.3)
                # without Decision Events the Player packet reports the hand (fallback)
                out = await s.call("wait_for_trigger", {"timeout_s": 5})
                assert out["triggers"][0]["type"] == "card_offer"
                assert "Player packet" in out["triggers"][0]["source"]
    run(main())


def test_telemetry_port_already_bound():
    async def main():
        with fake_game() as (game, port):
            blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            blocker.bind(("127.0.0.1", port))
            try:
                async with mcp_server(port, game.command_port) as s:
                    out = await s.call("get_session")
                    assert out["error_reason"] == "telemetry_listener_down"
                    assert "gameapi_client.py" in out["hint"]
                    out = await s.call("wait_for_trigger", {"timeout_s": 5})
                    assert out["error_reason"] == "telemetry_listener_down"
                    status = await s.call("get_api_status")
                    assert status["telemetry"]["bound"] is False and status["telemetry"]["problem"]
                    assert status["game"]["reachable"]                # commands still work
                    assert (await s.call("ping"))["ok"]
            finally:
                blocker.close()
    run(main())


def test_bad_config_exits_with_message():
    import subprocess
    env = dict(os.environ, PYTHONPATH=SRC, BC_COMMAND_TOKEN="short")
    proc = subprocess.run([sys.executable, "-m", "backseat_champions_mcp"], env=env, capture_output=True,
                          text=True, timeout=30, stdin=subprocess.DEVNULL)
    assert proc.returncode == 2 and "12 to 47" in proc.stderr and proc.stdout == ""
    for bad in ("abc", "0", "-1", "nan", "inf"):           # V-D10: a clean config error, never a traceback
        env = dict(os.environ, PYTHONPATH=SRC, BC_PROGRESS_INTERVAL_S=bad)
        proc = subprocess.run([sys.executable, "-m", "backseat_champions_mcp"], env=env, capture_output=True,
                              text=True, timeout=30, stdin=subprocess.DEVNULL)
        assert proc.returncode == 2 and "BC_PROGRESS_INTERVAL_S" in proc.stderr, (bad, proc.stderr)
        assert "Traceback" not in proc.stderr and proc.stdout == ""


def test_no_unsolicited_notifications():
    """V-D9: the server advertises neither logging nor resource subscriptions, so it sends neither
    notifications/message nor notifications/resources/updated, however many triggers fire."""
    async def main():
        with fake_game() as (game, port):
            async with mcp_server(port, game.command_port) as s:
                caps = s.init.capabilities
                assert caps.logging is None
                assert caps.resources is None or not caps.resources.subscribe
                await wait_receiving(s)
                game.card_event()
                out = await s.call("wait_for_trigger", {"timeout_s": 5})
                assert out["triggered"] and out["triggers"][0]["type"] == "card_offer"
                game.tire_event()
                out = await s.call("wait_for_trigger", {"timeout_s": 5, "since_seq": out["last_seq"]})
                assert any(t["type"] == "tire_screen" for t in out["triggers"])
                await asyncio.sleep(0.5)
                text = repr(s.messages)
                assert "resources/updated" not in text and "ResourceUpdated" not in text, text
                assert not any(isinstance(m, tuple) and m[0] == "log" for m in s.messages), text
                assert "LoggingMessage" not in text and "notifications/message" not in text, text
    run(main())


# ------------------------------------------------------------------------------------------------ results

def test_results_receiver():
    async def main(tmp):
        port = free_tcp_port()
        with fake_game() as (game, tport):
            async with mcp_server(tport, game.command_port, BC_RESULTS_PORT=port, BC_RESULTS_DIR=tmp,
                                  BC_RESULTS_TOKEN="secret-token-123") as s:
                tools = {t.name for t in (await s.s.list_tools()).tools}
                assert {"list_race_results", "get_race_result"} <= tools
                doc = {"format": 2, "game_version": "0.86.2", "public_lobby": False, "timepoint": 1,
                       "sessions": [{"kind": "race", "track": "JapanShort", "started_at": 1, "ended_at": 2,
                                     "race_id": "r1", "header": None,
                                     "classification": [{"position": 1, "class_position": 1, "car_id": 3,
                                                         "car_number": 7, "car_class": "StockCar", "name": "Pole",
                                                         "driver": "ai", "steam_id": None, "local_seat": None,
                                                         "grid": 2, "laps": 24, "gap_to_leader_s": 0,
                                                         "best_lap_s": 26.1}],
                                     "decisions": [{"type": "decision", "decision_type": "card",
                                                    "chosen_by": "ml_agent", "state": [1, 2], "offered": [[1]],
                                                    "chosen": 0}]}]}

                def post(token):
                    req = urllib.request.Request("http://127.0.0.1:%d/" % port, data=json.dumps(doc).encode(),
                                                 method="POST", headers={"Content-Type": "application/json",
                                                                         "X-BC-Format": "2",
                                                                         "X-BC-Game-Version": "0.86.2",
                                                                         "Authorization": "Bearer " + token})
                    try:
                        return urllib.request.urlopen(req, timeout=5).status
                    except urllib.error.HTTPError as e:
                        return e.code

                loop = asyncio.get_running_loop()
                assert await loop.run_in_executor(None, post, "wrong") == 401
                assert await loop.run_in_executor(None, post, "secret-token-123") == 200
                listed = await s.call("list_race_results")
                assert len(listed["results"]) == 1 and listed["results"][0]["game_version"] == "0.86.2"
                out = await s.call("get_race_result", {"include_decisions": True})
                session = out["sessions"][0]
                assert listed["results"][0]["session_count"] is None or isinstance(listed["results"][0]["session_count"], int)
                assert session["classification"][0]["name"] == "Pole"
                assert "steam_id" not in session["classification"][0] and "host_steam_id" not in out   # R8
                out = await s.call("get_race_result", {"include_steam_ids": True})
                assert "steam_id" in out["sessions"][0]["classification"][0]
                assert session["decisions_by_type"] == {"card/ml_agent": 1}
                assert "state" not in session["decisions"][0]
                out = await s.call("get_race_result", {"result_id": "../etc/passwd"})
                assert out["ok"] is False

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        run(main(tmp))
