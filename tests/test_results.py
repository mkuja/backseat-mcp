"""Race results receiver and store (R1-R6, R10 and the list / load_summary contract), in process on port 0."""
import concurrent.futures
import datetime
import errno
import http.client
import io
import json
import os
import socket
import stat
import struct
import sys
import threading
import time
import zipfile

import pytest

from backseat_champions_mcp import results as R
from backseat_champions_mcp.results import ResultsError, ResultsReceiver, ResultsStore

STEAM = "76561198123456789"           # > 2**53: must survive as a string


def car(pos, human=False, cid=0):
    return {"position": pos, "class_position": pos, "car_id": cid, "car_number": 10 + cid, "car_class": "FormulaCar",
            "name": "Human" if human else "AI %d" % cid, "driver": "human" if human else "ai",
            "steam_id": STEAM if human else None, "local_seat": 0 if human else None, "grid": pos, "laps": 10,
            "gap_to_leader_s": 0.0 if pos == 1 else 1.23456789 * pos, "best_lap_s": 61.987654321,
            "future_field": {"x": 1}}


def decision(cid, idx, typ="card"):
    return {"type": "decision", "car_id": cid, "car_type": 2, "decision_idx": idx, "decision_type": typ, "lap": 3,
            "race_time_s": 123.456789, "state": [0.1] * 8, "offered": [[0.5] * 4 for _ in range(3)], "chosen": 1,
            "chosen_by": "ml_agent", "policy": "p1",
            "outcome": {"finish_position": 2, "class_position": 2, "class_size": 8, "finish_score": 0.123456789,
                        "race_duration_s": 900.5}}


def header(race_id):
    return {"type": "header", "schema_version": 7, "schema_hash": 4294967295, "run_id": "run", "race_id": race_id,
            "game_version": "1.2.3", "unity_version": "6000.0.1f1", "timestamp_utc": "2026-11-17T10:00:00Z",
            "seed": 42, "car_type_setting": 1, "ai_difficulty": 2, "build": "retail", "demo": False,
            "model_bundle": "", "log_rev": 1, "context": [1.0, 2.0]}


def document(fmt=2, ndec=5):
    # finishing order deliberately not sorted by car_id
    race_cls = [car(1, cid=3), car(2, True, cid=0), car(3, cid=1), car(4, cid=2)]
    quali_cls = [car(1, True, cid=0), car(2, cid=3), car(3, cid=2), car(4, cid=1)]
    return {"format": fmt, "game_version": "1.2.3", "unity_version": "6000.0.1f1", "schema_version": 7,
            "schema_hash": 18446744073709551615, "timepoint": 1763370000000, "editor": False, "demo": True,
            "public_lobby": False, "host_steam_id": "76561198999999999", "new_top_field": [1, 2],
            "sessions": [
                {"kind": "qualifying", "track": "Monza_Scene", "started_at": 1, "ended_at": 2,
                 "classification": quali_cls, "race_id": None, "header": None, "decisions": []},
                {"kind": "race", "track": "Monza_Scene", "started_at": 3, "ended_at": 4,
                 "classification": race_cls, "race_id": "race-1", "header": header("race-1"),
                 "decisions": [decision(i % 4, i, ("card", "pit", "tire")[i % 3]) for i in range(ndec)]}]}


def headers(doc=None, token=None, zipped=False, fmt="2"):
    doc = doc or document()
    h = {"Content-Type": "application/zip" if zipped else "application/json", "X-BC-Format": fmt,
         "X-BC-Game-Version": doc.get("game_version", "x"), "X-BC-Session-Count": str(len(doc.get("sessions", [])))}
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def zip_of(entries, method=zipfile.ZIP_DEFLATED):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", method) as z:
        for name, data in entries:
            z.writestr(name, data)
    return b.getvalue()


def post(port, body, hdrs, method="POST", timeout=10):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.request(method, "/", body=body, headers=hdrs)
        resp = c.getresponse()
        resp.read()
        return resp.status
    finally:
        c.close()


def raw(port, data, timeout=5.0):
    """Send raw bytes; what came back (b"" = closed without an answer)."""
    s = socket.create_connection(("127.0.0.1", port), timeout)
    out = b""
    try:
        s.sendall(data)
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return out
            out += chunk
    except socket.timeout:
        return out + b"<<TIMEOUT>>"
    except ConnectionResetError:
        return out
    finally:
        s.close()


def status_of(answer):
    return int(answer.split(b" ", 2)[1]) if answer.startswith(b"HTTP/") else None


