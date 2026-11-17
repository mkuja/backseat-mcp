"""GameClient without MCP: listener hardening, config validation, reply matching."""
import asyncio
import socket

import pytest

from conftest import free_udp_port, load_bin
from fake_game import FakeGame
from backseat_champions_mcp import protocol as P
from backseat_champions_mcp.client import Config, GameClient


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


def test_config_from_env_and_validation():
    cfg = Config.from_env({"BC_TELEMETRY_PORT": "30000", "BC_ALLOW_DRIVE": "yes", "BC_TRIGGERS": "tire_screen",
                           "BC_RESULTS_PORT": "29780"}).validate()
    assert cfg.telemetry_port == 30000 and cfg.allow_drive and cfg.results_port == 29780
    assert cfg.triggers.enabled == {"tire_screen"}
    assert "token" not in str(cfg.public()).lower().replace("command_token_set", "")
    for bad in ({"BC_COMMAND_TOKEN": "too-short"}, {"BC_COMMAND_TOKEN": "x" * 48}, {"BC_TELEMETRY_PORT": "70000"},
                {"BC_TELEMETRY_PORT": "abc"}, {"BC_WAIT_MAX_S": "0"},
                {"BC_RESULTS_PORT": "1", "BC_RESULTS_HOST": "0.0.0.0"}):
        with pytest.raises(ValueError):
            Config.from_env(bad).validate()
    Config.from_env({"BC_COMMAND_TOKEN": "twelve-bytes"}).validate()


def test_remote_listener_only_accepts_the_game_host():
    async def main():
        port = free_udp_port()
        client = GameClient(Config(telemetry_host="0.0.0.0", telemetry_port=port, game_host="192.0.2.7").validate())
        await client.start()
        try:
            assert client.listener_status()["bound"]
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(load_bin("session"), ("127.0.0.1", port))
            s.close()
            await asyncio.sleep(0.2)
            assert client.latest(P.PKT_SESSION) is None
            assert client.dropped["not from the game host"] == 1
        finally:
            await client.stop()
    run(main())


def test_loopback_listener_caches_and_resets_per_session():
    async def main():
        port = free_udp_port()
        client = GameClient(Config(telemetry_port=port, command_port=free_udp_port()).validate())
        await client.start()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for name in ("session", "player", "lap_data", "error", "motion"):
                s.sendto(load_bin(name), ("127.0.0.1", port))
            s.sendto(b"garbage", ("127.0.0.1", port))
            await asyncio.sleep(0.2)
            assert client.latest(P.PKT_SESSION)[1]["scene"] == "JapanShort"
            assert client.latest(P.PKT_PLAYER, 0)[1]["car"] == 10
            assert client.my_car() == 10
            assert client.dropped["reply on the telemetry port"] == 1
            assert client.dropped["not a Game API packet"] == 1
            # the event capture is from another session: the cache is reset
            s.sendto(load_bin("event"), ("127.0.0.1", port))
            await asyncio.sleep(0.2)
            assert client.latest(P.PKT_SESSION) is None and client.events[-1]["code"] == "CARD"
            s.close()
        finally:
            await client.stop()
    run(main())


def test_second_listener_is_refused_not_shared():
    async def main():
        port = free_udp_port()
        first = GameClient(Config(telemetry_port=port).validate())
        second = GameClient(Config(telemetry_port=port).validate())
        await first.start()
        await second.start()
        try:
            assert first.listener_status()["bound"]
            assert not second.listener_status()["bound"]
            assert second.listener_failure()["error_reason"] == "telemetry_listener_down"
        finally:
            await first.stop()
            await second.stop()
    run(main())


