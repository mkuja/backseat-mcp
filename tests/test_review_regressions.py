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
    assert data["results"] == [{"id": stamp, "bytes": os.path.getsize(folder / (stamp + ".results.json")),
                                "game_version": "0.86.2", "session_count": "2"}]