@pytest.fixture
def receiver(tmp_path):
    started = []

    def make(token="", keep=R.DEFAULT_KEEP, folder=None):
        rec = ResultsReceiver("127.0.0.1", 0, token, str(folder or tmp_path / "results"), keep)
        rec.start()
        assert rec.error is None, rec.error
        started.append(rec)
        return rec

    yield make
    for rec in started:
        rec.stop()


def put(folder, rid, data, suffix=None, headers=b"{}"):
    """Store a file by hand, as an older version would have (with its headers file: only then is it listed)."""
    (folder / (rid + (suffix or R.RESULT_SUFFIX))).write_bytes(data)
    (folder / (rid + R.HEADERS_SUFFIX)).write_bytes(headers)


def body_of(doc):
    return json.dumps(doc).encode("utf-8")


# ------------------------------------------------------------------------------------------------ R1

def test_stalled_connection_closed(receiver, monkeypatch):
    monkeypatch.setattr(R, "REQUEST_TIMEOUT_S", 0.5)
    rec = receiver()
    t0 = time.monotonic()
    # stalled in the request line / headers: closed by the socket timeout (before any token check)
    assert raw(rec.port, b"POST / HTTP/1.1\r\n", timeout=10) in (b"", )
    # stalled in the body: answered 408
    answer = raw(rec.port, b"POST / HTTP/1.1\r\nHost: x\r\nX-BC-Format: 2\r\nContent-Length: 1000\r\n\r\n{", timeout=10)
    assert status_of(answer) == 408
    assert time.monotonic() - t0 < 5
    # Content-Length longer than the body (the "CL +5" lie) gets an answer, not a hang
    body = body_of(document())
    req = (b"POST / HTTP/1.1\r\nHost: x\r\nX-BC-Format: 2\r\nContent-Length: %d\r\n\r\n" % (len(body) + 5)) + body
    assert status_of(raw(rec.port, req, timeout=10)) == 408


def test_connection_slots_bounded_and_queued(receiver, monkeypatch):
    monkeypatch.setattr(R, "REQUEST_TIMEOUT_S", 1.0)
    monkeypatch.setattr(R, "MAX_CONNECTIONS", 2)
    rec = receiver()
    stalled = [socket.create_connection(("127.0.0.1", rec.port)) for _ in range(2)]
    for s in stalled:
        s.sendall(b"POST / HTTP/1.1\r\n")
    time.sleep(0.3)
    assert rec._server._slots._value == 0
    # a real upload while all slots are taken waits for one (the stalled ones time out after 1 s)
    t0 = time.monotonic()
    assert post(rec.port, body_of(document()), headers(), timeout=20) == 200
    assert time.monotonic() - t0 < R.SLOT_WAIT_S + 1
    for s in stalled:
        s.close()


def test_saturated_receiver_closes_new_connections_fast(receiver, monkeypatch):
    monkeypatch.setattr(R, "REQUEST_TIMEOUT_S", 5.0)
    monkeypatch.setattr(R, "HEADER_DEADLINE_S", 5.0)
    monkeypatch.setattr(R, "SLOT_WAIT_S", 1.0)                # shorter than the stall: the saturated path
    monkeypatch.setattr(R, "MAX_CONNECTIONS", 2)
    rec = receiver()
    stalled = []
    for _ in range(2):
        s = socket.create_connection(("127.0.0.1", rec.port))
        s.sendall(b"POST / HTTP/1.1\r\n")
        stalled.append(s)
    time.sleep(0.3)
    t0 = time.monotonic()
    answers = [raw(rec.port, b"POST / HTTP/1.1\r\n", timeout=10) for _ in range(20)]
    assert all(a == b"" for a in answers)                     # closed, never a thread each
    assert time.monotonic() - t0 < R.SLOT_WAIT_S + 2           # waited once, then closed at once
    assert len([t for t in threading.enumerate() if "process_request_thread" in t.name]) <= 2
    for s in stalled:
        s.close()
    time.sleep(R.REQUEST_TIMEOUT_S + 0.5)                     # the stalled ones time out and free their slots
    assert post(rec.port, body_of(document()), headers()) == 200


def test_concurrent_uploads_all_accepted(receiver):
    rec = receiver(keep=0)
    body = body_of(document(ndec=0))
    with concurrent.futures.ThreadPoolExecutor(48) as ex:
        codes = list(ex.map(lambda i: post(rec.port, body, headers(), timeout=30), range(96)))
    assert codes.count(200) == 96
    assert len(rec.store.ids()) == 96