def test_reply_matching_and_token():
    async def main():
        game = FakeGame(telemetry_port=free_udp_port(), token="a-secret-token").start()
        try:
            good = GameClient(Config(command_port=game.command_port, command_token="a-secret-token",
                                     telemetry_port=free_udp_port()).validate())
            bad = GameClient(Config(command_port=game.command_port, command_token="the-wrong-token",
                                    telemetry_port=free_udp_port(), command_timeout=0.3).validate())
            await good.start()
            await bad.start()
            results = await asyncio.gather(*[good.ping() for _ in range(10)])
            assert all(r["ok"] for r in results)
            assert len({r["request_id"] for r in results}) == 10
            hello = await good.hello(max_age=0)
            assert hello["ok"] and hello["hello"]["decision_events"] is True
            assert good.triggers.decision_events is True
            out = await bad.ping()
            assert out["error_reason"] == "no_reply"
            await good.stop()
            await bad.stop()
        finally:
            game.stop()
    run(main())


def test_listener_binds_by_itself_once_the_port_is_freed():
    async def main():
        port = free_udp_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", port))
        client = GameClient(Config(telemetry_port=port, command_port=free_udp_port()).validate())
        client.BIND_RETRY_S = 0.1
        await client.start()
        try:
            status = client.listener_status()
            assert not status["bound"] and "retrying_every_s" in status and status["server_pid"] > 0
            blocker.close()
            for _ in range(50):
                await asyncio.sleep(0.05)
                if client.listener_status()["bound"]:
                    break
            status = client.listener_status()
            assert status["bound"] and "error" not in status and status["bind_attempts"] >= 2
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(load_bin("session"), ("127.0.0.1", port))
            s.close()
            for _ in range(40):
                await asyncio.sleep(0.05)
                if client.listener_status()["packets_received"]:
                    break
            assert client.listener_status()["packets_received"] == 1
        finally:
            await client.stop()
            blocker.close()
    run(main())


# ------------------------------------------------------------------------------------------------ review fixes
# A scripted game: every command is answered by ``handler(command_id, request_id)``, which returns the datagrams to
# send back (built with the helpers below). Packets are crafted from the captures with a patched header.

import struct
import time

from backseat_champions_mcp import client as C
from backseat_champions_mcp import server as S

_REPLY_HEADER = struct.Struct("<HBB3BBIIf4B")


def _reply_header(pid):
    return _REPLY_HEADER.pack(1, pid, 1, 0, 88, 2, 0, 0, 0, 0.0, 255, 255, 255, 255)


def ack(cid, rid):
    return _reply_header(P.PKT_ACK) + struct.pack("<BI", cid, rid)


def error(cid, rid, reason):
    return _reply_header(P.PKT_ERROR) + struct.pack("<BIB", cid, rid, P.ERROR_REASONS.index(reason))


def hello(rid):
    return _reply_header(P.PKT_HELLO) + struct.pack("<I16sBBBHI", rid, b"0.88.2", 3, 15, 50, 7, 0)


def packet(name, uid=1, frame=1, st=1.0, version=None, key=None):
    data = bytearray(load_bin(name))
    struct.pack_into("<IIf", data, 8, uid, frame, st)
    if version is not None:
        data[3] = version
    if key is not None:
        data[P.HEADER_SIZE] = key
    return bytes(data)


class ScriptedGame(asyncio.DatagramProtocol):
    def __init__(self, handler):
        self.handler = handler
        self.received = []               # (command id, request id)
        self.addr = None
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        _, cid, _, rid, _ = struct.unpack_from("<HBBI48s", data)
        self.addr = addr
        self.received.append((cid, rid))
        for reply in self.handler(cid, rid) or ():
            self.transport.sendto(reply, addr)

    def sent(self, cid):
        return [rid for c, rid in self.received if c == cid]

    def send(self, data):
        self.transport.sendto(data, self.addr)


async def scripted(handler, **config):
    loop = asyncio.get_running_loop()
    _, game = await loop.create_datagram_endpoint(lambda: ScriptedGame(handler), local_addr=("127.0.0.1", 0))
    port = game.transport.get_extra_info("sockname")[1]
    config.setdefault("command_timeout", 0.3)
    client = GameClient(Config(command_port=port, telemetry_port=free_udp_port(), **config).validate())
    await client.start()
    return game, client


