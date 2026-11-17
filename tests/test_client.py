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