# ------------------------------------------------------------------------------------------------ R2

def test_zip_stored_compressed_and_pruned(receiver, tmp_path):
    assert R.MAX_PARSING == 1
    rec = receiver(keep=2)
    doc = document()
    body = body_of(doc)
    zipped = zip_of([("results.json", body + b" " * 200000)])
    assert post(rec.port, zipped, headers(doc, zipped=True)) == 200
    (rid,) = rec.store.ids()
    folder = tmp_path / "results"
    stored = folder / (rid + R.ZIP_SUFFIX)
    assert stored.read_bytes() == zipped                      # as received, not unpacked
    entry = rec.store.list(10)[0]
    assert entry["compressed"] is True and entry["bytes"] == len(zipped)
    assert rec.store.load_summary(rid)["sessions"][1]["race_id"] == "race-1"
    for _ in range(2):
        assert post(rec.port, body, headers(doc)) == 200
    ids = rec.store.ids()
    assert len(ids) == 2 and rid not in ids                   # oldest deleted
    assert not any(n.startswith(rid) for n in os.listdir(folder))
    assert all(n.endswith(R.RESULT_SUFFIX) or n.endswith(R.HEADERS_SUFFIX) for n in os.listdir(folder))


def test_keep_zero_keeps_all_and_start_prunes(tmp_path):
    folder = str(tmp_path / "r")
    store = ResultsStore(folder, keep=0)
    for i in range(5):
        store.save(b'{"i":%d}' % i, {}, [])
    assert len(store.ids()) == 5
    rec = ResultsReceiver("127.0.0.1", 0, "", folder, keep=3)
    rec.start()
    try:
        assert len(rec.store.ids()) == 3
    finally:
        rec.stop()


def test_unzip_bounded_and_no_extra_copy(monkeypatch):
    monkeypatch.setattr(R, "MAX_UNPACKED_BYTES", 1000)
    with pytest.raises(ValueError, match="unpacks to more"):
        R.parse_upload(zip_of([("results.json", b"{}" + b" " * 5000)]), True)
    raw_ = R.unzip_results(zip_of([("results.json", b"{}")]))
    assert isinstance(raw_, bytearray)                        # handed to json.loads as is


def test_save_positional_compat(tmp_path):
    store = ResultsStore(str(tmp_path / "r"))
    rid = store.save(b'{"t":1}', {}, [])                      # (body, headers, problems) as in 0.1.1
    assert rid.endswith("Z") and store.stored_as(rid) == "json"


# ------------------------------------------------------------------------------------------------ R3

def test_format3_recorded_and_refused_by_tool(receiver):
    rec = receiver()
    d3 = document(fmt=3)
    assert post(rec.port, body_of(d3), headers(d3, fmt="3")) == 200     # kept: nothing is retried
    entry = rec.store.list(1)[0]
    assert entry["format"] == 3
    assert any("unsupported format 3" in p for p in entry["problems"])
    assert any("X-BC-Format header is '3'" in p for p in entry["problems"])
    with pytest.raises(ResultsError) as e:
        rec.store.load_summary("latest")
    assert e.value.reason == "unsupported_format"
    assert e.value.details["format"] == 3 and e.value.details["id"] == entry["id"]
    assert e.value.explanation


def test_format_header_mismatch_and_bool_format(receiver):
    rec = receiver()
    assert post(rec.port, body_of(document()), headers(fmt="")) == 200
    assert any("X-BC-Format header" in p for p in rec.store.list(1)[0]["problems"])
    assert R.check_document({"format": True, "game_version": "x", "sessions": []})[0].startswith("unsupported")
    assert R.check_document({"format": 2.0, "game_version": "x", "sessions": []})


# ------------------------------------------------------------------------------------------------ R4