def acks_pings(cid, rid):
    return [ack(cid, rid)] if cid == P.CMD_PING else ([hello(rid)] if cid == P.CMD_HELLO else [])


def test_ack_answering_hello_is_protocol_error():
    async def main():
        game, client = await scripted(lambda cid, rid: [ack(cid, rid)] if cid == P.CMD_HELLO else [hello(rid)])
        try:
            first = await client.hello(max_age=10)
            assert first["ok"] is False and first["error_reason"] == "protocol_error"
            assert "Ack" in first["explanation"] and first["reply_kind"] == "ack"
            second = await client.hello(max_age=10)          # a wrong-kind hello is never cached
            assert second["error_reason"] == "protocol_error"
            ping = await client.ping()                       # a Hello answering a ping
            assert ping["error_reason"] == "protocol_error" and ping["reply_kind"] == "hello"
            assert client.dropped["wrong reply kind"] >= 3          # + the background hello at start
        finally:
            await client.stop()
            game.transport.close()
    run(main())


def test_ack_for_other_command_not_credited():
    async def main():
        def handler(cid, rid):
            if cid == P.CMD_PING:
                return [ack(P.CMD_SET_PIT, rid)]             # right id, wrong command
            if cid == P.CMD_SET_PIT:
                return [error(255, rid, "invalid_packet")]   # 255: too short to have one, still credited
            return []
        game, client = await scripted(handler)
        try:
            out = await client.ping()
            assert out["error_reason"] == "no_reply" and client.dropped["reply for another command"] == 1
            pit = await client.set_pit(True)
            assert pit["error_reason"] == "invalid_packet"
        finally:
            await client.stop()
            game.transport.close()
    run(main())


def test_late_error_from_replaced_hold_ignored():
    async def main():
        game, client = await scripted(acks_pings)
        try:
            first = await client.boost(P.BOOST_SPEED, 5.0)
            assert first["ok"]
            old_ids = list(game.sent(P.CMD_BOOST))
            second = await client.boost(P.BOOST_CORNERING, 5.0)
            assert second["ok"]
            game.send(error(P.CMD_BOOST, old_ids[-1], "refused"))    # late Error for the replaced hold
            await asyncio.sleep(0.1)
            hold = client.holds["boost"]
            assert hold.ended is None and hold.error is None
            assert client.dropped["error for no current hold"] == 1
            new_id = [rid for rid in game.sent(P.CMD_BOOST) if rid not in old_ids][-1]
            game.send(error(P.CMD_BOOST, new_id, "refused"))         # one the current hold sent: ends it
            await asyncio.sleep(0.1)
            assert hold.ended == "error" and hold.error["error_reason"] == "refused"
        finally:
            await client.stop()
            game.transport.close()
    run(main())


def test_silent_game_hold_sends_nothing():
    async def main():
        game, client = await scripted(lambda cid, rid: [])
        try:
            out = await client.boost(P.BOOST_SPEED, 2.0)
            assert out["ok"] is False and out["error_reason"] == "no_reply" and out["packets_sent"] == 0
            await asyncio.sleep(0.3)
            assert game.sent(P.CMD_BOOST) == [] and [c for c, _ in game.received if c == P.CMD_PING]
            assert client.hold_status()["boost"]["active"] is False
        finally:
            await client.stop()
            game.transport.close()
    run(main())


def test_no_reply_stateful_outcome_unknown():
    async def main():
        game, client = await scripted(lambda cid, rid: [])
        try:
            for call in (client.pick_card(0), client.ban_card(1), client.set_pit(True), client.pick_tire(1)):
                out = await call
                assert out["error_reason"] == "no_reply" and out["outcome_unknown"] is True
                assert "get_pending_decisions" in out["hint"] or "unknown" in out["hint"].lower()
            ping = await client.ping()
            assert ping["error_reason"] == "no_reply" and "outcome_unknown" not in ping
        finally:
            await client.stop()
            game.transport.close()
    run(main())


