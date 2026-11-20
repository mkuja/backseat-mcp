"""End to end through the REAL server process over stdio (raw JSON-RPC, tests/e2e_rpc.py) against tests/fake_game.py,
for the behaviour seen live in 0.88.2 (FINDINGS L1-L3) and the plan critique's additional tests (PLAN X1-X11).

Time constants are scaled through the environment: BC_REMIND_S=1.0 stands for the 20 s resend interval, so "60 s"
of the plan is 3 s here. Only free ephemeral UDP ports and tmp_path are used.
"""
import asyncio
import contextlib
import json
import os
import time

from conftest import ROOT, SRC, free_udp_port
from e2e_rpc import RpcServer
from fake_game import FakeGame

REMIND = 1.0          # BC_REMIND_S used by these tests (the real value is 20 s)
MAX_RESEND = 3
MY_PID = None         # filled from the template (player_id of my car)
STOPPED_WORDS = ("telemetry has stopped", "has stopped", "stopped (")


def run(coro, limit=100):
    return asyncio.run(asyncio.wait_for(coro, limit))


@contextlib.contextmanager
def fake_game(**kw):
    """The fake game, with no hand on offer at the start (tests offer one with offer())."""
    port = free_udp_port()
    game = FakeGame(telemetry_port=port, rate_hz=kw.pop("rate_hz", 25), **kw)
    game.points, game.hand = 0, []
    game.start()
    try:
        yield game, port
    finally:
        game.stop()


# BC_E2E_SRC: run these tests against another source tree (e.g. an older version, to see them fail there)
SERVER_SRC = os.environ.get("BC_E2E_SRC") or SRC


def base_env(port, game, **extra):
    env = {"PYTHONPATH": SERVER_SRC, "BC_TELEMETRY_PORT": str(port), "BC_COMMAND_PORT": str(game.command_port),
           "BC_COMMAND_TIMEOUT": "0.5", "BC_PROGRESS_INTERVAL_S": "0.3", "BC_LOG_LEVEL": "WARNING",
           "BC_REMIND_S": str(REMIND), "BC_WAIT_MAX_S": "3"}
    env.update({k: str(v) for k, v in extra.items()})
    return env


@contextlib.asynccontextmanager
async def served(tmp_path, game, port, name="server", **extra):
    srv = RpcServer(base_env(port, game, **extra), ROOT, str(tmp_path / ("%s.stderr" % name)))
    try:
        await srv.start()
        yield srv
    finally:
        await srv.close()
        err = srv.stderr_text()
        assert "Traceback" not in err, err[-3000:]