def test_ids_utc_and_sorted(tmp_path, monkeypatch):
    folder = tmp_path / "r"
    store = ResultsStore(str(folder))
    new = store.save(b'{"n":1}', {}, [])
    assert new.endswith("Z")
    assert abs(R.id_time(new) - time.time()) < 5
    # a legacy (0.1.1) local-time id from one hour ago, in a zone far from UTC, sorts below the new one
    if hasattr(time, "tzset"):
        monkeypatch.setenv("TZ", "XXX-3")
        time.tzset()
    try:
        legacy = datetime.datetime.fromtimestamp(time.time() - 3600).strftime("%Y%m%d-%H%M%S-%f")
        put(folder, legacy, b'{"old":1}')
        later_legacy = datetime.datetime.fromtimestamp(time.time() + 3600).strftime("%Y%m%d-%H%M%S-%f")
        put(folder, later_legacy, b'{"newer":1}')
        put(folder, "20261399-000000-000000", b"{}")    # id-shaped, not a time
        put(folder, "notes", b"{}")
        assert store.ids() == [later_legacy, new, legacy]
        assert store.resolve("latest") == later_legacy
        listed = store.list(10)
        assert [e["id"] for e in listed] == [later_legacy, new, legacy]
        received = datetime.datetime.fromisoformat(listed[2]["received_utc"])
        assert abs(received.timestamp() - (time.time() - 3600)) < 5
    finally:
        monkeypatch.delenv("TZ", raising=False)
        if hasattr(time, "tzset"):
            time.tzset()


# ------------------------------------------------------------------------------------------------ R5

def _encrypted(body):
    zb = bytearray(zip_of([("results.json", body)]))
    zb[6] |= 1
    cd = zb.rfind(b"PK\x01\x02")
    zb[cd + 8] |= 1
    return bytes(zb)


def _method99(body):
    zb = bytearray(zip_of([("results.json", body)]))
    cd = zb.rfind(b"PK\x01\x02")
    struct.pack_into("<H", zb, 8, 99)
    struct.pack_into("<H", zb, cd + 10, 99)
    return bytes(zb)


def _corrupt_deflate(body):
    zb = bytearray(zip_of([("results.json", body)]))
    zb[60:70] = b"\xff" * 10
    return bytes(zb)


def test_hostile_zips_get_422(receiver):
    rec = receiver(token="s3cret")
    doc = document()
    body = body_of(doc)
    J = headers(doc, "s3cret")
    Z = headers(doc, "s3cret", zipped=True)
    deep = b'{"format":2,"game_version":"a","sessions":[],"x":' + b"[" * 100000 + b"]" * 100000 + b"}"
    deep_small = b'{"format":2,"game_version":"a","sessions":[],"x":' + b"[" * 100 + b"]" * 100 + b"}"
    cases = {
        "zip encrypted flag": (_encrypted(body), Z),
        "zip method 99": (_method99(body), Z),
        "zip corrupt deflate": (_corrupt_deflate(body), Z),
        "zip garbage": (b"PK\x03\x04garbage", Z),
        "zip two entries": (zip_of([("results.json", body), ("x", b"1")]), Z),
        "zip json body": (body, Z),
        "deep nesting 100000": (deep, J),
        "deep nesting 100": (deep_small, J),
        "invalid utf8": (body[:-2] + b"\xff}", J),
        "JSON array": (b"[1,2]", J),
        "empty body": (b"", J),
    }
    for label, (b, h) in cases.items():
        assert post(rec.port, b, h) == 422, label
    assert post(rec.port, body, J) == 200
    assert post(rec.port, b"\xef\xbb\xbf" + body, J) == 200                 # UTF-8 BOM is readable
    assert post(rec.port, body, {**J, "Origin": "null"}) == 403
    assert post(rec.port, body, {k: v for k, v in J.items() if k != "X-BC-Format"}) == 403
    assert post(rec.port, body, {**J, "Authorization": "Bearer nope"}) == 401
    assert post(rec.port, body, J, method="GET") == 405
    assert post(rec.port, body, J, method="PUT") == 405
    assert len(rec.store.ids()) == 2


def test_bad_content_length_answered(receiver):
    rec = receiver()
    body = body_of(document())
    base = b"Host: x\r\nX-BC-Format: 2\r\n"

    def req(extra, payload=body):
        return b"POST / HTTP/1.1\r\n" + base + extra + b"\r\n" + payload

    assert status_of(raw(rec.port, req(b"Content-Length: -1\r\n"))) == 411
    assert status_of(raw(rec.port, req(b"Content-Length: 1_0\r\n"))) == 411
    assert status_of(raw(rec.port, req(b"Content-Length: +%d\r\n" % len(body)))) == 411
    assert status_of(raw(rec.port, req(b"Transfer-Encoding: chunked\r\n"))) == 411
    assert status_of(raw(rec.port, req(b"Content-Length: %d\r\nContent-Length: 3\r\n" % len(body)))) == 411
    assert status_of(raw(rec.port, req(b""))) == 411
    assert status_of(raw(rec.port, req(b"Content-Length: %d\r\n" % (R.MAX_UPLOAD_BYTES + 1), b""))) == 413
    assert status_of(raw(rec.port, req(b"Content-Length: %d\r\n" % len(body)))) == 200