def test_hello_refresh_rate_limited():
    async def main():
        game, client = await scripted(acks_pings)
        try:
            await asyncio.sleep(0.2)
            base = len(game.sent(P.CMD_HELLO))                  # the hello sent at start
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for uid in range(1000, 1300):                       # 300 session changes in a burst
                s.sendto(packet("session", uid=uid), ("127.0.0.1", client.telemetry_port))
            s.close()
            await asyncio.sleep(1.6)
            hellos = len(game.sent(P.CMD_HELLO)) - base
            assert client.sessions_seen == 300
            assert 1 <= hellos <= 3, hellos                     # ~1/s, and the last change is still asked about
            assert client.dropped["hello refresh rate-limited"] > 0
        finally:
            await client.stop()
            game.transport.close()
    run(main())


async def _listener():
    client = GameClient(Config(telemetry_port=free_udp_port(), command_port=free_udp_port()).validate())
    await client.start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    async def send(*datagrams):
        for d in datagrams:
            sock.sendto(d, ("127.0.0.1", client.telemetry_port))
        await asyncio.sleep(0.1)
    return client, sock, send


def test_late_old_session_packet_ignored():
    async def main():
        client, sock, send = await _listener()
        try:
            await send(packet("session", uid=111), packet("session", uid=222), packet("lap_data", uid=222))
            assert client.session_uid == 222 and client.sessions_seen == 2
            await send(packet("motion", uid=111), packet("event", uid=111))      # late packets of session 111
            assert client.session_uid == 222 and client.sessions_seen == 2
            assert client.dropped["packet from an earlier session"] == 2
            assert client.latest(P.PKT_SESSION)[0]["session_uid"] == 222
            assert client.latest(P.PKT_LAP_DATA) is not None and client.latest(P.PKT_MOTION) is None
        finally:
            sock.close()
            await client.stop()
    run(main())


def test_older_frame_dropped_unless_stale():
    async def main():
        client, sock, send = await _listener()
        try:
            await send(packet("lap_data", frame=100), packet("lap_data", frame=50))
            assert client.latest(P.PKT_LAP_DATA)[0]["frame"] == 100
            assert client.dropped["older frame than the one kept"] == 1
            await asyncio.sleep(0.5)                            # the kept one is now >= 0.5 s old
            await send(packet("lap_data", frame=50))
            assert client.latest(P.PKT_LAP_DATA)[0]["frame"] == 50
            assert client.dropped["older frame than the one kept"] == 1
        finally:
            sock.close()
            await client.stop()
    run(main())


def test_packet_version_mismatch_dropped_and_warned():
    async def main():
        client, sock, send = await _listener()
        try:
            await send(packet("motion", version=2), packet("ai_observations", version=1), packet("session"))
            assert client.latest(P.PKT_MOTION) is None and client.keys(P.PKT_AI_OBSERVATIONS) == []
            status = client.listener_status()
            assert status["packet_version_mismatches"] == 2 and status["dropped"]["unknown packet_version"] == 2
            assert "2 telemetry packets dropped for an unknown packet_version" in status["warnings"][0]
            assert client.latest(P.PKT_SESSION) is not None
        finally:
            sock.close()
            await client.stop()
    run(main())


def test_layout_bump_with_new_size_counted_as_version_mismatch():
    """V-T1: Player v2 at another size and AI Observations v3 are version mismatches (count + warning)."""
    async def main():
        client, sock, send = await _listener()
        try:
            player2 = packet("player", version=2) + bytes(40)
            ai3 = packet("ai_observations", version=3, key=2) + bytes(8)
            bad_size = packet("session") + bytes(4)                     # known version, wrong size: malformed
            await send(packet("session"), player2, ai3, bad_size)
            assert client.latest(P.PKT_PLAYER, 0) is None and client.keys(P.PKT_AI_OBSERVATIONS) == []
            status = client.listener_status()
            assert status["packet_version_mismatches"] == 2
            assert status["dropped"]["not a Game API packet"] == 1
            assert "2 telemetry packets dropped for an unknown packet_version" in status["warnings"][0]
            assert "version 3" in status["warnings"][0]
        finally:
            sock.close()
            await client.stop()
    run(main())