async def until(fn, timeout=6.0, step=0.1, what="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await fn()
        if last:
            return last
        await asyncio.sleep(step)
    raise AssertionError("%s not met within %.1f s (last: %r)" % (what, timeout, last))


async def receiving(srv, participants=True):
    async def ok():
        st = await srv.call("get_api_status")
        t = st.get("telemetry") or {}
        return t.get("receiving") and (not participants or (t.get("packets_by_type") or {}).get("participants"))
    await until(ok, 8, what="telemetry arriving")


async def wait(srv, timeout_s=None, since_seq=None):
    args = {}
    if timeout_s is not None:
        args["timeout_s"] = timeout_s
    if since_seq is not None:
        args["since_seq"] = since_seq
    r = await srv.call("wait_for_trigger", args)
    assert not r["_is_error"] and r.get("ok"), r
    return r


def of_type(r, kind):
    return [t for t in r.get("triggers", []) if t["type"] == kind]


async def first_wait(srv, since_seq=0):
    """The first wait of a process ignores since_seq (X4); spend it so later calls behave as acks."""
    return await wait(srv, 0.3, since_seq)


HAND = [[13, 6, 0, 0], [0, 0, 0, 0], [11, 3, 0, 0]]


def offer(game, points=1, hand=None):
    """A new hand appears (Player packet) and the CARD event announces it."""
    game.points, game.hand = points, [list(c) for c in (hand or HAND)]
    game.card_event()


def my_pid(game):
    return game.truth(game.my_car)["player_id"]


# ------------------------------------------------------------------------------------- 1. L1 no-cancel abandon

def test_l1_abandoned_call_without_cancel_resends(tmp_path):
    async def main():
        with fake_game() as (game, port):
            async with served(tmp_path, game, port) as srv:
                await receiving(srv)
                r0 = await first_wait(srv, 0)
                last = r0["last_seq"]
                rid = await srv.abandon("wait_for_trigger", {"timeout_s": 3, "since_seq": last})
                await asyncio.sleep(0.3)
                offer(game)
                await until(lambda: _ready(srv, rid), 4, what="abandoned call finished server-side")
                dropped = srv.answer_of(rid)
                assert of_type(dropped, "card_offer"), dropped            # the result the client never saw
                seq = of_type(dropped, "card_offer")[0]["seq"]
                # the bridge cut the call at 80 s, i.e. long after REMIND_S: re-send the old since_seq
                await asyncio.sleep(REMIND + 0.2)
                r1 = await wait(srv, 3, last)
                cards = of_type(r1, "card_offer")
                assert cards and cards[0]["seq"] == seq and cards[0]["resent"] is True, r1
                assert r1["_elapsed"] < 0.8, r1["_elapsed"]
                # without since_seq (lost track): comes again within REMIND_S + 1
                r2 = await wait(srv, 3)
                cards = of_type(r2, "card_offer")
                assert cards and cards[0]["seq"] == seq and cards[0]["resent"] is True, r2
                assert r2["_elapsed"] <= REMIND + 1.0, r2["_elapsed"]
                p = await srv.call("pick_card", {"index": 0, "seq": seq})
                assert p.get("ok"), p
                r3 = await wait(srv, 2, r2["last_seq"])
                assert r3["status"] == "timeout" and not r3["triggers"], r3
                assert r3["_elapsed"] >= 1.8, r3["_elapsed"]
    run(main())


async def _ready(srv, rid):
    return srv.answer_of(rid) is not None


# ------------------------------------------------------------------------------------- 2. stale since_seq stuck

def test_stale_since_seq_bounded_resends_no_busy_loop(tmp_path):
    async def main():
        with fake_game() as (game, port):
            async with served(tmp_path, game, port) as srv:
                await receiving(srv)
                stale = (await first_wait(srv, 0))["last_seq"]
                offer(game)
                r = await wait(srv, 3, stale)
                seq = of_type(r, "card_offer")[0]["seq"]
                # the model never advances since_seq and never answers: plan "60 s" of polling = 3*REMIND here x2
                returns, calls = [], 0
                t_end = time.monotonic() + 6 * REMIND
                while time.monotonic() < t_end:
                    t0 = time.monotonic()
                    x = await wait(srv, 0.4, stale)
                    calls += 1
                    if x["triggers"]:
                        assert [t["seq"] for t in x["triggers"]] == [seq], x
                        assert x["triggers"][0]["resent"] is True
                        returns.append(t0 + x["_elapsed"])
                    else:
                        assert x["status"] == "timeout" and x["_elapsed"] >= 0.35, x   # blocked, no busy loop
                    assert x.get("open_decisions"), x                              # still reported, no early return
                assert 1 <= len(returns) <= MAX_RESEND, returns
                gaps = [b - a for a, b in zip([time.monotonic() - 1e9] + returns, returns)][1:]
                assert all(g >= REMIND * 0.9 for g in gaps), gaps
                assert calls >= 8, calls
                # answered: the same stale since_seq never brings it back
                p = await srv.call("pick_card", {"index": 0, "seq": seq})
                assert p.get("ok"), p
                t_end = time.monotonic() + 2.5 * REMIND
                while time.monotonic() < t_end:
                    x = await wait(srv, 0.5, stale)
                    assert x["status"] == "timeout" and not x["triggers"], x
                    assert x["_elapsed"] >= 0.45, x["_elapsed"]
    run(main(), 60)


# ------------------------------------------------------------------------------------- 3. puncture result dropped

def _punctures(r):
    return [t for t in of_type(r, "car_damaged") if t.get("signal") == "puncture"]


def test_puncture_result_dropped_resent_once_with_since_seq(tmp_path):
    async def main():
        with fake_game() as (game, port):
            async with served(tmp_path, game, port) as srv:
                await receiving(srv)
                stale = (await first_wait(srv, 0))["last_seq"]
                game.puncture = True
                r = await wait(srv, 3, stale)
                got = _punctures(r)
                assert len(got) == 1 and not got[0]["resent"], r
                seq = got[0]["seq"]
                seen = []
                t_end = time.monotonic() + 4 * REMIND
                while time.monotonic() < t_end:
                    x = await wait(srv, 0.5, stale)              # the result above was lost: old since_seq again
                    seen += [(t["seq"], t["resent"]) for t in _punctures(x)]
                assert seen == [(seq, True)], seen
    run(main(), 40)


def test_puncture_result_dropped_never_resent_without_since_seq(tmp_path):
    async def main():
        with fake_game() as (game, port):
            async with served(tmp_path, game, port) as srv:
                await receiving(srv)
                await wait(srv, 0.3)
                game.puncture = True
                r = await wait(srv, 3)
                assert len(_punctures(r)) == 1, r
                seen = []
                t_end = time.monotonic() + 4 * REMIND
                while time.monotonic() < t_end:
                    x = await wait(srv, 0.5)
                    seen += _punctures(x)
                assert seen == [], seen
    run(main(), 40)


# ------------------------------------------------------------------------------------- 4. L3 pause

def _texts(obj):
    return json.dumps(obj).lower()


def test_l3_pause_on_hand_reported_as_paused(tmp_path):
    async def main():
        with fake_game() as (game, port):
            async with served(tmp_path, game, port) as srv:
                await receiving(srv)
                last = (await first_wait(srv, 0))["last_seq"]
                game.auto_pause_on_hand = True
                offer(game)
                r = await wait(srv, 3, last)
                cards = of_type(r, "card_offer")
                assert cards, r
                seq = cards[0]["seq"]
                await asyncio.sleep(2.6)                      # past PAUSE_AFTER_S and Lap Data's stale limit
                assert game.paused
                st = await srv.call("get_api_status")
                p = st["telemetry"]["paused"]
                assert p and p["paused"] is True and p.get("card_hand_pending") is True, p
                warnings = " ".join(st.get("warnings") or []).lower()
                assert "have not arrived recently" not in warnings, st.get("warnings")
                assert not any(w in warnings for w in STOPPED_WORDS), st.get("warnings")
                for tool, args in (("get_standings", {}), ("get_car_telemetry", {}), ("get_car_motion", {}),
                                   ("get_player_state", {}), ("get_race_snapshot", {}),
                                   ("get_car_abilities", {}), ("get_pending_decisions", {})):
                    x = await srv.call(tool, args)
                    assert not x["_is_error"], (tool, x)
                    text = _texts(x)
                    assert not any(w in text for w in STOPPED_WORDS), (tool, x)
                    if x.get("stale"):
                        assert x.get("paused") is True and "paused" in x.get("stale_note", ""), (tool, x)
                x = await wait(srv, 1, r["last_seq"])
                assert x["status"] == "timeout" and x.get("paused", {}).get("paused") is True, x
                assert x.get("open_decisions"), x
                p = await srv.call("pick_card", {"index": 0, "seq": seq})
                assert p.get("ok"), p
                await until(lambda: _not_paused(game), 2, what="game resumed")
                t_end = time.monotonic() + 3 * REMIND
                while time.monotonic() < t_end:
                    x = await wait(srv, 1, r["last_seq"])
                    assert not x["triggers"], x
                    assert x["status"] == "timeout", x

                async def unpaused():
                    s = await srv.call("get_api_status")
                    return not s["telemetry"]["paused"]
                await until(unpaused, 4, what="server sees the game running again")
    run(main(), 60)


async def _not_paused(game):
    return not game.paused


# ------------------------------------------------------------------------------------- 5. L2 index shuffle

ROW_TOOLS = (("get_standings", {}, "standings"), ("get_car_telemetry", {"car": "all"}, "cars"),
             ("get_car_status", {"car": "all"}, "cars"), ("get_car_abilities", {"car": "all"}, "cars"),
             ("get_car_motion", {"car": "all"}, "cars"))


async def check_no_wrong_names(srv, game):
    pid_me = my_pid(game)
    problems = []
    for tool, args, key in ROW_TOOLS:
        x = await srv.call(tool, args)
        assert x.get("ok"), (tool, x)
        for row in x[key]:
            truth = game.truth(row["car"])
            if "name" in row and row["name"] != truth["name"]:
                problems.append((tool, row["car"], row["name"], truth["name"]))
            if "player_id" in row and row["player_id"] != truth["player_id"]:
                problems.append((tool, row["car"], "pid", row["player_id"], truth["player_id"]))
            if row.get("player_id") == pid_me and row["car"] != game.my_car:
                problems.append((tool, row["car"], "has my player_id", game.my_car))
            if row.get("is_me") and row["car"] != game.my_car:
                problems.append((tool, row["car"], "is_me", game.my_car))
    snap = await srv.call("get_race_snapshot")
    for key in ("car_ahead", "car_behind"):
        row = snap.get(key)
        if not row:
            continue
        truth = game.truth(row["car"])
        if "name" in row and row["name"] != truth["name"]:
            problems.append(("snapshot", key, row["name"], truth["name"]))
        if row.get("player_id") == pid_me or row.get("is_me"):
            problems.append(("snapshot", key, "is me", row))
    want_ahead = game.my_car - 1                        # fake: position = index + 1
    if snap.get("car_ahead"):
        if snap["car_ahead"]["car"] != want_ahead:
            problems.append(("snapshot car_ahead index", snap["car_ahead"]["car"], want_ahead))
    return problems


async def next_participants(srv):
    st = await srv.call("get_api_status")
    n0 = st["telemetry"]["packets_by_type"].get("participants", 0)

    async def more():
        s = await srv.call("get_api_status")
        return s["telemetry"]["packets_by_type"].get("participants", 0) > n0
    await until(more, 7, step=0.05, what="next Participants packet")


def test_l2_index_shuffle_no_wrong_names(tmp_path):
    async def main():
        with fake_game() as (game, port):
            async with served(tmp_path, game, port) as srv:
                await receiving(srv)
                assert game.my_car == 10
                # (a) my car swaps with the car ahead right after a Participants packet
                await next_participants(srv)
                perm = list(range(16))
                perm[9], perm[10] = 10, 9
                game.reorder(perm)
                assert game.my_car == 9
                await asyncio.sleep(0.7)       # Car Status comes at 2 Hz: let every packet type be post-reorder
                problems = await check_no_wrong_names(srv, game)
                assert problems == [], problems
                snap = await srv.call("get_race_snapshot")
                ahead = snap.get("car_ahead")
                assert ahead and ahead["car"] == 8 and ahead.get("selector") == "ahead", snap
                assert "name" not in ahead or ahead["name"] == game.truth(8)["name"], ahead
                sel = await srv.call("get_car_telemetry", {"car": "ahead"})
                assert sel.get("ok") and sel.get("resolved_car_index") == 8, sel
                assert sel["cars"][0]["car"] == 8 and not sel["cars"][0].get("is_me"), sel
                # (b) two OTHER cars ahead swap; my index unchanged; no new Participants
                await next_participants(srv)
                perm = list(range(16))
                perm[3], perm[5] = 5, 3
                game.reorder(perm)
                assert game.my_car == 9
                await asyncio.sleep(0.7)
                t_end = time.monotonic() + 3.0
                while time.monotonic() < t_end:
                    await asyncio.sleep(0.25)
                    problems = await check_no_wrong_names(srv, game)
                    assert problems == [], problems
                for key, want in (("ahead", 8), ("behind", 10), ("P4", 3), ("P6", 5)):
                    sel = await srv.call("get_car_status", {"car": key})
                    assert sel.get("ok") and sel.get("resolved_car_index") == want, (key, sel)
                    row = sel["cars"][0]
                    assert "name" not in row or row["name"] == game.truth(want)["name"], (key, row)
                # after Participants catches up, names (where given) are still right
                await next_participants(srv)
                await asyncio.sleep(0.1)
                problems = await check_no_wrong_names(srv, game)
                assert problems == [], problems
                parts = await srv.call("get_participants")
                assert parts.get("ok"), parts
    run(main(), 60)


# ------------------------------------------------------------------------------------- 6. G1 last lap (0.88.2 lap semantics)

async def race(tmp_path, total_laps, name):
    """The fake game sends the lap semantics observed in 0.88.2 (Lap Data lap / LAPC = the lap being driven, Player
    laps_remaining = laps after the current one); game.lap is the truth in laps completed."""
    lap_s = 2.0
    found = []
    with fake_game() as (game, port):
        game.points, game.hand = 0, []
        # from the session's first packet on (the captured Lap Data says lap 2; laps never go back in a session)
        game.start_scenario(lap_s=lap_s, wear_per_lap=0.4, start_health=1.0, total_laps=total_laps, lap=0)
        t_end = time.monotonic() + lap_s * total_laps + 0.8
        async with served(tmp_path, game, port, name=name) as srv:
            await receiving(srv, participants=False)
            last = (await first_wait(srv, 0))["last_seq"]
            while time.monotonic() < t_end:
                r = await wait(srv, 0.4, last)
                last = r["last_seq"]
                for t in of_type(r, "tires_wont_last"):
                    found.append((t, game.lap))
                snap = await srv.call("get_race_snapshot")
                lc, lr = snap.get("laps_completed"), snap.get("laps_remaining")
                truth = min(game.lap, total_laps)
                if lc is not None:
                    # never ahead of the truth (the 0.2.0 bug: one too high); may lag a sample at a boundary
                    assert lc in (truth, truth - 1), (lc, truth, snap.get("lap_source"))
                    assert lr == total_laps - lc, (lc, lr, snap.get("lap_source"))
                    if lc < total_laps:
                        assert snap.get("current_lap") == lc + 1, snap
            cfg = await srv.call("get_trigger_config")
            obs = (cfg.get("laps") or {}).get("lap_observations") or cfg["state"].get("lap_observations")
    return found, obs


def test_g1_last_lap_silent(tmp_path):
    found, obs = run(race(tmp_path, 3, "observed"))
    assert all(t.get("laps_remaining") is None or t["laps_remaining"] > 1 for t, _ in found), found
    assert all(lap < 2 for _, lap in found), found          # never once the final lap has started
    assert obs and any(o.get("lapc_lap") is not None for o in obs), obs


def test_g1_positive_control_fires_before_last_lap(tmp_path):
    """Same wear in a 4-lap race: the forecast fires going into lap 3 of 4 (so the 3-lap silence is real)."""
    found, obs = run(race(tmp_path, 4, "control"))
    assert found, "tires_wont_last never fired with 0.4 wear per lap"
    assert all(t.get("laps_remaining", 2) >= 2 for t, _ in found), found


# ------------------------------------------------------------------------------------- 7. server restart mid-race

def test_server_restart_mid_race(tmp_path):
    async def main():
        with fake_game() as (game, port):
            async with served(tmp_path, game, port, name="a") as srv:
                await receiving(srv)
                last = (await first_wait(srv, 0))["last_seq"]
                offer(game)
                r = await wait(srv, 3, last)
                assert of_type(r, "card_offer"), r
            # the old process is gone; the model still holds a since_seq from it
            async with served(tmp_path, game, port, name="b") as srv:
                await receiving(srv, participants=False)
                offer(game)
                await asyncio.sleep(0.3)
                r = await wait(srv, 3, 37)
                cards = of_type(r, "card_offer")
                assert cards and cards[0]["seq"] <= 2, r
                assert r["last_seq"] < 37, r
                assert any("37" in n for n in r.get("notes") or []), r
                r2 = await wait(srv, 3, 37)              # since_seq ahead of the newest trigger: ack reset
                assert any("37" in n for n in r2.get("notes") or []), r2
            # restart while the game is paused on a hand this process never saw
            game.pause()
            async with served(tmp_path, game, port, name="c") as srv:
                await receiving(srv, participants=False)
                await asyncio.sleep(6.8)                 # > PAUSE_AFTER_S + PAUSE_HINT_S
                x = await wait(srv, 0.5, 37)
                hint = (x.get("paused") or {}).get("paused_hint") or ""
                assert "has not seen the hand" in hint, x
                st = await srv.call("get_api_status")
                assert any("has not seen the hand" in w for w in st.get("warnings") or []), st.get("warnings")
            game.resume()
    run(main(), 60)


# ------------------------------------------------------------------------------------- 8. tire_screen by default

def test_tire_screen_via_wait_by_default_bounded(tmp_path):
    async def main():
        with fake_game() as (game, port):
            async with served(tmp_path, game, port) as srv:
                await receiving(srv)
                cfg = await srv.call("get_trigger_config")
                assert "tire_screen" in cfg["enabled"], cfg["enabled"]
                await wait(srv, 0.3)
                game.in_pits = True
                game.tire_event()
                returns = 0
                t_end = time.monotonic() + 6 * REMIND            # plan: 120 s at REMIND_S = 20
                first = True
                while time.monotonic() < t_end:
                    x = await wait(srv, 0.5)                     # never answered, never acknowledged
                    got = of_type(x, "tire_screen")
                    if first:
                        assert got and got[0]["open"] is True, x
                        first = False
                    returns += len(got)
                    if not got:
                        assert x["_elapsed"] >= 0.45, x["_elapsed"]
                assert 1 <= returns <= 1 + MAX_RESEND, returns
    run(main(), 40)


# ------------------------------------------------------------------------------------- 9. concurrent waits

def test_two_concurrent_waits_both_get_the_card(tmp_path):
    async def main():
        with fake_game() as (game, port):
            async with served(tmp_path, game, port) as srv:
                await receiving(srv)
                last = (await first_wait(srv, 0))["last_seq"]
                a = asyncio.ensure_future(srv.call("wait_for_trigger", {"timeout_s": 3, "since_seq": last}))
                b = asyncio.ensure_future(srv.call("wait_for_trigger", {"timeout_s": 3, "since_seq": last}))
                await asyncio.sleep(0.4)
                offer(game)
                ra, rb = await asyncio.gather(a, b)
                for r in (ra, rb):
                    assert not r["_is_error"] and r.get("ok"), r
                    assert of_type(r, "card_offer"), r
                    assert r["_elapsed"] < 2.0, r["_elapsed"]
                assert of_type(ra, "card_offer")[0]["seq"] == of_type(rb, "card_offer")[0]["seq"]
    run(main())


# ------------------------------------------------------------------------------------- 10. wait cap

def test_wait_max_240_capped_to_60(tmp_path):
    async def main():
        with fake_game() as (game, port):
            async with served(tmp_path, game, port, BC_WAIT_MAX_S=240) as srv:
                st = await srv.call("get_api_status")
                assert st["config"]["wait_for_trigger_max_s"] == 60, st["config"]
            async with served(tmp_path, game, port, name="low", BC_WAIT_MAX_S=1.5) as srv:
                await receiving(srv)
                last = (await first_wait(srv, 0))["last_seq"]
                x = await wait(srv, 120, last)
                assert x["status"] == "timeout" and 1.3 <= x["_elapsed"] <= 2.5, x
    run(main())


# ------------------------------------------------------------------------------------- 11. eviction note

def test_queue_eviction_note(tmp_path):
    async def main():
        with fake_game() as (game, port):
            async with served(tmp_path, game, port) as srv:
                await receiving(srv)
                await first_wait(srv, 0)
                game.tire_event()
                r = await wait(srv, 3, 0)
                assert of_type(r, "tire_screen"), r
                last = r["last_seq"]
                for i in range(230):                      # more than the queue keeps (200)
                    game.tire_event()
                    if i % 20 == 19:
                        await asyncio.sleep(0.02)

                async def queued():
                    c = await srv.call("get_trigger_config")
                    return c["state"]["last_seq"] >= last + 230
                await until(queued, 5, what="all TIRE events processed")
                x = await wait(srv, 2, last)
                notes = " ".join(x.get("notes") or [])
                assert "evicted" in notes, x
                import re
                m = re.search(r"(\d+) triggers evicted", notes)
                assert m and int(m.group(1)) >= 1, notes
                screens = of_type(x, "tire_screen")
                assert len(screens) == 1 and screens[0]["seq"] == last + 230, x   # superseded ones skipped
    run(main())