def test_unexpected_handler_error_answers_500(receiver, monkeypatch):
    rec = receiver()

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(rec.store, "save", boom)
    assert post(rec.port, body_of(document()), headers()) == 500


def test_corrupt_stored_files_structured_error(tmp_path):
    folder = tmp_path / "r"
    store = ResultsStore(str(folder))
    store.makedirs()

    def latest_error():
        with pytest.raises(ResultsError) as e:
            store.load_summary("latest")
        return e.value

    put(folder, "20990101-000000-000000Z", b"not json")
    err = latest_error()
    assert err.reason == "stored_file_unreadable" and err.details["id"] == "20990101-000000-000000Z"
    os.remove(folder / ("20990101-000000-000000Z" + R.RESULT_SUFFIX))
    os.mkdir(folder / ("20990101-000000-000001Z" + R.RESULT_SUFFIX))
    (folder / ("20990101-000000-000001Z" + R.HEADERS_SUFFIX)).write_bytes(b"{}")
    assert latest_error().reason == "stored_file_unreadable"
    os.rmdir(folder / ("20990101-000000-000001Z" + R.RESULT_SUFFIX))
    if hasattr(os, "symlink") and hasattr(os, "O_NOFOLLOW"):
        target = tmp_path / "elsewhere.json"
        target.write_bytes(json.dumps(document()).encode())
        os.symlink(target, folder / ("20990101-000000-000002Z" + R.RESULT_SUFFIX))
        (folder / ("20990101-000000-000002Z" + R.HEADERS_SUFFIX)).write_bytes(b"{}")
        assert latest_error().reason == "stored_file_unreadable"
        os.remove(folder / ("20990101-000000-000002Z" + R.RESULT_SUFFIX))
    deep = '{"format":2,"game_version":"a","sessions":[{"classification":[{"x":%s}]}]}' % ("[" * 1500 + "]" * 1500)
    put(folder, "20990101-000000-000003Z", deep.encode())        # e.g. stored by 0.1.1
    assert latest_error().reason == "stored_file_unreadable"
    os.remove(folder / ("20990101-000000-000003Z" + R.RESULT_SUFFIX))
    put(folder, "20990101-000000-000004Z", b"[1]")
    assert latest_error().reason == "stored_file_unreadable"
    with pytest.raises(ResultsError) as e:
        store.load_summary("../etc/passwd")
    assert e.value.reason == "bad_argument"


# ------------------------------------------------------------------------------------------------ R6

def test_backlog_64(receiver):
    assert R.LISTEN_BACKLOG == 64 and R._Server.request_queue_size == 64
    assert R._Server6.address_family == socket.AF_INET6
    rec = receiver()
    assert rec._server.request_queue_size == 64


# ------------------------------------------------------------------------------------------------ R10

@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes")
def test_files_private(receiver, tmp_path):
    folder = tmp_path / "pre"
    folder.mkdir(mode=0o755)
    os.chmod(folder, 0o755)
    rec = receiver(folder=folder)
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700           # existing folder made private
    assert post(rec.port, body_of(document()), headers()) == 200
    assert post(rec.port, zip_of([("results.json", body_of(document()))]), headers(zipped=True)) == 200
    names = os.listdir(folder)
    assert len(names) == 4
    for n in names:
        assert stat.S_IMODE(os.stat(folder / n).st_mode) == 0o600, n
    fresh = tmp_path / "a" / "b"
    ResultsStore(str(fresh)).save(b"{}", {}, [])
    assert stat.S_IMODE(os.stat(fresh).st_mode) == 0o700


# ------------------------------------------------------------------------------------------------ contract

def test_list_contract(receiver):
    rec = receiver()
    doc = document()
    assert post(rec.port, body_of(doc), headers(doc)) == 200
    assert post(rec.port, b'{"format":2,"game_version":"x"}', headers(doc)) == 200
    listed = rec.store.list(10)
    assert len(listed) == 2 and len(rec.store.list(1)) == 1
    newest, older = listed
    for e in listed:
        assert {"id", "received_utc", "bytes", "compressed", "format", "game_version", "session_count",
                "problems"} <= set(e)
        assert e["id"].endswith("Z")
        datetime.datetime.fromisoformat(e["received_utc"])
        assert e["compressed"] is False and isinstance(e["bytes"], int)
        assert e["format"] == 2 and e["game_version"] == "1.2.3"
        assert isinstance(e["session_count"], int) and e["session_count"] == 2
        assert isinstance(e["problems"], list)
    assert older["problems"] == []
    assert newest["problems"] == ["missing sessions"]
    json.dumps(listed, allow_nan=False)