def test_ai_obs_car_out_of_range_dropped():
    async def main():
        client, sock, send = await _listener()
        try:
            await send(packet("ai_observations", key=3), packet("ai_observations", key=16),
                       packet("ai_observations", key=255))
            assert client.keys(P.PKT_AI_OBSERVATIONS) == [3]
            assert client.dropped["car index out of range"] == 2
        finally:
            sock.close()
            await client.stop()
    run(main())


def test_wait_cap_default_55():
    assert Config().validate().wait_max_s == 55
    assert Config.from_env({}).validate().wait_max_s == 55
    assert Config.from_env({"BC_WAIT_MAX_S": "240"}).validate().wait_max_s == 60      # a hard ceiling
    assert Config.from_env({"BC_WAIT_MAX_S": "30"}).validate().wait_max_s == 30       # can only lower it
    assert Config(wait_max_s=3600).validate().wait_max_s == 60
    assert Config.from_env({"BC_WAIT_MAX_S": "240"}).validate().public()["wait_for_trigger_max_s"] == 60
    for bad in ("0", "-5", "nan", "abc"):
        with pytest.raises(ValueError):
            Config.from_env({"BC_WAIT_MAX_S": bad}).validate()


def test_cards_unavailable_keeps_card_event():
    client = GameClient(Config().validate())
    for reason, cleared in (("cards_unavailable", False), ("invalid_index", False), ("no_points", True), (None, True)):
        client.card_event = {"code": "CARD", "hand": []}
        result = {"ok": True} if reason is None else {"ok": False, "error_reason": reason}
        client.note_answered("pick_card", result, 0)
        assert (client.card_event is None) is cleared, reason


def test_log_level_case_insensitive(monkeypatch):
    assert S.parse_args(["--log-level", "info"]).log_level == "INFO"
    assert S.parse_args(["--log-level", " Debug "]).log_level == "DEBUG"
    monkeypatch.setenv("BC_LOG_LEVEL", "warning")
    assert S.parse_args([]).log_level == "WARNING"
    with pytest.raises(SystemExit):
        S.parse_args(["--log-level", "loud"])


def test_paused_while_session_time_frozen_and_status_still_arrives():
    async def main():
        client, sock, send = await _listener()
        try:
            assert client.paused() == {"paused": False, "session_time_frozen_for_s": 0.0, "session_time_s": None,
                                       "note": None, "stopped": True, "telemetry_age_s": None,
                                       "session_ended": False}
            await send(packet("session", st=10.0), packet("car_status", st=10.0))
            assert client.paused()["paused"] is False and client.paused()["stopped"] is False
            for _ in range(8):                                  # 2 Hz Session / Car Status, frozen clock
                await send(packet("session", st=10.5), packet("car_status", st=10.5),
                           packet("motion", st=10.45))          # a late datagram must not end the pause
                await asyncio.sleep(0.15)
            info = client.paused()
            assert info["paused"] is True and info["stopped"] is False
            assert info["session_time_frozen_for_s"] >= 1.5 and info["session_time_s"] == 10.5
            assert "paused" in info["note"] and "card_hand_pending" not in info
            assert "card" not in info["note"].lower() and "session_time not advancing" in info["note"]
            assert info["session_ended"] is False
            await send(packet("session", st=10.6))              # time moves again
            assert client.paused()["paused"] is False
        finally:
            sock.close()
            await client.stop()
    run(main())


def test_no_packets_for_3_s_is_stopped_not_paused():
    async def main():
        client, sock, send = await _listener()
        try:
            await send(packet("session", st=10.0), packet("car_status", st=10.0))
            await asyncio.sleep(3.1)
            info = client.paused()
            assert info["paused"] is False and info["stopped"] is True
            assert info["telemetry_age_s"] >= 3.0 and "No telemetry" in info["note"]
            assert "Car Status when Driver Data is on" in info["note"]          # V-T10
            assert not client.in_session()
        finally:
            sock.close()
            await client.stop()
    run(main())


# ------------------------------------------------------------------------------------------------ round 2 (V-T*)

def event_packet(code, seat=0, uid=1, frame=1, st=1.0, details=b""):
    data = bytearray(packet("event", uid=uid, frame=frame, st=st))
    body = code.encode("ascii") + bytes([255 if seat is None else seat]) + details
    data[P.HEADER_SIZE:] = body + bytes(len(data) - P.HEADER_SIZE - len(body))
    return bytes(data)


def test_paused_reports_session_ended_after_send_or_fini():
    """V-T4: a frozen clock after SEND / FINI (seat 0) is the session's end, not a pause; a new uid resets it."""
    async def main():
        for code, seat in (("SEND", None), ("FINI", 0)):
            client, sock, send = await _listener()
            try:
                await send(packet("session", st=10.0), event_packet(code, seat, st=10.0))
                assert client.session_end == code
                for _ in range(12):
                    await send(packet("session", st=10.0), packet("car_status", st=10.0))
                    await asyncio.sleep(0.05)
                info = client.paused()
                assert info["paused"] is False and info["session_ended"] is True and info["stopped"] is False
                assert info["session_time_frozen_for_s"] >= 1.5 and code in info["note"]
                await send(packet("session", uid=2, st=0.5))
                assert client.paused()["session_ended"] is False and client.session_end is None
            finally:
                sock.close()
                await client.stop()
        client, sock, send = await _listener()
        try:
            await send(packet("session", st=10.0), event_packet("FINI", 1, st=10.0))     # another seat finished
            assert client.session_end is None
        finally:
            sock.close()
            await client.stop()
    run(main())


def test_session_time_step_back_is_not_a_pause():
    """V-T5: a same-uid step back of < 5 s followed by a running clock is time moving, not a frozen one."""
    async def main():
        client, sock, send = await _listener()
        try:
            st = 20.0
            for _ in range(5):
                st += 0.1
                await send(packet("session", st=st))
            st -= 3.0                                           # the clock steps back 3 s and runs on
            for _ in range(20):
                st += 0.1
                await send(packet("session", st=st))           # 20 x 0.1 s wall clock: 2 s > PAUSE_AFTER_S
            info = client.paused()
            assert info["paused"] is False and info["session_time_frozen_for_s"] < 0.5, info
            assert info["session_time_s"] == round(st, 2)
            # then a freeze at the lower value is still a pause
            for _ in range(17):
                await send(packet("session", st=st))
            assert client.paused()["paused"] is True
        finally:
            sock.close()
            await client.stop()
    run(main())


def _ids_after(game, cid, rid):
    return [r for r in game.sent(cid) if r > rid]


def test_holds_end_with_release_on_send_fini_and_new_session():
    """V-T2: SEND, FINI for seat 0 and a new session_uid end boost / drive holds and send their release."""
    async def main():
        loop = asyncio.get_running_loop()
        for trigger in ("SEND", "FINI", "uid"):
            _, game = await loop.create_datagram_endpoint(_Recorder, local_addr=("127.0.0.1", 0))
            port = game.transport.get_extra_info("sockname")[1]
            client = GameClient(Config(command_port=port, telemetry_port=free_udp_port()).validate())
            await client.start()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(packet("session", uid=5, st=1.0), ("127.0.0.1", client.telemetry_port))
                await asyncio.sleep(0.1)
                assert (await client.boost(P.BOOST_SPEED, 5.0))["ok"]
                assert (await client.drive(0.5, 0.1, 5.0))["ok"]
                t_end = time.monotonic()
                if trigger == "uid":
                    sock.sendto(packet("session", uid=6, st=0.1), ("127.0.0.1", client.telemetry_port))
                else:
                    sock.sendto(event_packet(trigger, None if trigger == "SEND" else 0, uid=5, st=1.5),
                                ("127.0.0.1", client.telemetry_port))
                await asyncio.sleep(0.4)
                expected = "session_changed" if trigger == "uid" else "session_ended"
                assert client.holds["boost"].ended == expected and client.holds["drive"].ended == expected
                after = [(t, c, pl) for t, c, _, pl in game.log if t > t_end + 0.05]
                assert [c for _, c, _ in after if c == P.CMD_DRIVE] == [], trigger          # no drive after
                assert [pl[1] for _, c, pl in after if c == P.CMD_BOOST] == [], trigger     # no boost after
                before = [(c, pl) for t, c, _, pl in game.log if t >= t_end]
                assert (P.CMD_BOOST, bytes([0, 0])) in before, trigger                    # boost release
                assert P.CMD_RELEASE_DRIVE in [c for c, _ in before], trigger              # drive release
            finally:
                sock.close()
                await client.stop()
                game.transport.close()
    run(main())