def test_list_reads_only_headers(tmp_path):
    store = ResultsStore(str(tmp_path / "r"))
    rid = store.save(b"{ not json", {"x-bc-session-count": "two"}, [])
    (e,) = store.list(5)
    assert e["id"] == rid and e["session_count"] is None and e["format"] is None


def test_load_summary_contract(receiver):
    rec = receiver()
    doc = document()
    doc["sessions"][1]["classification"][0]["best_lap_s"] = float("nan")
    body = json.dumps(doc).encode()
    assert post(rec.port, body, headers(doc)) == 200
    out = rec.store.load_summary("latest")
    json.dumps(out, allow_nan=False)
    assert out["ok"] is True and out["id"] == rec.store.ids()[0]
    for key in ("format", "game_version", "unity_version", "schema_version", "schema_hash", "timepoint", "editor",
                "demo", "public_lobby"):
        assert out[key] == doc[key], key
    assert out["schema_hash"] == 18446744073709551615
    assert "host_steam_id" not in out
    assert [s["kind"] for s in out["sessions"]] == ["qualifying", "race"]
    race = out["sessions"][1]
    assert [r["car_id"] for r in race["classification"]] == [3, 0, 1, 2]       # order kept as uploaded
    assert all("steam_id" not in r for s in out["sessions"] for r in s["classification"])
    row = race["classification"][1]
    assert row["gap_to_leader_s"] == 1.23456789 * 2 and row["future_field"] == {"x": 1}    # not rounded
    assert race["classification"][0]["best_lap_s"] is None                      # NaN -> null
    assert race["header"]["schema_version"] == 7 and race["header"]["unity_version"] == "6000.0.1f1"
    assert out["sessions"][0]["header"] is None
    assert race["decisions_count"] == 5
    assert race["decisions_by_type"] == {"card/ml_agent": 2, "pit/ml_agent": 2, "tire/ml_agent": 1}
    assert "decisions" not in race
    assert "problems" not in out

    full = rec.store.load_summary(out["id"], include_decisions=True, max_decisions=2, include_steam_ids=True)
    assert full["host_steam_id"] == "76561198999999999"
    human = [r for r in full["sessions"][1]["classification"] if r["driver"] == "human"][0]
    assert human["steam_id"] == STEAM
    assert full["sessions"][1]["classification"][0]["steam_id"] is None
    decisions = full["sessions"][1]["decisions"]
    assert len(decisions) == 2 and full["sessions"][1].get("decisions_truncated") is True
    assert "state" not in decisions[0] and "offered" not in decisions[0]
    assert decisions[0]["outcome"]["finish_score"] == 0.123456789 and decisions[0]["race_time_s"] == 123.456789
    assert full["sessions"][0]["decisions"] == []


def test_load_summary_numeric_steam_ids_stringified(tmp_path):
    store = ResultsStore(str(tmp_path / "r"))
    doc = document()
    doc["host_steam_id"] = 76561198999999999
    doc["sessions"][1]["classification"][1]["steam_id"] = 76561198123456789
    store.save(json.dumps(doc).encode(), {}, [])
    out = store.load_summary("latest", include_steam_ids=True)
    assert out["host_steam_id"] == "76561198999999999"
    assert out["sessions"][1]["classification"][1]["steam_id"] == "76561198123456789"


def test_load_summary_zip_and_problems_passthrough(tmp_path):
    store = ResultsStore(str(tmp_path / "r"))
    doc = document()
    rid = store.save(zip_of([("results.json", json.dumps(doc).encode())]), {}, ["X-BC-Format header is '', not 2"],
                     zipped=True)
    out = store.load_summary(rid)
    assert out["problems"] == ["X-BC-Format header is '', not 2"]
    assert out["sessions"][1]["race_id"] == "race-1"


# ------------------------------------------------------------------------------------------------ round 2: deadlines

def _trickle(port, first, line, every, stop_after=15.0):
    """Send ``first`` then ``line`` every ``every`` s; (seconds until the server closed / answered, answer)."""
    s = socket.create_connection(("127.0.0.1", port))
    s.settimeout(every)
    t0 = time.monotonic()
    got = b""
    try:
        s.sendall(first)
        while time.monotonic() - t0 < stop_after:
            try:
                chunk = s.recv(65536)
                if not chunk:
                    break
                got += chunk
            except socket.timeout:
                try:
                    s.sendall(line)
                except OSError:
                    break
            except OSError:
                break
        return time.monotonic() - t0, got
    finally:
        s.close()