class _Recorder(ScriptedGame):
    """ScriptedGame that keeps the arrival time and payload of every datagram; pings answered after ``delay``."""

    def __init__(self):
        super().__init__(lambda cid, rid: [hello(rid)] if cid == P.CMD_HELLO else [])
        self.log = []                    # (monotonic time, command id, request id, payload)
        self.delay = 0.0
        self.answer_pings = True

    def datagram_received(self, data, addr):
        _, cid, _, rid, _ = struct.unpack_from("<HBBI48s", data)
        self.log.append((time.monotonic(), cid, rid, data[56:]))
        super().datagram_received(data, addr)
        if cid == P.CMD_PING and self.answer_pings:
            asyncio.get_running_loop().call_later(self.delay, self.transport.sendto, ack(cid, rid), addr)


def test_replacing_a_hold_pings_first_and_swaps_without_a_gap():
    """V-T3: the running hold keeps sending while the ping is out; the new hold follows within one tick; a failed
    ping keeps the running hold."""
    async def main():
        loop = asyncio.get_running_loop()
        _, game = await loop.create_datagram_endpoint(_Recorder, local_addr=("127.0.0.1", 0))
        port = game.transport.get_extra_info("sockname")[1]
        client = GameClient(Config(command_port=port, telemetry_port=free_udp_port(), command_timeout=1.0).validate())
        await client.start()
        try:
            assert (await client.boost(P.BOOST_SPEED, 5.0))["ok"]
            await asyncio.sleep(0.3)
            first = client.holds["boost"]
            game.delay = 0.4
            out = await client.boost(P.BOOST_SPEED | P.BOOST_CORNERING, 5.0)
            assert out["ok"] and first.ended == "replaced"
            boosts = [(t, pl) for t, c, _, pl in game.log if c == P.CMD_BOOST]
            gaps = [b[0] - a[0] for a, b in zip(boosts, boosts[1:])]
            assert max(gaps) < 0.06, max(gaps)                  # 50 Hz: no hole while the ping was out
            masks = [pl[1] for _, pl in boosts]
            old_mask, new_mask = P.BOOST_SPEED, P.BOOST_SPEED | P.BOOST_CORNERING
            switch = masks.index(new_mask)
            assert set(masks[:switch]) == {old_mask} and set(masks[switch:]) == {new_mask}
            assert 0 not in masks                                # no release between the two holds
            # a lost ping: the running hold is kept and the failure reported
            second = client.holds["boost"]
            game.answer_pings = False
            out = await client.boost(P.BOOST_PARTY_MODE, 3.0)
            assert out["ok"] is False and out["error_reason"] == "no_reply" and out["kept_running_hold"] is True
            assert out["packets_sent"] == 0 and client.holds["boost"] is second and second.ended is None
            def held():
                return len([1 for _, c, _, pl in game.log if c == P.CMD_BOOST and pl[1] == new_mask])
            n = held()
            await asyncio.sleep(0.2)
            assert held() >= n + 5                               # the kept hold is still sending
            assert not [1 for _, c, _, pl in game.log if c == P.CMD_BOOST and pl[1] == P.BOOST_PARTY_MODE]
        finally:
            await client.stop()
            game.transport.close()
    run(main())