def test_body_deadline_numbers():
    assert R.HEADER_DEADLINE_S == 10 and R.REQUEST_TIMEOUT_S == 30 and R.MAX_CONNECTIONS == 16
    assert R.SLOT_WAIT_S > R.HEADER_DEADLINE_S
    assert R.body_deadline(0) == 30 and R.body_deadline(3 * 1024 * 1024) == 30
    assert R.body_deadline(8 * 1024 * 1024) == 64
    assert R.body_deadline(R.MAX_UPLOAD_BYTES) == 120


def test_header_trickle_closed_at_header_deadline(receiver, monkeypatch):
    monkeypatch.setattr(R, "HEADER_DEADLINE_S", 1.0)          # REQUEST_TIMEOUT_S stays 30: each line resets nothing
    rec = receiver()
    took, answer = _trickle(rec.port, b"POST / HTTP/1.1\r\n", b"X-A: b\r\n", 0.2)
    assert answer == b"" and took < 3


def test_body_trickle_answered_408_at_body_deadline(receiver, monkeypatch):
    monkeypatch.setattr(R, "BODY_DEADLINE_MIN_S", 1.0)
    rec = receiver()
    took, answer = _trickle(rec.port, b"POST / HTTP/1.1\r\nHost: x\r\nX-BC-Format: 2\r\nContent-Length: 1000\r\n\r\n{",
                            b" ", 0.2)
    assert status_of(answer) == 408 and took < 3
    assert rec.store.ids() == []


def test_header_tricklers_cannot_block_upload(receiver, monkeypatch):
    monkeypatch.setattr(R, "HEADER_DEADLINE_S", 1.5)
    monkeypatch.setattr(R, "SLOT_WAIT_S", 2.5)
    rec = receiver()
    stop = threading.Event()

    def trickler():
        s = socket.create_connection(("127.0.0.1", rec.port))
        try:
            s.sendall(b"POST / HTTP/1.1\r\n")
            while not stop.is_set():
                time.sleep(0.2)
                s.sendall(b"X-A: b\r\n")
        except OSError:
            pass
        finally:
            s.close()

    threads = [threading.Thread(target=trickler, daemon=True) for _ in range(R.MAX_CONNECTIONS)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    assert rec._server._slots._value == 0
    t0 = time.monotonic()
    assert post(rec.port, body_of(document()), headers(), timeout=20) == 200
    assert time.monotonic() - t0 < R.HEADER_DEADLINE_S + 1
    stop.set()
    for t in threads:
        t.join(5)


# ------------------------------------------------------------------------------------------------ round 2: atomic saves

def _failing_write(monkeypatch, folder, allow):
    """os.write into files of ``folder`` writes ``allow`` bytes in all, then fails with ENOSPC (Linux /proc)."""
    real = os.write
    left = [allow]

    def fake(fd, data):
        try:
            target = os.readlink("/proc/self/fd/%d" % fd)
        except OSError:
            target = ""
        if not target.startswith(str(folder)):
            return real(fd, data)
        if left[0] <= 0:
            raise OSError(errno.ENOSPC, "No space left on device")
        n = real(fd, bytes(data[:left[0]]))
        left[0] -= n
        return n

    monkeypatch.setattr(os, "write", fake)


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc to pick the store's fds")
@pytest.mark.parametrize("allow", [0, 10, 200, 5000])        # in the headers file, or part way through the body
def test_enospc_leaves_nothing_and_latest_stays(receiver, tmp_path, monkeypatch, allow):
    rec = receiver()
    folder = tmp_path / "results"
    good = body_of(document())
    assert post(rec.port, good, headers()) == 200
    (good_id,) = rec.store.ids()
    before = sorted(os.listdir(folder))
    _failing_write(monkeypatch, folder, allow)
    big = body_of(document(ndec=200))
    assert post(rec.port, big, headers()) == 500
    with pytest.raises(OSError):
        rec.store.save(big, {}, [])
    monkeypatch.undo()
    assert sorted(os.listdir(folder)) == before                # no partial, empty or temp file left
    assert rec.store.ids() == [good_id]
    assert rec.store.load_summary("latest")["id"] == good_id
    assert post(rec.port, good, headers()) == 200              # and saving works again


def test_failed_rename_cleans_up(tmp_path, monkeypatch):
    store = ResultsStore(str(tmp_path / "r"))
    first = store.save(b'{"a":1}', {}, [])
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 2:                                   # the results file, after its headers file
            raise OSError(errno.EIO, "I/O error")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        store.save(b'{"b":2}', {}, [])
    monkeypatch.undo()
    assert sorted(os.listdir(tmp_path / "r")) == [first + R.HEADERS_SUFFIX, first + R.RESULT_SUFFIX]
    assert store.ids() == [first]


def test_concurrent_writers_and_readers_never_unreadable(receiver):
    rec = receiver(keep=5)
    body = body_of(document(ndec=50))
    assert post(rec.port, body, headers()) == 200
    errors = []
    stop = threading.Event()
    counts = {"ok": 0}

    def reader():
        while not stop.is_set():
            try:
                rec.store.list(20)
                rec.store.load_summary("latest")
                counts["ok"] += 1
            except ResultsError as e:
                if e.reason != "bad_argument":            # an id pruned under a reader: not "unreadable"
                    errors.append(e.explanation)
            except Exception as e:                        # pragma: no cover - the test fails on it
                errors.append(repr(e))

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda i: post(rec.port, body, headers(), timeout=30), range(80)))
    stop.set()
    for t in readers:
        t.join()
    assert codes.count(200) == 80
    assert errors == [] and counts["ok"] > 0
    names = os.listdir(rec.store.folder)
    assert len(rec.store.ids()) == 5 and len(names) == 10
    assert not any(n.endswith(".tmp") for n in names)