def test_stop_while_ping_out_cancels_the_start():
    async def main():
        loop = asyncio.get_running_loop()
        _, game = await loop.create_datagram_endpoint(_Recorder, local_addr=("127.0.0.1", 0))
        game.delay = 0.2
        port = game.transport.get_extra_info("sockname")[1]
        client = GameClient(Config(command_port=port, telemetry_port=free_udp_port()).validate())
        await client.start()
        try:
            task = asyncio.ensure_future(client.boost(P.BOOST_SPEED, 2.0))
            await asyncio.sleep(0.05)
            client.stop_hold("boost")
            out = await task
            assert out["ok"] is False and out["error_reason"] == "replaced" and out["packets_sent"] == 0
            assert not [1 for _, c, _, _ in game.log if c == P.CMD_BOOST]
        finally:
            await client.stop()
            game.transport.close()
    run(main())


def test_too_short_hold_refused():
    """V-T7: a hold below MIN_HOLD_S (or NaN) is bad_argument, sends nothing and leaves a running hold alone."""
    async def main():
        game, client = await scripted(acks_pings)
        try:
            assert (await client.boost(P.BOOST_SPEED, 2.0))["ok"]
            running = client.holds["boost"]
            for call in (client.boost(P.BOOST_SPEED, 1e-9), client.boost(P.BOOST_SPEED, 0.049),
                         client.boost(P.BOOST_SPEED, float("nan")), client.drive(1.0, -1.0, 0.0001),
                         client.drive(0.5, 0.0, 0.0)):
                pings = len(game.sent(P.CMD_PING))
                out = await call
                assert out["ok"] is False and out["error_reason"] == "bad_argument" and out["packets_sent"] == 0
                assert len(game.sent(P.CMD_PING)) == pings
            assert client.holds["boost"] is running and running.ended is None
            assert game.sent(P.CMD_DRIVE) == []
            assert (await client.drive(0.5, 0.0, C.MIN_HOLD_S))["ok"]
        finally:
            await client.stop()
            game.transport.close()
    run(main())


def test_pick_address_prefers_ipv4():
    v6 = (socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("::1", 29771, 0, 0))
    v4 = (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("127.0.0.1", 29771))
    assert C.pick_address([v6, v4]) == (socket.AF_INET, ("127.0.0.1", 29771))
    assert C.pick_address([v6]) == (socket.AF_INET6, ("::1", 29771, 0, 0))
    with pytest.raises(OSError):
        C.pick_address([])


def test_command_host_name_reaches_an_ipv4_only_game():
    """BC_COMMAND_HOST=localhost must reach a game listening on 127.0.0.1 even where localhost is ::1 first."""
    async def main():
        game, client = await scripted(acks_pings, command_host="localhost")
        try:
            family, address = await client._command_address()
            assert family == socket.AF_INET and address[0] == "127.0.0.1"
            assert (await client.ping())["ok"]
        finally:
            await client.stop()
            game.transport.close()
    run(main())


def _ipv6_loopback():
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.bind(("::1", 0))
        s.close()
        return True
    except OSError:
        return False


def test_command_host_ipv6_literal_kept():
    async def main():
        client = GameClient(Config(command_host="::1", command_port=29771, telemetry_port=free_udp_port()).validate())
        client._loop = asyncio.get_running_loop()
        assert await client._command_address() == (socket.AF_INET6, ("::1", 29771))
        client.config.command_host = "127.0.0.1"
        assert await client._command_address() == (socket.AF_INET, ("127.0.0.1", 29771))
        if not _ipv6_loopback():
            return                                   # no IPv6 here: the address choice above is what matters
        loop = asyncio.get_running_loop()
        _, game = await loop.create_datagram_endpoint(lambda: ScriptedGame(acks_pings), local_addr=("::1", 0))
        port = game.transport.get_extra_info("sockname")[1]
        client = GameClient(Config(command_host="::1", command_port=port, telemetry_port=free_udp_port(),
                                   command_timeout=0.3).validate())
        await client.start()
        try:
            assert (await client.ping())["ok"]
        finally:
            await client.stop()
            game.transport.close()
    run(main())