def test_incomplete_entries_ignored_and_leftovers_pruned(tmp_path):
    folder = tmp_path / "r"
    store = ResultsStore(str(folder))
    good = store.save(b'{"format":2}', {}, [])
    (folder / ("20990101-000000-000000Z" + R.RESULT_SUFFIX)).write_bytes(b"{")       # no headers file
    orphan = folder / ("20990101-000000-000001Z" + R.HEADERS_SUFFIX)                   # no results file
    orphan.write_bytes(b"{}")
    temp = folder / (".20990101-000000-000002Z.%s.tmp" % ("ab" * 8))
    temp.write_bytes(b"partial")
    fresh_temp = folder / (".20990101-000000-000003Z.%s.tmp" % ("cd" * 8))
    fresh_temp.write_bytes(b"partial")
    assert store.ids() == [good] and store.resolve("latest") == good
    assert [e["id"] for e in store.list(10)] == [good]
    old = time.time() - R.STALE_TEMP_S - 10
    os.utime(orphan, (old, old))
    os.utime(temp, (old, old))
    store.prune()
    assert not orphan.exists() and not temp.exists()
    assert fresh_temp.exists()                                 # may be a save in progress
    assert (folder / ("20990101-000000-000000Z" + R.RESULT_SUFFIX)).exists()          # data is never deleted


@pytest.mark.skipif(not hasattr(os, "symlink") or not hasattr(os, "O_NOFOLLOW"), reason="POSIX symlinks")
def test_headers_and_size_do_not_follow_symlinks(tmp_path):
    folder = tmp_path / "r"
    store = ResultsStore(str(folder))
    rid = store.save(b'{"format":2}', {"x-bc-game-version": "1.2.3"}, [])
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"x-bc-game-version": "SECRET", "problems": ["leak"]}) + " " * 5000)
    fake = "20200101-000000-000000Z"
    os.symlink(secret, folder / (fake + R.RESULT_SUFFIX))
    os.symlink(secret, folder / (fake + R.HEADERS_SUFFIX))
    assert store.load_headers(fake) == {}
    assert store.size(fake) is None
    listed = {e["id"]: e for e in store.list(10)}
    assert listed[fake]["game_version"] is None and listed[fake]["problems"] == [] and listed[fake]["bytes"] is None
    assert "SECRET" not in json.dumps(listed)
    assert listed[rid]["game_version"] == "1.2.3" and listed[rid]["bytes"] == len(b'{"format":2}')
    with pytest.raises(ResultsError) as e:
        store.load_summary(fake)
    assert e.value.reason == "stored_file_unreadable"


def test_folder_tilde_and_relative_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert ResultsStore("~/bc").folder == str(tmp_path / "home" / "bc")
    monkeypatch.chdir(tmp_path)
    store = ResultsStore("rel/dir")
    assert store.folder == str(tmp_path / "rel" / "dir") and os.path.isabs(store.folder)
    monkeypatch.chdir("/")
    store.save(b"{}", {}, [])                                  # still the folder resolved at start
    assert (tmp_path / "rel" / "dir").is_dir()
