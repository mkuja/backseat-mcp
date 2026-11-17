"""MCP server for the Backseat Champions Game API (unofficial).

The model acts as race engineer. The server is trigger-driven: ``wait_for_trigger`` blocks (at most 60 s) until
something needs a decision (a new card hand, the tire screen, damage, tires that will not last another lap) and
keeps returning an unanswered decision until it is acknowledged with ``since_seq`` or answered; everything else is
available through on-demand query tools. The server never sends a gameplay command on its own; the only command it
sends unasked is the read-only ``hello`` handshake (at start, on a new session at most once a second, and from read
tools when data is missing, to say why).

Run ``backseat-champions-mcp --help`` for options. Logs go to stderr; stdout carries only MCP messages.
"""
import argparse
import asyncio
import contextlib
import functools
import inspect
import json
import logging
import math
import os
import re
import statistics
import sys
import time
import warnings
from typing import Annotated, Any, List, Optional, Union

from pydantic import Field, Strict

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from . import __version__
from . import protocol as P
from . import reference as R
from .client import MAX_BOOST_S, MAX_DRIVE_S, MIN_HOLD_S, Config, GameClient, failure
from .instructions import INSTRUCTIONS, race_engineer_prompt
from .results import ResultsError, ResultsReceiver

log = logging.getLogger("backseat_champions_mcp")

# Strict scalar types for tool arguments: true is not 1, "yes" is not true, and 1 is still a valid float.
SInt = Annotated[int, Strict()]
SStr = Annotated[str, Strict()]
SFloat = Annotated[float, Strict()]

TRIGGERS_URI = "bc://triggers/latest"
WAIT_EVENT_MAX_S = 60.0
WAIT_HARD_MAX_S = 60.0       # wait_for_trigger never blocks longer (X3); BC_WAIT_MAX_S can only lower it
ME_WORDS = ("me", "mine", "self", "player", "")
_ASCII_NUMBER = re.compile(r"[0-9]+")


def progress_interval(environ):
    """BC_PROGRESS_INTERVAL_S (seconds between MCP progress notifications during waits; default 10)."""
    raw = (environ.get("BC_PROGRESS_INTERVAL_S") or "").strip()
    if not raw:
        return 10.0
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not math.isfinite(value) or not 0 < value <= 60:
        raise ValueError("BC_PROGRESS_INTERVAL_S must be a number of seconds above 0 and at most 60, got %r" % raw)
    return value


try:
    PROGRESS_INTERVAL_S = progress_interval(os.environ)
except ValueError:                   # reported cleanly by main() (config_from_args); never a traceback at import
    PROGRESS_INTERVAL_S = 10.0

# ------------------------------------------------------------------------------------------------ formatting

MAX_CLEAN_DEPTH = 100


def clean(value, digits=3, _depth=0):
    """Round floats (NaN/inf -> None) recursively, for compact JSON. Deeper than MAX_CLEAN_DEPTH -> None."""
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value if digits is None else round(value, digits)
    if isinstance(value, (dict, list, tuple)) and _depth >= MAX_CLEAN_DEPTH:
        return None
    if isinstance(value, dict):
        return {k: clean(v, digits, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, digits, _depth + 1) for v in value]
    return value


def _finite(value):
    """Non-finite floats (a hostile or broken datagram's NaN/inf) -> None, so tool output is always valid JSON."""
    return clean(value, None)


def _result(out):
    is_error = isinstance(out, dict) and out.get("ok") is False
    text = json.dumps(_finite(out), separators=(",", ":"), ensure_ascii=False, default=str)
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)


# Every tool that reads the game (live telemetry / UDP) is open-world (D11); only the static reference and this
# server's own trigger configuration are not.
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
READ_NET = READ                  # read tools that also send a UDP command (hello / ping) to the game
READ_LOCAL = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WAIT = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=True)


def _write(destructive, idempotent, title=None):
    return ToolAnnotations(title=title, read_only_hint=False, destructive_hint=destructive,
                           idempotent_hint=idempotent, open_world_hint=True)      # sends UDP to the game


# ------------------------------------------------------------------------------------------------ shared state

class AppState:
    """The game client (one per process) shared by every MCP session, started on first use."""

    def __init__(self, config):
        self.config = config
        self.client = GameClient(config)
        self.results = None
        if config.results_port is not None:
            self.results = ResultsReceiver(config.results_host, config.results_port, config.results_token,
                                           config.results_dir, config.results_keep)
        self.users = 0
        self.lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def use(self):
        async with self.lock:
            if self.users == 0:
                await self.client.start()
                if self.results is not None:
                    self.results.start()
            self.users += 1
        try:
            yield self
        finally:
            async with self.lock:
                self.users -= 1
                if self.users == 0:
                    await self.client.stop()
                    if self.results is not None:
                        self.results.stop()


# ------------------------------------------------------------------------------------------------ server

def build_server(config, log_level="WARNING"):
    state = AppState(config)
    client = state.client

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with state.use():
            yield state

    mcp = MCPServer("backseat-champions", instructions=INSTRUCTIONS, version=__version__, lifespan=lifespan,
                    log_level=log_level)
    mcp.state = state

    def tool(annotations, name=None):
        """Register a tool whose function returns a dict; the dict is sent as compact JSON text."""
        def decorator(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                try:
                    out = await fn(*args, **kwargs)
                except ValueError as e:
                    out = failure("bad_argument", detail="(%s)" % e)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # never a bare "Error executing tool": always the structured shape
                    log.exception("tool %s failed", fn.__name__)
                    out = failure("server_error", detail="(%s: %s)" % (type(e).__name__, e))
                return _result(out)
            # cleandoc: Python 3.13 dedents docstrings itself and 3.10 does not; clients get the same text (V-D5)
            mcp.add_tool(wrapper, name=name or fn.__name__, description=inspect.cleandoc(fn.__doc__ or ""),
                         annotations=annotations, structured_output=False)
            return fn
        return decorator

    # ------------------------------------------------------------------ helpers

    engine = client.triggers

    class Names(dict):
        """Car index -> Participants entry, valid for the packet whose header it was built for (X7).

        The game re-orders car indices by race position mid-session (seen in 0.88.2) and Participants only comes
        every 5 s, so another car's name is attached only when the Participants packet has the same frame as the
        packet being labelled. My own car is always named: through Participants' own seat_car[0] (its player_id is
        stable), and the labelled packet's own seat_car[0] says which of its rows is mine."""
        me = None
        exact = False
        note = None

    def names(header):
        info = Names()
        info.me = header["seat_car"][0] if header is not None else client.my_car()
        got = client.latest(P.PKT_PARTICIPANTS)
        if got is None:
            return info
        ph, body, _age = got
        cars = body["cars"]
        if header is not None and ph["frame"] == header.get("frame"):
            info.exact = True
            info.update(enumerate(cars))
            return info
        pme = ph["seat_car"][0]
        if info.me is not None and pme is not None and pme < len(cars):
            info[info.me] = cars[pme]
        info.note = ("Only your own car is named: names come from the Participants packet (every 5 s, frame %d) and "
                     "this packet is from frame %s; the game re-orders car indices by race position mid-session, so "
                     "another car's name could be wrong. get_participants lists every name with its age."
                     % (ph["frame"], None if header is None else header.get("frame")))
        return info

    def label(car, info):
        out = {"car": car}
        p = info.get(car)
        if p:
            out.update(name=p["name"], car_type=p["car_type"], kind=p["kind"], player_id=p["player_id"])
        if car == info.me:
            out["is_me"] = True
        return out

    def add_names_note(out, info):
        if info.note:
            out["names_note"] = info.note
        return out

    def session_ended_info():
        """client.paused() when it reports the session ended (SEND / FINI seen), else None (V-T4)."""
        p = client.paused()
        if p and p.get("session_ended"):
            return dict(p)
        return None

    def paused_info():
        """client.paused() plus the trigger engine's view of the card hand (X4), or None when not paused.

        A session that ended (SEND / FINI) is not a pause, even if Session / Car Status still stream (V-T4)."""
        p = client.paused()
        if not p or not p.get("paused") or p.get("session_ended"):
            return None
        p = dict(p)
        with contextlib.suppress(Exception):
            p["card_hand_pending"] = engine.card_state().get("open")
        hint = None
        with contextlib.suppress(Exception):
            hint = engine.paused_hint(True, p.get("session_time_frozen_for_s") or 0.0)
        if hint:
            p["paused_hint"] = hint
        return p

    def current_points(cs=None):
        """Upgrade points now (G8, V-D8): from the newer of the newest Player sample and the card offer of the
        engine's last hand (CARD event or Player), by frame (then arrival order). Spending the last point therefore
        shows 0 as soon as a Player sample says so, even though the last hand was offered with 1."""
        cs = engine.card_state() if cs is None else cs
        pl = client.latest(P.PKT_PLAYER, 0)
        if pl is None:
            return cs.get("points")
        lc = getattr(engine, "_last_card", None)
        if not lc or lc.get("fired_frame") is None:
            return pl[1]["points"]
        player = (pl[0]["frame"], getattr(engine, "_player_no", None) or 0)
        offer = (lc["fired_frame"], lc.get("fired_no") or 0)
        return pl[1]["points"] if player >= offer else cs.get("points")

    def open_decisions():
        """The open (or unknown) card hand / tire screen, from the trigger engine's live state."""
        return clean(engine.open_decisions())

    def never_returned():
        if hasattr(engine, "undelivered"):
            return engine.undelivered()
        return sum(1 for e in engine.triggers if not e.get("returns"))

    def stale_note(pid, age):
        if age > client.stale_after(pid):
            if session_ended_info():
                return ("This data is %.1f s old because the session has ended (SEND / FINI seen). It is the state "
                        "at the end." % age)
            paused = paused_info()
            if paused:
                why = ("a card hand waits for pick_card" if paused.get("card_hand_pending") is True
                       else "this server knows of no open card hand, so the reason is unknown")
                return ("This data is %.1f s old because the game is paused (session_time is not advancing; %s). "
                        "It is the state at the pause." % (age, why))
            return ("This data is %.1f s old: telemetry has stopped (session over, game paused or closed, or the "
                    "switch was turned off)." % age)
        return None

    async def need(pid, key=0, what=None):
        got = client.latest(pid, key)
        if got is None:
            return None, await client.no_data_failure(pid, what)
        return got, None

    def base(pid, header, age):
        out = {"ok": True, "age_s": round(age, 2), "session_uid": header["session_uid"],
               "session_time_s": round(header["session_time"], 2), "frame": header["frame"]}
        note = stale_note(pid, age)
        if note:
            out["stale"] = True
            out["stale_note"] = note
            if paused_info():
                out["paused"] = True
        return out

    def _position_selector(key):
        """"ahead" / "behind" / "p<n>" -> wanted position, or None when key is not such a selector."""
        if key in ("ahead", "behind"):
            return key
        if len(key) > 1 and key[0] == "p" and _ASCII_NUMBER.fullmatch(key[1:]):    # ASCII digits only (V-T10)
            return int(key[1:])
        return None

    def select_cars(car, count, header):
        """``(indices, meta)`` for a car selector, as indices of the packet whose ``header`` is given.

        "me": the packet's own seat_car[0]. "all": every car. "ahead" / "behind" / "P<n>": resolved by race
        position in the NEWEST Lap Data packet (X8); meta says which index that gave and, when the Lap Data
        packet is from another frame than this packet, the frame difference. An integer index is taken as is
        (valid for this instant only)."""
        me = header["seat_car"][0] if header is not None else client.my_car()
        if car is None:
            car = "me"
        if isinstance(car, str):
            key = car.strip().lower()
            if key in ME_WORDS:
                if me is None:
                    raise ValueError("seat 0 has no car in this session (seat_car[0] is 255)")
                if me >= count:
                    raise ValueError("seat 0's car index %d is not among the %d cars of this packet" % (me, count))
                return [me], {"resolved_car_index": me}
            if key == "all":
                return list(range(count)), {}
            want = _position_selector(key)
            if want is not None:
                return _resolve_position(key, want, count, header)
            if not _ASCII_NUMBER.fullmatch(key):
                raise ValueError('car must be "me", "ahead", "behind", "P<n>", "all" or a car index 0..%d, got %r'
                                 % (max(count - 1, 0), car))
            car = int(key)
        if isinstance(car, bool) or not isinstance(car, int):
            raise ValueError("car must be a selector or an integer index, got %r" % (car,))
        if count <= 0:
            raise ValueError("car index %d is out of range: this packet has no cars (num_cars 0)" % car)
        if not (0 <= car < count) or car >= P.MAX_CARS:
            raise ValueError("car index %r is out of range: this session has %d cars (0..%d)"
                             % (car, count, count - 1))
        return [car], {"resolved_car_index": car}

    def _resolve_position(key, want, count, header):
        lap = client.latest(P.PKT_LAP_DATA)
        if lap is None:
            raise ValueError("car=%r needs Lap Data (Driver Data switch) to find positions; none has arrived" % key)
        lh, lb, _age = lap
        cars = lb["cars"][:min(lh["num_cars"], P.MAX_CARS) or len(lb["cars"])]
        if want in ("ahead", "behind"):
            lme = lh["seat_car"][0]
            if lme is None or lme >= len(cars) or not cars[lme]["position"]:
                raise ValueError("car=%r: your position is unknown in the newest Lap Data packet" % key)
            mine = cars[lme]["position"]
            pos = mine - 1 if want == "ahead" else mine + 1
            if pos < 1:
                raise ValueError("car='ahead': you are in position 1, there is no car ahead")
        else:
            pos = want
        found = [i for i, c in enumerate(cars) if c["position"] == pos]
        if not found:
            raise ValueError("car=%r: no car is in position %d (%d cars)" % (key, pos, len(cars)))
        idx = found[0]
        if idx >= count:
            raise ValueError("car=%r resolved to index %d, which is not in this packet's %d cars" % (key, idx, count))
        meta = {"selector": key, "resolved_car_index": idx, "resolved_position": pos}
        add_lap_delta(meta, header, lh["frame"])
        return [idx], meta

    def add_lap_delta(meta, header, lap_frame):
        """Say how far the Lap Data packet a selector was resolved with is from the packet it labels (X8)."""
        if header is None or lap_frame is None or header.get("frame") == lap_frame:
            return meta
        meta["resolved_from_lap_data_frame_delta"] = header["frame"] - lap_frame
        meta["selector_note"] = ("Resolved by position in the newest Lap Data packet, %d frame(s) %s this packet; "
                                 "indices can change when positions change." %
                                 (abs(header["frame"] - lap_frame), "before" if header["frame"] > lap_frame
                                  else "after"))
        return meta

    def energy_kind():
        got = client.latest(P.PKT_SESSION)
        return None if got is None else got[1]["energy"]

    CarParam = Annotated[Union[SInt, SStr], Field(
        description='Which car: "me" (player one\'s car, the default), "ahead" / "behind" (the car one position '
                    'ahead of / behind you), "P<n>" (the car in race position n, e.g. "P1"), "all" (every car), or a '
                    'car index 0..15. Positions are read from the newest Lap Data packet. An index is valid for this '
                    'instant only: the game re-orders car indices by race position mid-session (seen in 0.88.2), so '
                    'never reuse an index from an earlier result; follow a car by player_id or use a selector.')]

    # ================================================================== status

    def expected_missing(flags, ages):
        """Packet types the broadcast switches should send but that have not arrived within their stale limit."""
        out = []
        for pid in (P.PKT_MOTION, P.PKT_CAR_TELEMETRY, P.PKT_LAP_DATA, P.PKT_CAR_STATUS, P.PKT_PLAYER,
                    P.PKT_SESSION, P.PKT_PARTICIPANTS, P.PKT_CAR_ABILITIES, P.PKT_RACING_LINE,
                    P.PKT_AI_OBSERVATIONS):
            switches = R.PACKET_SWITCHES.get(pid, ())
            if not any(flags.get(sw) for sw in switches):
                continue
            name = P.PACKET_NAMES[pid]
            age = ages.get(name)
            if age is None or age > client.stale_after(pid):
                out.append(name)
        return out

    @tool(READ_NET)
    async def get_api_status(ctx: Context) -> dict:
        """Check the connection to the game and what is switched on. Call this first.

        Sends the Game API `hello` command (and a `ping` for latency) to the game's command port and combines the
        Hello reply with this server's own state:
        - game: reachable, game_version, rate_hz, feature schema version/hash, `inputs` (player_input,
          driver_input, public_lobby) and `broadcast` switches (driver_data, player_data, decision_events,
          ai_observations).
        - switches_off: each broadcast/input switch that is off, with `required` and the tools that cannot work
          without it (Driver Data, Player Data, Decision Events, Player Input are required by the trigger loop; AI
          Observations and Driver Input are optional; Driver Input is only listed when the drive tools are
          registered). required_switches_off: just the required ones. Settings live in the game under
          Settings > Telemetry (the API is off by default).
        - telemetry listener: bound address, whether packets are arriving, last packet age, counts and
          last_seen_age_s per packet type, expected_not_arriving (types the switches should send that have not
          come recently), paused (false, or {paused, session_time_frozen_for_s, card_hand_pending, ...}: the game
          stops time while a card hand waits), packets dropped for an unknown packet_version, session_uid, my car
          index (seat 0; can change mid-session, see get_participants).
        - holds (running boost/drive repeats and any error that ended one), trigger summary (queued, last_seq,
          acked_up_to_seq, open_decisions), results receiver.
        - config: ports, whether a command token is set (never the token itself), whether drive tools exist.
        Needs nothing switched on. If the game does not answer, `game.reachable` is false with a hint (game not
        running, API off, wrong BC_COMMAND_HOST/PORT, or a missing Command Token for a remote game).
        """
        client.forget_hello()
        hello = await client.hello(max_age=0)
        out = {"ok": True, "server_version": __version__}
        game = {"reachable": hello["ok"]}
        warnings_ = []
        if hello["ok"]:
            h = hello["hello"]
            ping = await client.ping()
            game.update(game_version=h["game_version"], rate_hz=h["rate_hz"],
                        ping_ms=ping.get("latency_ms") if ping["ok"] else None,
                        feature_schema_version=h["feature_schema_version"],
                        feature_schema_hash=h["feature_schema_hash"],
                        inputs={k: h[k] for k in ("player_input", "driver_input", "public_lobby")},
                        broadcast={k: h[k] for k in R.BROADCAST_SWITCHES})
            off = []
            for key, info in R.BROADCAST_SWITCHES.items():
                if not h[key]:
                    tools = list(info["tools"])
                    # Car Abilities needs Driver Data OR Player Data: only lost when both are off
                    other = {"driver_data": "player_data", "player_data": "driver_data"}.get(key)
                    if other and h[other]:
                        tools = [t for t in tools if t != "get_car_abilities"]
                    off.append({"switch": info["label"], "kind": "broadcast", "required": key in R.REQUIRED_SWITCHES,
                                "affects": tools})
            for key, info in R.INPUT_SWITCHES.items():
                if not h[key]:
                    if key == "driver_input" and not config.allow_drive:
                        continue                 # only drive / release_drive need it, and they are not registered
                    off.append({"switch": info["label"], "kind": "input", "required": key in R.REQUIRED_SWITCHES,
                                "affects": info["tools"]})
            game["switches_off"] = off
            required = [o["switch"] for o in off if o["required"]]
            optional = [o["switch"] for o in off if not o["required"]]
            game["required_switches_off"] = required
            if required:
                warnings_.append("Required for the race-engineer loop and off: %s. Switch them on under %s."
                                 % (", ".join(required), R.SETTINGS_PATH))
            if optional:
                warnings_.append("Optional and off (only the listed tools are affected): %s." % ", ".join(optional))
            if h["public_lobby"]:
                warnings_.append("Public lobby: every command except ping, hello and set_camera is refused "
                                 "(public_lobby). Read tools still work.")
            if not h["decision_events"]:
                warnings_.append("Decision Events is off: card offers are detected from the Player packet instead "
                                 "of CARD events, and the tire screen (TIRE event) cannot be seen at all.")
        else:
            game.update(error_reason=hello["error_reason"], explanation=hello["explanation"], hint=hello["hint"])
        out["game"] = game
        listener = client.listener_status()
        listener["my_car"] = client.my_car()
        listener["my_car_note"] = ("seat_car[0] of the newest packet. The game re-orders car indices by race position "
                                   "mid-session (seen in 0.88.2), so this index can change; player_id does not.")
        listener["num_cars"] = min(client.num_cars, P.MAX_CARS)
        out["telemetry"] = listener
        for text in listener.get("warnings") or []:          # e.g. packets dropped for an unknown packet_version (T3)
            warnings_.append(text)
        paused = paused_info()
        listener["paused"] = paused or False
        ended = session_ended_info()
        if ended:
            listener["session_ended"] = True
            if ended.get("note"):
                warnings_.append(ended["note"])
        if paused:
            if paused.get("note"):
                warnings_.append(paused["note"])
            if paused.get("paused_hint"):
                warnings_.append(paused["paused_hint"])
        if not listener["bound"]:
            out["telemetry"]["problem"] = client.listener_failure()
        elif not listener["receiving"]:
            warnings_.append("No telemetry is arriving. The game sends telemetry only during a session (practice, "
                             "qualifying or race).")
        elif hello["ok"]:
            missing = expected_missing(hello["hello"], listener["last_seen_age_s"])
            # while paused the configured-rate packets stop by design (L3): not reported as missing
            if missing and not paused and not ended:
                listener["expected_not_arriving"] = missing
                warnings_.append("Telemetry arrives, but these packet types the switches should send have not "
                                 "arrived recently: %s. Check the switches and that nothing else receives on the "
                                 "telemetry port." % ", ".join(missing))
        out["holds"] = client.hold_status()
        out["triggers"] = {"enabled": sorted(engine.config.enabled), "queued": len(engine.triggers),
                           "last_seq": engine.seq, "acked_up_to_seq": getattr(engine, "acked", None),
                           "never_returned": never_returned(), "open_decisions": open_decisions()}
        if state.results is not None:
            out["results_receiver"] = state.results.status()
        out["config"] = config.public()
        if warnings_:
            out["warnings"] = warnings_
        return out

    @tool(READ_NET)
    async def ping(ctx: Context) -> dict:
        """Liveness check: send the Game API `ping` command and wait for its Ack.

        Returns {ok, latency_ms}. Works in public lobbies and needs no switch except the API itself being on.
        On failure: error_reason no_reply / game_unreachable with an explanation and hint.
        """
        return await client.ping()

    # ================================================================== session data

    @tool(READ)
    async def get_session(ctx: Context) -> dict:
        """The current session: type, laps, fuel or boost, weather, input flags, track and its measured properties.

        Source: Session packet (id 5; sent twice a second and when a session starts; needs any broadcast switch)
        plus the packet header. Returns:
        - session_uid (new for every session; qualifying and the race after it are two sessions), session_time_s
          (seconds since the cars were placed on the grid), num_cars, my_car (seat 0's car index), game_version.
        - session_type (AITraining, Practice, Qualifying, Race), energy_kind ("fuel": energy fields are fuel,
          "boost": they are boost), total_laps (0 = no lap count), weather (-1 clear sky to 1 storm; rain starts
          above 0).
        - player_input, driver_input, public_lobby (the API's input flags).
        - scene (track scene name) and track (null when the track has no measured properties): length_km,
          corner_count, avg_corner_speed, straight_ratio, elevation_change, base_tire_wear_rate,
          overtake_difficulty, pit_lane_time_loss_s (seconds), width_mean, width_min, barrier_ratio,
          drs_zone_count, drs_coverage, lap_time_est_s (seconds). Units other than those stated are not
          documented by the game.
        - feature_schema_version/hash: identify the Extreme AI feature layout of the strategy blocks.
        - age_s: seconds since the packet arrived (stale=true when telemetry stopped).
        """
        got, err = await need(P.PKT_SESSION, what="Session")
        if err:
            return err
        header, body, age = got
        out = base(P.PKT_SESSION, header, age)
        out.update(num_cars=min(header["num_cars"], P.MAX_CARS), my_car=header["seat_car"][0], game_version=header["game_version"],
                   session_type=body["session_type"], energy_kind=body["energy"], total_laps=body["total_laps"],
                   weather=body["weather"], player_input=body["player_input"], driver_input=body["driver_input"],
                   public_lobby=body["public_lobby"], scene=body["scene"], track=body["track"],
                   feature_schema_version=body["feature_schema_version"],
                   feature_schema_hash=body["feature_schema_hash"])
        return clean(out)

    @tool(READ)
    async def get_participants(ctx: Context) -> dict:
        """Every car in the session with its driver's display name, car type and kind.

        Source: Participants packet (id 6; every 5 s and when a session starts; needs Driver Data). Per car:
        car (index 0..15 as of this packet's frame; valid for that instant only: the game re-orders car indices
        by race position mid-session (seen in 0.88.2), so follow a car by player_id), name (display name; other
        online players' names included), car_type (Formula, ClassicFormula, ModernFormula, Endurance, GT2, Stock,
        FormulaOldskool, FormulaLegacy), kind (ai, local = a player on this machine, remote = a player on another
        machine), player_id (stable for the session), is_me for seat 0's car (by this packet's own header).
        Also frame and age_s of the packet, and a note: the indices may have changed since.
        """
        got, err = await need(P.PKT_PARTICIPANTS, what="Participants")
        if err:
            return err
        header, body, age = got
        out = base(P.PKT_PARTICIPANTS, header, age)
        me = header["seat_car"][0]                   # this packet's own index of seat 0's car
        out["cars"] = [dict({"car": i}, **c, **({"is_me": True} if i == me else {}))
                       for i, c in enumerate(body["cars"])]
        out["note"] = ("Car indices as of frame %d (%.1f s ago). The game re-orders indices by race position "
                       "mid-session, so they may have changed since; player_id identifies a car."
                       % (header["frame"], age))
        return out

    @tool(READ)
    async def get_standings(ctx: Context) -> dict:
        """The running order: every car sorted by position, with names, laps, gaps and pit state.

        Source: Lap Data packet (id 2; at the configured rate; needs Driver Data), names from Participants.
        Per row: position (overall, from 1), class_position, car, name, car_type, kind, laps (laps completed),
        gap_to_leader_s, interval_ahead_s, interval_behind_s (seconds; the game does not document the sign of
        interval_behind and real captures show it negative, so read it as a magnitude), pit_stops, in_pits
        (in the pit lane), finished, pit_planned (only ever true for this machine's cars: a rival's call is never
        shown), api_driving (an API client is driving that car), is_me (by this packet's own header), name,
        car_type, kind, player_id. Names come from Participants (every 5 s): other cars are named only when that
        packet is from the same frame as this one; otherwise only your own car is named and names_note says why
        (the game re-orders car indices by race position mid-session). car is valid for this instant only.
        """
        got, err = await need(P.PKT_LAP_DATA, what="Lap Data")
        if err:
            return err
        header, body, age = got
        info = names(header)
        rows = []
        for i, c in enumerate(body["cars"]):
            row = {"position": c["position"], "class_position": c["class_position"]}
            row.update(label(i, info))
            row.update(laps=c["lap"], gap_to_leader_s=c["gap_to_leader"], interval_ahead_s=c["interval_ahead"],
                       interval_behind_s=c["interval_behind"], pit_stops=c["pit_stops"], in_pits=c["in_pits"],
                       finished=c["finished"], pit_planned=c["pit_planned"], api_driving=c["api_driving"])
            rows.append(row)
        rows.sort(key=lambda r: (r["position"] or 99, r["car"]))
        out = base(P.PKT_LAP_DATA, header, age)
        out["standings"] = rows
        add_names_note(out, info)
        notes = []
        if not rows:
            notes.append("This Lap Data packet lists no cars (num_cars %d)." % header["num_cars"])
        participants = client.latest(P.PKT_PARTICIPANTS)
        if participants is None:
            notes.append("No Participants packet yet (sent every 5 s): names are missing.")
        elif not participants[1]["cars"]:
            notes.append("The newest Participants packet lists no cars (num_cars %d): names are missing."
                         % participants[0]["num_cars"])
        if notes:
            out["note"] = " ".join(notes)
        return clean(out, 3)

    @tool(READ)
    async def get_car_telemetry(
            ctx: Context, car: CarParam = "me",
            include_wheels: Annotated[bool, Field(strict=True, description="Include the four wheels (front two, then rear two): "
                                                             "on_track, grounded, slip_forward, slip_sideways.")] = False
    ) -> dict:
        """Speed, applied pedal and steer, tires and energy of one car or all cars.

        Source: Car Telemetry packet (id 1; at the configured rate; needs Driver Data). Per car: speed_kmh,
        pedal (-1 full brake to 1 full throttle, as applied), steer (-1 left to 1 right, as applied),
        tire_compound (Standard, Boost, Experimental, Fast, Durable, Wet, Golden), tire_health (1 new, 0 worn
        out), puncture, energy (boost or fuel left, 0 to 1; energy_kind says which), wheels_on_track and
        wheels_grounded (count of 4), and with include_wheels the per-wheel data (slip's unit and scale are not
        documented by the game; it is 0 while off the ground).
        """
        got, err = await need(P.PKT_CAR_TELEMETRY, what="Car Telemetry")
        if err:
            return err
        header, body, age = got
        info = names(header)
        cars = []
        indices, sel = select_cars(car, len(body["cars"]), header)
        for i in indices:
            c = body["cars"][i]
            row = label(i, info)
            row.update(speed_kmh=round(c["speed_kmh"], 1), pedal=c["pedal"], steer=c["steer"],
                       tire_compound=c["tire_compound"], tire_health=c["tire_health"], puncture=c["puncture"],
                       energy=c["energy"],
                       wheels_on_track=sum(w["on_track"] for w in c["wheels"]),
                       wheels_grounded=sum(w["grounded"] for w in c["wheels"]))
            if include_wheels:
                row["wheels"] = c["wheels"]
            cars.append(row)
        out = base(P.PKT_CAR_TELEMETRY, header, age)
        out.update(sel)
        add_names_note(out, info)
        out["energy_kind"] = energy_kind()
        out["cars"] = cars
        return clean(out)

    @tool(READ)
    async def get_car_motion(ctx: Context, car: CarParam = "me") -> dict:
        """World position, velocity and orientation of one car or all cars.

        Source: Motion packet (id 0; at the configured rate; needs Driver Data). Per car: position_m [x, y, z]
        (world, metres), velocity_m_s [x right, y up, z forward] in the car's own frame, speed_m_s (its length),
        angular_velocity_rad_s (car frame), pitch_deg (-180..180), roll_deg, yaw_deg.
        """
        got, err = await need(P.PKT_MOTION, what="Motion")
        if err:
            return err
        header, body, age = got
        info = names(header)
        cars = []
        indices, sel = select_cars(car, len(body["cars"]), header)
        for i in indices:
            c = body["cars"][i]
            row = label(i, info)
            row.update(position_m=clean(c["position"], 2), velocity_m_s=clean(c["velocity"], 2),
                       speed_m_s=round(math.sqrt(sum(v * v for v in c["velocity"])), 2),
                       angular_velocity_rad_s=clean(c["angular_velocity"], 3), pitch_deg=round(c["pitch"], 2),
                       roll_deg=round(c["roll"], 2), yaw_deg=round(c["yaw"], 2))
            cars.append(row)
        out = base(P.PKT_MOTION, header, age)
        out.update(sel)
        add_names_note(out, info)
        out["cars"] = cars
        return out

    @tool(READ)
    async def get_car_status(ctx: Context, car: CarParam = "me") -> dict:
        """Current performance ratio per performance area for one car or all cars, with the field median.

        Source: Car Status packet (id 3; twice a second; needs Driver Data). Per car, `performance` maps each area
        (Suspension, EngineTorque, Downforce, Acceleration, Drag, AngularDrag, TractionControl, Weight) to the
        car's current ratio: its car type's base, raised by upgrades and moved by weather, slipstream, abilities and
        failures (the game's own description; the ratio is unitless and has no explicit failure flag).
        field_median gives the median over all cars per area, to tell a car-specific change from a field-wide one
        (weather).
        """
        got, err = await need(P.PKT_CAR_STATUS, what="Car Status")
        if err:
            return err
        header, body, age = got
        info = names(header)
        cars = []
        indices, sel = select_cars(car, len(body["cars"]), header)
        for i in indices:
            row = label(i, info)
            row["performance"] = body["cars"][i]["performance"]
            cars.append(row)
        out = base(P.PKT_CAR_STATUS, header, age)
        out.update(sel)
        add_names_note(out, info)
        out["cars"] = cars
        if body["cars"]:
            out["field_median"] = {a: statistics.median(c["performance"][a] for c in body["cars"])
                                   for a in P.PERFORMANCE_AREAS}
        return clean(out)

    @tool(READ)
    async def get_car_abilities(ctx: Context, car: CarParam = "me") -> dict:
        """Who is boosting, on DRS or in a slipstream; your unlocks and ban tokens.

        Source: Car Abilities packet (id 12; at the configured rate; needs Driver Data or Player Data). Per car:
        active: list of speed_boost, cornering_boost, super_boost, party_mode, drs (DRS open), boost_overspeed,
        recharging (boost ran empty and is recharging); slipstream (0..1, how far into another car's slipstream);
        recharge_left (while recharging, share of the wait still to go: 1 just emptied, 0 ready).
        Only for this machine's cars: unlocked (super_boost, party_mode, drs), api_boosting (an API client is
        holding boost buttons), ban_tokens (card bans left). DRS, slipstream and overspeed are applied by the game;
        they cannot be switched through the API.
        """
        got, err = await need(P.PKT_CAR_ABILITIES, what="Car Abilities")
        if err:
            return err
        header, body, age = got
        info = names(header)
        cars = []
        indices, sel = select_cars(car, len(body["cars"]), header)
        for i in indices:
            c = body["cars"][i]
            row = label(i, info)
            row["active"] = [k for k in ("speed_boost", "cornering_boost", "super_boost", "party_mode", "drs",
                                         "boost_overspeed", "recharging") if c[k]]
            row.update(slipstream=c["slipstream"], recharge_left=c["recharge_left"])
            if i in header["seat_car"]:          # this machine's seats, by this packet's own header (T6)
                row["unlocked"] = [k for k, f in (("super_boost", "super_boost_unlocked"),
                                                  ("party_mode", "party_mode_unlocked"),
                                                  ("drs", "drs_unlocked")) if c[f]]
                row.update(api_boosting=c["api_boosting"], ban_tokens=c["ban_tokens"])
            cars.append(row)
        out = base(P.PKT_CAR_ABILITIES, header, age)
        out.update(sel)
        add_names_note(out, info)
        out["cars"] = cars
        return clean(out)

    def player_view(body, include_blocks):
        out = {"seat": body["seat"], "car": body["car"], "points": body["points"], "hand": body["hand"],
               "upgrades_applied": body["upgrades_applied"], "total_laps": body["total_laps"],
               "laps_remaining": body["laps_remaining"],
               "pit": {"pit_planned": body["pit_planned"], "can_call_now": body["can_pit"],
                       "in_pits": body["in_pits"], "pit_stops": body["pit_stops"]},
               "tires": {"compound": body["tire_compound"], "health": body["tire_health"],
                         "puncture": body["puncture"], "compound_picks_left": body["compound_picks_left"]},
               "energy": body["energy"], "energy_kind": energy_kind()}
        strat = body["strategy"]
        if strat is None:
            out["strategy"] = None
        else:
            s = {k: v for k, v in strat.items() if k not in ("opponents", "near")}
            if include_blocks:
                s["opponents"] = clean(strat["opponents"], 4)
                s["near"] = clean(strat["near"], 4)
                sess = client.latest(P.PKT_SESSION)
                if sess is not None:
                    s["feature_schema_version"] = sess[1]["feature_schema_version"]
                    s["feature_schema_hash"] = sess[1]["feature_schema_hash"]
            out["strategy"] = s
        return out

    @tool(READ)
    async def get_player_state(
            ctx: Context,
            include_strategy_blocks: Annotated[bool, Field(strict=True,
                description="Also return the raw strategy blocks `opponents` (194 floats) and `near` (42 floats), "
                            "the Extreme AI's input in its FeatureSchema order (layout not documented; identified "
                            "by feature_schema_version/hash). Large; off by default.")] = False
    ) -> dict:
        """Player one's own state: upgrade points, the card hand, laps, pit flags, tires, energy, strategy view.

        Source: Player packet (id 4) for seat 0 (at the configured rate; needs Player Data). Returns:
        points (upgrade points to spend), hand (cards on offer: index 0..3 as pick_card/ban_card take it,
        upgrade, facility, tier, banned), upgrades_applied, total_laps, laps_remaining, pit {pit_planned,
        can_call_now (a stop can be called now), in_pits, pit_stops}, tires {compound, health (1 new..0 worn),
        puncture, compound_picks_left (per compound; null = no limit)}, energy (0..1, fuel or boost per
        energy_kind), strategy (null when the seat has no strategy model): start_grid_position, laps_since_pit,
        tire_age_laps, cars_within_5s_ahead/behind, class_position, class_size, class_gap_to_leader/ahead/behind
        (seconds, inferred from the field names), reference_lap_s (seconds).
        """
        got, err = await need(P.PKT_PLAYER, 0, what="Player")
        if err:
            return err
        header, body, age = got
        out = base(P.PKT_PLAYER, header, age)
        out.update(player_view(body, include_strategy_blocks))
        return clean(out)

    def ban_tokens_now():
        ab = client.latest(P.PKT_CAR_ABILITIES)
        me = None if ab is None else ab[0]["seat_car"][0]      # the Car Abilities packet's own index of my car
        if ab is not None and me is not None and me < len(ab[1]["cars"]):
            return ab[1]["cars"][me]["ban_tokens"]
        return None

    @tool(READ)
    async def get_pending_decisions(ctx: Context) -> dict:
        """What currently needs an answer: the card hand, the tire screen, the pit call.

        From the trigger engine's live state (CARD / TIRE events with Decision Events, the Player packet with
        Player Data) plus Car Abilities (ban tokens):
        - cards: open (true; false; null = unknown, e.g. Player Data off and the race resumed), points, hand
          (index, upgrade, facility, tier, banned), seq (the card_offer trigger of this hand: pass it to
          pick_card / ban_card), in_pits, source, closed_because, ban_tokens. Answer with pick_card(index, seq).
        - tire_screen: open, pre_race, current, allowed compounds, picks_left (null = no limit), seq. Closed by a
          successful pick_tire, leaving the pit lane, the car driving off the grid, or the session ending.
          Answer with pick_tire(compound).
        - pit: pit_planned, can_call_now, in_pits (Player packet), and the latest PITW event (sent each new lap).
          Answer with set_pit(true/false).
        - paused: present while the game is paused (it pauses while a card hand waits).
        """
        out = {"ok": True}
        cards = dict(engine.card_state())
        cards["points"] = current_points(cards)
        tokens = ban_tokens_now()
        if tokens is not None or "ban_tokens" not in cards:
            cards["ban_tokens"] = tokens
        out["cards"] = cards
        out["tire_screen"] = dict(engine.tire_state())
        got = client.latest(P.PKT_PLAYER, 0)
        if got is not None:
            header, body, age = got
            out["pit"] = {"pit_planned": body["pit_planned"], "can_call_now": body["can_pit"],
                          "in_pits": body["in_pits"], "age_s": round(age, 2)}
        if client.pit_event is not None:
            out.setdefault("pit", {})["last_pit_window_event"] = client.public_event(client.pit_event)
        paused = paused_info()
        if paused:
            out["paused"] = paused
        if got is None and client.card_event is None and client.pit_event is None and client.tire_event is None:
            fail = await client.no_data_failure(P.PKT_PLAYER, "Player")
            out["note"] = fail.get("explanation")
            out["hint"] = fail.get("hint")
        out["open_decisions"] = open_decisions()
        return clean(out)

    @tool(READ)
    async def get_racing_line(
            ctx: Context,
            max_points: Annotated[int, Field(strict=True, ge=1, le=20, description="How many of the next checkpoints to return "
                                                                      "(1..20).")] = 20
    ) -> dict:
        """The track ahead of your car: the next checkpoints with the racing line point and both track edges.

        Source: Racing Line packet (id 13) for seat 0 (at the configured rate; needs Driver Data). Points run in
        driving order from the checkpoint the car is heading for; during a pit stop they follow the pit lane.
        Per point: racing_line, left, right ([x, y, z] world metres, as in Motion) and width_m (distance between
        the edges). car_position_m is the car's position from the newest Motion packet (found through that
        packet's own seat_car), with car_position_frame_delta = Motion frame minus Racing Line frame.
        """
        got, err = await need(P.PKT_RACING_LINE, 0, what="Racing Line")
        if err:
            return err
        header, body, age = got
        pts = []
        for i, p in enumerate(body["points"][:max_points]):
            width = math.sqrt(sum((a - b) ** 2 for a, b in zip(p["left"], p["right"])))
            pts.append({"i": i, "racing_line": p["racing_line"], "left": p["left"], "right": p["right"],
                        "width_m": width})
        out = base(P.PKT_RACING_LINE, header, age)
        out.update(seat=body["seat"], car=body["car"], points_available=len(body["points"]), points=pts)
        motion = client.latest(P.PKT_MOTION)
        if motion is not None:
            # the car is found through Motion's own seat_car (indices can move between packets)
            mcar = motion[0]["seat_car"][body["seat"]] if body["seat"] < 4 else None
            if mcar is not None and mcar < len(motion[1]["cars"]):
                out["car_position_m"] = motion[1]["cars"][mcar]["position"]
                out["car_position_age_s"] = round(motion[2], 2)
                out["car_position_frame_delta"] = motion[0]["frame"] - header["frame"]
        return clean(out, 2)

    @tool(READ)
    async def get_ai_observations(
            ctx: Context, car: CarParam = "me",
            include_obs: Annotated[bool, Field(strict=True, description="Include the driving net's input vector (up to 339 "
                                                          "floats per car; layout belongs to the net, identified "
                                                          "by `policy`). Large; off by default.")] = False
    ) -> dict:
        """The AI driver's network for one car or all cars: policy id, last action and (optionally) its inputs.

        Source: AI Observations packet (id 8; one per car at the configured rate; needs the AI Observations
        switch, off by default, about 1.1 MB/s at 50 Hz; only sent by the machine that simulates the cars:
        single player, splitscreen or the online host). Per car: policy (first 12 hex digits of the net's
        SHA-256), policy_name, obs_frame (advances each time the net decides), action {pedal, steer} the net last
        chose, obs_count, api_driving, age_s, and obs when include_obs is true. "me" is the newest packet whose
        own header names its car as seat 0's.
        """
        have = client.keys(P.PKT_AI_OBSERVATIONS)
        if not have:
            return await client.no_data_failure(P.PKT_AI_OBSERVATIONS, "AI Observations")
        packets = {k: client.latest(P.PKT_AI_OBSERVATIONS, k) for k in have}
        packets = {k: v for k, v in packets.items() if v is not None}
        ref = min(packets.values(), key=lambda g: g[2]) if packets else None     # the newest packet
        count = min(ref[0]["num_cars"], P.MAX_CARS) if ref is not None else P.MAX_CARS
        sel = car.strip().lower() if isinstance(car, str) else car
        meta = {}
        wanted = None
        if sel == "all":
            keys = have
        elif sel is None or sel in ME_WORDS:
            # my car: the newest packet whose own header names its car as seat 0's (T5)
            mine = sorted((g[2], k) for k, g in packets.items() if g[0]["seat_car"][0] == g[1]["car"])
            keys = [mine[0][1]] if mine else []
            wanted = "your car (a packet whose own header names its car as seat 0's)"
        else:
            # indices are checked against num_cars of the newest AI Observations packet (V-T9)
            keys, meta = select_cars(car, count, None)
            got = packets.get(keys[0])
            if got is not None and meta.get("selector"):
                lap = client.latest(P.PKT_LAP_DATA)
                add_lap_delta(meta, got[0], None if lap is None else lap[0]["frame"])
            wanted = "car index %d" % keys[0]
        cars = []
        newest = None
        for key in keys:
            got = client.latest(P.PKT_AI_OBSERVATIONS, key)
            if got is None:
                continue
            header, body, age = got
            newest = got if newest is None or age < newest[2] else newest
            row = label(body["car"], names(header))
            row.update(age_s=round(age, 2), frame=header["frame"], policy=body["policy"],
                       policy_name=body["policy_name"], obs_frame=body["obs_frame"],
                       action={"pedal": body["action"][0], "steer": body["action"][1]},
                       obs_count=len(body["obs"]), api_driving=body["api_driving"])
            if include_obs:
                row["obs"] = clean(body["obs"], 4)
            cars.append(row)
        if not cars:
            # packets exist, just not for the car asked for (V-D12): say so, without guessing why
            return {"ok": False, "error_reason": "no_data",
                    "explanation": "No AI Observations packet for %s has arrived; packets have arrived for car "
                                   "indices %s." % (wanted, ", ".join(map(str, sorted(packets)))),
                    "hint": "The game sends one AI Observations packet per car it simulates, keyed by car index "
                            "(indices change with race position). Call get_ai_observations with car=\"all\" to see "
                            "every car that has data.",
                    "cars_with_data": sorted(packets)}
        out = base(P.PKT_AI_OBSERVATIONS, newest[0], newest[2])
        out.update(meta)
        out["cars"] = cars
        out["names_note"] = ("AI Observations come one packet per car, keyed by the car index in each packet; the game "
                             "re-orders car indices by race position mid-session (seen in 0.88.2), so rows of "
                             "different ages can belong to cars that have since swapped indices. Compare age_s.")
        return clean(out)

    # ================================================================== events

    EventCodes = Annotated[Optional[List[SStr]], Field(
        description="Only these event codes (any of SSTA, SEND, CARD, TIRE, LAPC, PITW, PITI, PITO, FINI); "
                    "omit for all.")]

    def check_codes(codes):
        """Upper-cased codes; ValueError (bad_argument) for anything that is not 4 ASCII characters, and the list
        of well-formed codes this server does not know (a newer game may add codes, so those are only warned)."""
        if codes is None:
            return None, []
        out = []
        for code in codes:
            c = code.strip().upper()
            if len(c) != 4 or not c.isascii() or not c.isalnum():
                raise ValueError("event code %r is not a 4-character code; known codes: %s"
                                 % (code, ", ".join(R.EVENT_INFO)))
            out.append(c)
        return out, [c for c in out if c not in R.EVENT_INFO]

    def unknown_codes_warning(out, unknown):
        if unknown:
            out["warning"] = ("Unknown event code(s) %s: this server knows %s. A newer game may send them; check the "
                              "spelling." % (", ".join(unknown), ", ".join(R.EVENT_INFO)))
        return out

    @tool(READ)
    async def get_events(
            ctx: Context,
            since_seq: Annotated[int, Field(strict=True, ge=0, description="Return events with seq greater than this "
                                                              "(0 = everything buffered).")] = 0,
            codes: EventCodes = None,
            limit: Annotated[int, Field(strict=True, ge=1, le=500, description="At most this many (the newest).")] = 50,
    ) -> dict:
        """Buffered game events (oldest first), each with a sequence number.

        Source: Event packet (id 7; sent when it happens; needs Decision Events). The server keeps the last 500
        events of the current process with increasing `seq`. Codes: SSTA (cars on the grid), SEND (session
        ended), CARD (points + new hand: points, hand), TIRE (tire screen opened: pre_race, current, allowed,
        picks_left with null = no limit), LAPC (new lap: lap, position), PITW (each new lap: lap, pit_planned,
        can_pit), PITI / PITO (entering / leaving the pit lane), FINI (player took the flag: position).
        `seat` is the local seat (0 = player one) or null for the whole session. Returns events, last_seq (pass
        it as since_seq next time), and buffer_starts_at_seq (older ones were dropped).
        """
        codes, unknown = check_codes(codes)
        found = client.events_after(since_seq, codes, limit)
        out = {"ok": True, "events": [client.public_event(e) for e in found], "last_seq": client.event_seq,
               "buffer_starts_at_seq": client.oldest_seq()}
        if not client.events and client.event_seq == 0:
            if client._tele_transport is None:
                return client.listener_failure()
            out["note"] = ("No events received yet. Events need Decision Events and a running session; "
                           "get_api_status shows the switches.")
        return unknown_codes_warning(out, unknown)

    @tool(WAIT)
    async def wait_for_event(
            ctx: Context,
            codes: EventCodes = None,
            timeout_s: Annotated[float, Field(strict=True, gt=0, le=WAIT_EVENT_MAX_S, description="How long to wait, seconds "
                                                                                     "(max 60; some clients cut a "
                                                                                     "call at ~80 s).")] = 30.0,
            since_seq: Annotated[Optional[SInt], Field( ge=0, description="Return events after this seq (an event "
                                                                        "that already arrived counts). Omit to wait "
                                                                        "for events newer than now.")] = None,
    ) -> dict:
        """Block until a matching game event arrives (or timeout_s passes).

        Same events as get_events (needs Decision Events). Not needed for the race loop: wait_for_trigger already
        wakes on the tire screen (tire_screen trigger) and on card hands. Use it for other events (PITO, LAPC,
        ...). Codes must be 4 characters; unknown ones give a warning. Returns
        {ok, matched: true, events, last_seq} or {ok, matched: false, last_seq} on timeout. Sends MCP progress
        notifications while waiting. Prefer wait_for_trigger for the main loop.
        """
        if client._tele_transport is None:
            return client.listener_failure()
        codes, unknown = check_codes(codes)

        async def tick(elapsed, total):
            with contextlib.suppress(Exception):
                await ctx.report_progress(elapsed, total, "waiting for event")

        found = await client.wait_for_event(codes, timeout_s, since_seq, on_tick=tick,
                                            tick=min(PROGRESS_INTERVAL_S, timeout_s))
        out = {"ok": True, "matched": bool(found), "events": [client.public_event(e) for e in found],
               "last_seq": client.event_seq}
        if not found:
            out["note"] = "No matching event within %g s." % timeout_s
            if not client.in_session():
                out["note"] += " No telemetry is arriving (no session, or the game / API is off)."
        return unknown_codes_warning(out, unknown)

    # ================================================================== snapshot

    def snapshot_context():
        """Compact race context for trigger results and the snapshot (G8: laps, points and hand from the trigger
        engine, so they agree with the triggers)."""
        ctx_out = {}
        ctx_out["my_car"] = client.my_car()
        paused = paused_info()
        if paused:
            ctx_out["paused"] = paused
        sess = client.latest(P.PKT_SESSION)
        if sess is not None:
            s = sess[1]
            ctx_out["session"] = {"type": s["session_type"], "scene": s["scene"], "total_laps": s["total_laps"],
                                  "weather": s["weather"], "energy_kind": s["energy"],
                                  "public_lobby": s["public_lobby"]}
        lap = client.latest(P.PKT_LAP_DATA)
        lap_me = None if lap is None else lap[0]["seat_car"][0]      # Lap Data's own index of my car
        if lap is not None and lap_me is not None and lap_me < len(lap[1]["cars"]):
            cars = lap[1]["cars"]
            c = cars[lap_me]
            ctx_out.update(position=c["position"], gap_to_leader_s=c["gap_to_leader"],
                           interval_ahead_s=c["interval_ahead"], interval_behind_s=c["interval_behind"],
                           in_pits=c["in_pits"], finished=c["finished"], pit_stops=c["pit_stops"])
            info = names(lap[0])
            for key, pos in (("car_ahead", (c["position"] or 0) - 1), ("car_behind", (c["position"] or 0) + 1)):
                found = [i for i, o in enumerate(cars) if pos >= 1 and o["position"] == pos]
                if found:
                    row = label(found[0], info)
                    row.update(selector="ahead" if key == "car_ahead" else "behind", position=pos)
                    ctx_out[key] = row
            ctx_out["cars_in_race"] = sum(1 for o in cars if o["position"])
            ctx_out["lap_data_age_s"] = round(lap[2], 2)
            if info.note:
                ctx_out["names_note"] = info.note
            ctx_out["car_index_note"] = ("car indices are valid for this instant only; use car=\"ahead\" / \"behind\" "
                                         "in the query tools")
        laps = engine.laps()
        ctx_out.update(laps_completed=laps.get("laps_completed"), total_laps=laps.get("total_laps"),
                       laps_remaining=laps.get("laps_remaining"), lap_source=laps.get("lap_source"))
        ended = session_ended_info()
        if ended:
            ctx_out["session_ended"] = True
            ctx_out["laps_note"] = "the session has ended: lap values are the state at the end"
        elif paused:
            ctx_out["laps_note"] = ("frozen while the game is paused: lap values are the state at the pause "
                                    "(session_time is not advancing)")
        lapc = next((e for e in reversed(client.events) if e["code"] == "LAPC" and e["seat"] == 0), None)
        if lapc is not None:
            # a timing hint only: the README does not say whether LAPC's lap is laps completed or the new lap
            ctx_out["last_lap_event"] = {"lap": lapc.get("lap"), "position": lapc.get("position"),
                                         "age_s": round(time.monotonic() - lapc["_received"], 1)}
        cs = engine.card_state()
        ctx_out["points"] = current_points(cs)
        ctx_out["cards_open"] = cs.get("open")
        for key in ("player_data_older_than_trigger",):
            if cs.get(key):
                ctx_out[key] = cs[key]
        pl = client.latest(P.PKT_PLAYER, 0)
        if pl is not None:
            b = pl[1]
            ctx_out.update(tire={"compound": b["tire_compound"], "health": b["tire_health"],
                                 "puncture": b["puncture"]},
                           energy=b["energy"], pit_planned=b["pit_planned"], can_pit=b["can_pit"])
            ctx_out["player_data_age_s"] = round(pl[2], 2)
        else:
            tel = client.latest(P.PKT_CAR_TELEMETRY)
            tme = None if tel is None else tel[0]["seat_car"][0]
            if tel is not None and tme is not None and tme < len(tel[1]["cars"]):
                c = tel[1]["cars"][tme]
                ctx_out.update(tire={"compound": c["tire_compound"], "health": c["tire_health"],
                                     "puncture": c["puncture"]}, energy=c["energy"])
        age = client.telemetry_age()
        ctx_out["telemetry_age_s"] = None if age is None else round(age, 2)
        return clean(ctx_out)

    @tool(READ)
    async def get_race_snapshot(ctx: Context) -> dict:
        """One compact overview of the race for your car: session, position, gaps, laps, tires, energy, decisions.

        Combines Session, Lap Data, Participants, Player and Car Telemetry (whichever are switched on) with the
        trigger engine's state: session type/scene/total_laps/weather/energy_kind, position, gaps (seconds; see
        get_standings about interval_behind's sign), laps_completed / total_laps / laps_remaining with lap_source
        and lap_data_age_s (labelled frozen while the game is paused), car_ahead / car_behind (car index valid for
        this instant only, name when known, selector "ahead" / "behind" to use with the query tools), tire
        (compound, health 1..0, puncture), energy (0..1), points, pit_planned, can_pit, in_pits; plus the open
        decisions (card hand, tire screen), the tire-wear forecast of the trigger engine, paused, and running
        boost/drive holds.
        """
        if client._tele_transport is None:
            return client.listener_failure()
        if client.latest(P.PKT_SESSION) is None and client.latest(P.PKT_LAP_DATA) is None \
                and client.latest(P.PKT_PLAYER, 0) is None:
            return await client.no_data_failure(P.PKT_SESSION, "race")
        out = {"ok": True}
        out.update(snapshot_context())
        cs = engine.card_state()
        if cs.get("open") is not False and cs.get("hand"):
            out["cards_on_offer"] = {"open": cs.get("open"), "hand": cs.get("hand"), "seq": cs.get("seq")}
        ts = engine.tire_state()
        if ts.get("open"):
            out["tire_screen_open"] = ts
        out["open_decisions"] = open_decisions()
        out["tire_forecast"] = clean(engine.tire_prediction())
        holds = client.hold_status()
        if holds:
            out["holds"] = holds
        return out

    # ================================================================== reference

    @tool(READ_LOCAL)
    async def get_reference(
            ctx: Context,
            topic: Annotated[str, Field(strict=True, description="One of: all, " + ", ".join(R.TOPICS)
                                                    + ". Aliases: errors, events, cameras, tires, cards, "
                                                      "switches.")] = "all"
    ) -> dict:
        """Static reference tables from the Game API documentation (no game needed).

        Topics: compounds, upgrades, facilities, session_types, car_types, performance_areas, camera_types,
        participant_kinds, error_reasons (every refusal reason with explanation and what to do), event_codes,
        packets (id, size, rate, needed switch), commands (id, size, payload, needed switch, reply), settings
        (in-game switches and which tools need them), rules (seat rule, public lobby, holds, drive
        consequences), triggers (what fires each trigger, the switches it needs, which are heuristic), observed
        (behaviour seen against the live game, 0.88.2, that the README does not state or contradicts). Also
        available as resources bc://reference/{topic}.
        """
        try:
            return {"ok": True, "topic": topic, "data": R.reference(topic)}
        except KeyError:
            return failure("bad_argument", detail="(unknown topic %r; one of: all, %s)" % (topic, ", ".join(R.TOPICS)))

    # ================================================================== triggers

    active_waits = []        # one record per running wait_for_trigger call (concurrent calls share new entries)

    def _wait_note(entries):
        types = [e.get("type") for e in entries]
        if "card_offer" in types and any(e.get("type") == "tire_screen" and e.get("open") for e in entries):
            return ("tire screen open in the pit lane: pick_tire before pick_card (a pick was seen refused with "
                    "cards_unavailable while the tire screen was open, 0.88.2); the hand stays open")
        return None

    @tool(WAIT)
    async def wait_for_trigger(
            ctx: Context,
            timeout_s: Annotated[Optional[SFloat], Field(
                gt=0, description="Longest wait in seconds; default 55, never more than 60 (BC_WAIT_MAX_S can only "
                                  "lower it). Some MCP clients (the Claude desktop app's device bridge) cut a call at "
                                  "about 80 s and its result is lost.")] = None,
            since_seq: Annotated[Optional[SInt], Field(
                ge=0, description="Acknowledge: the last_seq of the previous result, passed after you acted on it. "
                                  "Triggers up to it are not returned again; anything after it is (a result your "
                                  "client lost is returned again). Omit if you lost track.")] = None,
    ) -> dict:
        """Block until the race needs a decision from you; this is the main loop of the race engineer.

        Returns at once when a trigger is due, else blocks until one is (or timeout_s). Delivery is at least
        once: a trigger after since_seq that was returned before comes back (resent: true) at most every 20 s,
        up to 3 more times while it is an open decision (card hand, tire screen), once for other types when
        since_seq is passed. Answered, closed, superseded (a newer hand, the car pitted since) and old-session
        entries are not returned; `skipped` counts them. Enabled triggers (BC_TRIGGERS; default all four):
        - card_offer: a new hand of upgrade cards (seq, points, hand with index/upgrade/facility/tier/banned,
          ban_tokens, in_pits, open). From the CARD event; from the Player packet when Decision Events is off,
          and also with it on when no CARD event announced a hand the Player packet shows for 2 s.
          Answer at once with pick_card(index, seq) (ban_card(index, seq) first if wanted): the game stops time
          while a hand waits. In the pit lane a pick was seen refused with cards_unavailable (0.88.2): pick the
          tire first; the hand stays open.
        - tire_screen: the tire selection screen opened (pre_race, current, allowed, picks_left): pick_tire.
        - car_damaged: signal "puncture" (definite: two consecutive samples show the puncture flag) or
          "performance_drop"
          (heuristic: performance areas of your car fell suddenly while the field's did not and no ability was
          active; heuristic=true). Payload includes can_pit, pit_planned, in_pits, laps_remaining.
        - tires_wont_last (heuristic forecast): tire health is predicted to fall to BC_TIRE_MARGIN (0.05) or below
          by the end of the NEXT lap (or is at/below BC_TIRE_FLOOR when there is no forecast). Payload:
          tire_health, wear_per_lap and how it was derived, predicted_health_end_of_next_lap, laps_remaining,
          can_pit, pit_planned, compound, compound_picks_left, pit_lane_time_loss_s. Silent on the last lap, in
          the pit lane, after the flag or when a stop is already called.
        - session_ended: the session is over for you (SEND, or FINI for seat 0; code, position); one per session,
          kept even when the next session starts first.
        Result: {status: "triggered" | "session_ended" | "timeout", triggered, triggers: [{seq, type, age_s,
        resent, open, lap, ...payload}] oldest first, last_seq (pass it as since_seq next time, after acting),
        skipped, open_decisions (the open card hand / tire screen right now, on every result), context (position,
        laps, gaps, car_ahead/behind, tire, energy, points, paused, ...), notes}. Sends MCP progress
        notifications while waiting. The server never sends a gameplay command on its own (only the read-only
        hello): nothing happens unless you call a command.
        """
        if client._tele_transport is None:
            return client.listener_failure()
        cap = min(float(config.wait_max_s or WAIT_HARD_MAX_S), WAIT_HARD_MAX_S)
        timeout = cap if timeout_s is None else min(timeout_s, cap)
        loop = asyncio.get_running_loop()
        me = {"mailbox": [], "wake": loop.create_future()}
        active_waits.append(me)
        start = time.monotonic()
        deadline = start + timeout
        last_progress = start
        skipped = {}
        notes = []
        acked = None
        ack = since_seq
        try:
            while True:
                now = time.monotonic()
                r = engine.collect(ack, now)
                ack = None                       # the acknowledgement is applied once per call
                acked = r.get("last_seq", r.get("acked", acked))
                for k, v in (r.get("skipped") or {}).items():
                    skipped[k] = skipped.get(k, 0) + (v or 0)
                for n in r.get("notes") or []:
                    if n not in notes:
                        notes.append(n)
                entries = list(r.get("entries") or [])
                if entries:
                    for other in active_waits:   # a concurrent wait gets the same new entries
                        if other is not me:
                            other["mailbox"].extend(dict(e) for e in entries)
                            if not other["wake"].done():
                                other["wake"].set_result(None)
                elif me["mailbox"]:
                    entries = me["mailbox"]
                if entries:
                    seen = set()
                    unique = []
                    for e in sorted(entries, key=lambda e: e["seq"]):
                        if e["seq"] not in seen:
                            seen.add(e["seq"])
                            unique.append(e)
                    return trigger_result(unique, r, skipped, notes, start)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                wait = min(remaining, PROGRESS_INTERVAL_S)
                due = engine.next_due_in(time.monotonic())
                if due is not None:
                    wait = min(wait, max(due, 0.0) + 0.01)
                fut = engine.waiter()
                if me["wake"].done():
                    me["wake"] = loop.create_future()
                try:
                    await asyncio.wait({fut, me["wake"]}, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    engine.discard_waiter(fut)
                    if not fut.done():
                        fut.cancel()
                now = time.monotonic()
                if now - last_progress >= PROGRESS_INTERVAL_S * 0.99 and now < deadline:
                    last_progress = now
                    with contextlib.suppress(Exception):
                        await ctx.report_progress(round(now - start, 1), round(timeout, 1), "waiting for a trigger")
        finally:
            active_waits.remove(me)
        out = {"ok": True, "status": "timeout", "triggered": False, "triggers": [],
               "waited_s": round(time.monotonic() - start, 1),
               "last_seq": acked if acked is not None else getattr(engine, "acked", 0),
               "skipped": skipped, "open_decisions": open_decisions()}
        paused = paused_info()
        if paused:
            out["paused"] = paused
        if notes:
            out["notes"] = notes
        out["context"] = snapshot_context()
        out["next"] = ("Nothing new needed a decision. If open_decisions is not empty, answer it now; then call "
                       "wait_for_trigger again with since_seq = last_seq.")
        if not client.in_session():
            out["telemetry"] = ("No telemetry is arriving: the game sends none outside a session, or the API / "
                                "its switches are off (see get_api_status).")
        return out

    def trigger_result(entries, r, skipped, notes, start):
        now = time.monotonic()
        rows = [clean(e) for e in entries]
        ended = [e for e in rows if e.get("type") == "session_ended"]
        status = r.get("status") if r.get("entries") else None
        if status not in ("triggered", "session_ended"):
            status = "session_ended" if rows and rows[-1].get("type") == "session_ended" else "triggered"
        if r.get("entries"):
            # the engine's cursor: it never jumps over an earlier returned entry still waiting for its resend
            last = r.get("last_seq") or 0
        else:                            # entries handed over by a concurrent wait (they were returned there)
            last = max([r.get("last_seq") or 0] + [e["seq"] for e in rows])
        out = {"ok": True, "status": status, "triggered": any(e.get("type") != "session_ended" for e in rows),
               "triggers": rows, "last_seq": last, "skipped": skipped, "open_decisions": open_decisions(),
               "waited_s": round(now - start, 1)}
        note = _wait_note(rows)
        if note:
            notes = notes + [note]
        if any(e.get("resent") for e in rows):
            notes = notes + ["Entries marked resent were returned before and are not acknowledged; a decision "
                             "still open (open: true or null) needs an answer now."]
        if notes:
            out["notes"] = notes
        if ended:
            out["session_end"] = ended[-1]
            out["next"] = ("The session is over for you: summarise it. Call wait_for_trigger again (since_seq = "
                           "last_seq) only to wait for the next session.")
        out["context"] = snapshot_context()
        return out

    @tool(READ)
    async def get_triggers(
            ctx: Context,
            since_seq: Annotated[int, Field(strict=True, ge=0, description="Triggers with seq greater than this.")] = 0,
            limit: Annotated[int, Field(strict=True, ge=1, le=200, description="At most this many (the newest).")] = 20,
    ) -> dict:
        """Recent triggers kept by the server, without waiting and without changing what wait_for_trigger returns.

        Each: seq, type (card_offer, car_damaged, tires_wont_last, tire_screen, session_ended), age_s, superseded,
        open (decisions: true / false / null = unknown), lap, session_time_s and the payload described in
        wait_for_trigger. Entries of an earlier session are kept (marked stale_session) until the queue's size
        limit; sequence numbers only increase within this server process. Also: last_seq, acked_up_to_seq (the
        highest since_seq passed to wait_for_trigger), open_decisions.
        """
        now = time.monotonic()
        found = [e for e in engine.triggers if e["seq"] > since_seq][-limit:]
        return {"ok": True, "triggers": [clean(engine.public(e, now)) for e in found],
                "last_seq": engine.seq, "acked_up_to_seq": getattr(engine, "acked", None),
                "open_decisions": open_decisions()}

    @tool(READ_LOCAL)
    async def get_trigger_config(ctx: Context) -> dict:
        """The trigger engine's configuration and live state, to judge how much to trust a trigger.

        Returns enabled triggers, thresholds (tire_margin, tire_floor, perf_drop, perf_window_s; all tunable
        heuristics of this server, not game values) and state: which source card offers come from (CARD event or
        Player fallback), the card and tire-screen state, puncture flag and arming, alarmed performance areas,
        the lap count and its source with the last lap observations (LAPC lap vs Lap Data vs Player, to settle
        what LAPC's lap means), and the tire forecast (current health, per-lap wear samples on this tire set,
        wear_per_lap and how it was derived, lap time and its source, predicted health at the end of the next
        lap, armed / silenced reason; the floor only fires when there is no forecast).
        """
        out = {"ok": True}
        out.update(config.triggers.public())
        out["state"] = clean(engine.state(), 4)
        out["laps"] = clean(engine.laps(), 4)
        return out

    # ================================================================== commands

    CARD_SEQ_DESC = ("The seq of the card_offer trigger you are answering (optional). If the hand on offer is no "
                     "longer that trigger's hand, the command is refused with hand_changed and the current hand is "
                     "returned, so a stale index never picks the wrong card.")
    CARDS_UNAVAILABLE_HINT = ("Seen in 0.88.2 in the pit lane: pick_tire first (if the tire screen is open) or retry "
                              "after the car leaves the pit lane (PITO). The hand stays open.")

    def check_hand(seq, command):
        """None when the pick / ban may go ahead, else the hand_changed refusal (X11)."""
        if seq is None:
            return None
        cs = engine.card_state()
        want = engine.hand_sig_for_seq(seq)
        if want is not None and cs.get("sig") == want:
            return None
        why = ("(trigger %d is not a card_offer this server knows)" % seq if want is None
               else "(the hand on offer is not the hand of trigger %d)" % seq)
        return failure("hand_changed", command, why,
                       current_hand={k: cs.get(k) for k in ("open", "points", "hand", "seq", "in_pits")})

    @tool(_write(True, False, "Pick an upgrade card"))
    async def pick_card(
            ctx: Context,
            index: Annotated[int, Field(strict=True, ge=0, le=3, description="Card index in the current hand, 0..3 (the `index` "
                                                                "field of the hand in the card_offer trigger, "
                                                                "get_pending_decisions or get_player_state).")],
            seq: Annotated[Optional[SInt], Field(ge=0, description=CARD_SEQ_DESC)] = None,
    ) -> dict:
        """Pick a card from the hand on offer, spending one upgrade point (command pick_card, id 2, seat 0).

        Pass seq (the card_offer trigger's seq) so a hand that changed meanwhile is refused with hand_changed
        (the current hand is returned) instead of picking whatever card now has that index. Needs Player Input,
        points > 0 and a card that is not banned. The game answers Ack or Error; possible errors: no_points,
        invalid_index, cards_unavailable (observed in 0.88.2 while the car was in the pit lane with a hand on
        offer: pick the tire first or retry after the pit lane; the hand stays open), card_banned,
        input_disabled, public_lobby, no_player. The game stops time while a hand waits, so answer promptly. On
        no_reply the outcome is unknown: check get_pending_decisions before retrying.
        Returns {ok, picked: the card as last seen in the hand (when known)} or {ok: false, error_reason,
        explanation, hint}. After a pick the game may offer a new hand (a new card_offer trigger) while points
        remain.
        """
        refused = check_hand(seq, "pick_card")
        if refused:
            return refused
        card = None
        hand = engine.card_state().get("hand") or []
        for c in hand:
            if c.get("index") == index:
                card = c
        result = await client.pick_card(index)
        engine.answer_card(result)
        if result["ok"] and card is not None:
            result["picked"] = card
        if result.get("error_reason") == "cards_unavailable":
            result["hint"] = CARDS_UNAVAILABLE_HINT
            result["hand_still_open"] = True
        return result

    @tool(_write(True, False, "Ban an upgrade card"))
    async def ban_card(
            ctx: Context,
            index: Annotated[int, Field(strict=True, ge=0, le=3, description="Card index in the current hand, 0..3.")],
            seq: Annotated[Optional[SInt], Field(ge=0, description=CARD_SEQ_DESC)] = None,
    ) -> dict:
        """Ban a card in the current hand, spending one ban token (command ban_card, id 7, seat 0).

        This is the card screen's own ban. Unlike a pick it keeps your turn: you still have to pick a card. It
        cannot ban the last card left to pick. Pass seq (the card_offer trigger's seq) to be refused with
        hand_changed if the hand changed. Needs Player Input. Errors: ban_refused (no ban tokens left, last card
        left to pick, or a card that can't be banned), invalid_index, input_disabled, public_lobby, no_player.
        Ban tokens left: get_car_abilities / get_pending_decisions (ban_tokens).
        """
        refused = check_hand(seq, "ban_card")
        if refused:
            return refused
        return await client.ban_card(index)

    @tool(_write(True, True, "Call or cancel a pit stop"))
    async def set_pit(
            ctx: Context,
            pit: Annotated[bool, Field(strict=True, description="true calls a pit stop, false cancels a called one.")],
    ) -> dict:
        """Call (true) or cancel (false) a pit stop for your car (command set_pit, id 3, seat 0).

        Needs Player Input. Check can_call_now (get_player_state) / can_pit (trigger payload or PITW event)
        first: a call the car can't make is refused with `refused`. Other errors: input_disabled, public_lobby,
        no_player. When the car enters the pits the tire screen opens (a tire_screen trigger from
        wait_for_trigger): then use pick_tire. The game does not document what it fits if no compound is picked.
        """
        return await client.set_pit(pit)

    @tool(_write(True, False, "Pick a tire compound"))
    async def pick_tire(
            ctx: Context,
            compound: Annotated[Union[SStr, SInt], Field(
                description="Compound name (case-insensitive): Standard, Boost, Experimental, Fast, Durable, Wet, "
                            "Golden; or its index 0..6.")],
    ) -> dict:
        """Pick a tire compound while the tire screen is open (command pick_tire, id 4, seat 0).

        The tire screen opens on the starting grid, in the qualifying box and at a pit stop (TIRE event: its
        `allowed` list and `picks_left` say what can be chosen; null = no limit). Needs Player Input and an open
        tire screen. Errors: tire_select_closed (screen not open: wait for the tire_screen trigger),
        compound_refused (not allowed now / no picks left), invalid_compound, input_disabled, public_lobby.
        """
        try:
            index = P.compound_index(compound)
        except ValueError as e:
            return failure("bad_argument", "pick_tire", "(%s)" % e)
        result = await client.pick_tire(index)
        engine.answer_tire(result)
        result["compound"] = P.COMPOUNDS[index]
        return result

    @tool(_write(True, False, "Hold boost buttons (spends energy)"))
    async def boost(
            ctx: Context,
            speed: Annotated[bool, Field(strict=True, description="Hold the speed boost button.")] = False,
            cornering: Annotated[bool, Field(strict=True, description="Hold the cornering boost button. With speed this becomes "
                                                         "a super boost once the car has unlocked it.")] = False,
            party_mode: Annotated[bool, Field(strict=True, description="Hold party mode (only exists in fuel races, once "
                                                          "unlocked).")] = False,
            duration_s: Annotated[float, Field(strict=True, ge=MIN_HOLD_S, le=MAX_BOOST_S,
                                               description="How long to hold, seconds (0.05..30; a shorter hold "
                                                           "would send no command).")] = 2.0,
    ) -> dict:
        """Hold boost buttons for duration_s seconds (command boost, id 8, seat 0, re-sent at 50 Hz).

        The game holds the buttons only while commands keep arriving (released after 0.25 s of silence), so the
        server re-sends in the background and releases (buttons 0) at the end. A new boost call replaces the
        running hold; stop_boost ends it early. The game answers boost only with errors, so the result is
        `ok` when the game answered a ping and sent no error within ~0.2 s (presumed success); confirm with
        get_car_abilities (active, api_boosting). Errors: assist_on (a Boost / Party Mode Assist owns the
        buttons), input_disabled, public_lobby, no_player. An error during the hold ends it and is reported by
        get_api_status (holds). Needs Player Input. At least one button must be true.
        """
        buttons = P.boost_buttons(speed, cornering, party_mode)
        if not buttons:
            return failure("bad_argument", "boost", "(no button selected; use stop_boost to let go)")
        result = await client.boost(buttons, duration_s)
        if result.get("ok"):
            result["note"] = ("Presumed success: the game only answers boost when it refuses it, and it stayed "
                              "silent. Verify with get_car_abilities.")
        return result

    @tool(_write(False, True, "Release boost buttons"))
    async def stop_boost(ctx: Context) -> dict:
        """Stop the running boost hold and release every boost button now (command boost with no buttons).

        Safe to call when no hold is running. Returns {ok, was_holding}. Silent-on-success like boost.
        """
        return await client.stop_boost()

    @tool(_write(False, False, "Change camera"))
    async def set_camera(
            ctx: Context,
            camera: Annotated[Union[SStr, SInt], Field(
                description="Camera name (case-insensitive): Helicopter, TrackSide, Drone, Onboard_TCam, "
                            "Onboard_Rear, Onboard_Front, Onboard_Driver, Onboard_TVAngle, Chase, Onboard_BattleCam "
                            "(TrackOverview is not offered in races); or next, previous, automatic; or an index "
                            "(0..10, 253 previous, 254 next, 255 automatic).")],
    ) -> dict:
        """Change your own camera view (command set_camera, id 9, seat 0).

        Only changes your view, so it also works in a public lobby. The command table lists Player Input as its
        requirement. previous / next work like the camera button: while the view follows another car, the first
        press brings it back to your car in the same camera type. Errors: camera_unavailable (not a camera this
        race offers), input_disabled, no_player.
        """
        try:
            index = P.camera_index(camera)
        except ValueError as e:
            return failure("bad_argument", "set_camera", "(%s)" % e)
        result = await client.set_camera(index)
        name = {P.CAMERA_PREVIOUS: "previous", P.CAMERA_NEXT: "next", P.CAMERA_AUTOMATIC: "automatic"}
        result["camera"] = name.get(index) or P.CAMERA_TYPES[index]
        return result

    if config.allow_drive:
        @tool(_write(True, False, "Drive the car (withholds achievements)"))
        async def drive(
                ctx: Context,
                pedal: Annotated[float, Field(strict=True, ge=-1.0, le=1.0, description="-1 full brake to 1 full throttle.")],
                steer: Annotated[float, Field(strict=True, ge=-1.0, le=1.0, description="-1 full left to 1 full right.")],
                duration_s: Annotated[float, Field(strict=True, ge=MIN_HOLD_S, le=MAX_DRIVE_S,
                                                   description="How long to hold, seconds (0.05..10; a shorter "
                                                               "hold would send no command).")] = 1.0,
        ) -> dict:
            """Take over pedal and steering of your car for duration_s seconds (command drive, id 5, 50 Hz).

            WARNING: Achievements, Steam stats and leaderboard submissions are withheld for the WHOLE race weekend
            in which drive steered a car (a race after qualifying on the same track is the same weekend). Only
            works when this machine simulates the car (single player, splitscreen, or a private lobby you host;
            an online client gets not_host). Needs Driver Input. The built-in driver takes over again 0.25 s
            after the last command; the server re-sends at 50 Hz, then sends release_drive. A new drive call
            replaces the running hold. Silent on success: `ok` means a ping was answered and no error came within
            ~0.2 s; confirm with get_standings (api_driving). Errors: not_host, invalid_axis, input_disabled,
            public_lobby, no_player. Only enabled because the server runs with BC_ALLOW_DRIVE=1.
            """
            return await client.drive(pedal, steer, duration_s)

        @tool(_write(False, True, "Hand the car back to the AI driver"))
        async def release_drive(ctx: Context) -> dict:
            """Stop the drive hold and hand the car back to the built-in driver now (command release_drive, id 6).

            Needs Driver Input. Answered with Ack. Returns {ok, was_holding}.
            """
            return await client.release_drive()

    # ================================================================== race results

    if state.results is not None:
        store = state.results.store

        def results_failure(e):
            out = failure(getattr(e, "reason", "stored_file_unreadable"), detail="(%s)" % getattr(e, "explanation", e))
            out.update({k: v for k, v in (getattr(e, "details", None) or {}).items() if k not in out})
            return out

        @tool(READ)
        async def list_race_results(
                ctx: Context,
                limit: Annotated[int, Field(strict=True, ge=1, le=200, description="At most this many, newest first.")] = 20,
        ) -> dict:
            """Race weekends the game uploaded to this server's results receiver, newest first.

            The game uploads a finished race weekend only when you host and http://127.0.0.1:<BC_RESULTS_PORT>/
            (with BC_RESULTS_TOKEN as its token, if set) is listed under Race Results in Settings > Telemetry.
            Nothing is retried by the game, and the receiver only runs while this server runs. At most
            BC_RESULTS_KEEP uploads (default 100) are kept; this lists the newest `limit` (max 200). Per upload: id
            (a UTC time; use with get_race_result), received_utc (when this server saved it), bytes, compressed /
            stored_as (json, or zip: the body is stored as received), and from the upload's X-BC-* headers (not
            the document): format, game_version, session_count (integer); problems (empty, or why the document
            looked malformed or has a format other than 2).
            """
            try:
                results = await asyncio.to_thread(store.list, limit)
            except ResultsError as e:
                return results_failure(e)
            return {"ok": True, "receiver": state.results.status(), "results": results}

        @tool(READ)
        async def get_race_result(
                ctx: Context,
                result_id: Annotated[str, Field(strict=True, description='An id from list_race_results, or "latest".')] = "latest",
                include_decisions: Annotated[bool, Field(strict=True, description="Include the decision log entries (without "
                                                                      "their state and offered feature vectors).")] = False,
                max_decisions: Annotated[int, Field(strict=True, ge=1, le=1000, description="Decision entries per session when "
                                                                               "included.")] = 50,
                include_steam_ids: Annotated[bool, Field(strict=True, description="Include host_steam_id and each "
                                                                      "classification row's steam_id (SteamID64 as a "
                                                                      "string). Off by default: personal "
                                                                      "identifiers.")] = False,
        ) -> dict:
            """One uploaded race weekend: per session the classification, plus a summary of the decision log.

            Document format 2 only (schema/race-results in the game's GameAPI folder); another format is refused
            with unsupported_format, an unreadable stored file with stored_file_unreadable. Top level: format,
            game_version, unity_version, schema_version, schema_hash, timepoint (Unix ms), editor, demo,
            public_lobby (host_steam_id only with include_steam_ids). Per session: kind (qualifying / race), track,
            started_at / ended_at (Unix ms), race_id, header (the decision log's session header, or null),
            classification as uploaded (position, class_position, car_id, car_number, car_class, name, driver
            human/ai, local_seat, grid, laps, gap_to_leader_s, best_lap_s; steam_id only with include_steam_ids),
            decisions_count and decisions_by_type ("<decision_type>/<chosen_by>"); with include_decisions the
            entries without their state and offered vectors. Numbers are not rounded (only NaN/inf become null).
            A pick made by hand is not logged by the game; whether API picks count as by hand is not documented.
            """
            try:
                out = await asyncio.to_thread(store.load_summary, result_id, include_decisions, max_decisions,
                                              include_steam_ids)
            except ResultsError as e:
                return results_failure(e)
            return clean(out, None)

    # ================================================================== resources and prompt

    def _reference_resource(topic: str) -> str:
        """Game API reference table for one topic (see get_reference)."""
        try:
            return json.dumps(R.reference(topic), separators=(",", ":"))
        except KeyError:
            return json.dumps({"error": "unknown topic %r" % topic, "topics": ["all"] + list(R.TOPICS)})

    mcp.resource("bc://reference/{topic}", name="reference", mime_type="application/json",
                 description="Game API reference tables: all, " + ", ".join(R.TOPICS))(_reference_resource)

    def _latest_trigger() -> str:
        """The most recent trigger (null when none)."""
        latest = engine.triggers[-1] if engine.triggers else None
        return json.dumps({"trigger": None if latest is None else clean(engine.public(latest)),
                           "last_seq": engine.seq}, separators=(",", ":"))

    mcp.resource(TRIGGERS_URI, name="latest_trigger", mime_type="application/json",
                 description="The most recent trigger (card_offer, car_damaged, tires_wont_last, tire_screen, "
                             "session_ended). Read it on demand: the server sends no resource-updated "
                             "notifications (wait_for_trigger is the way to be woken).")(_latest_trigger)

    def race_engineer(goal: Optional[str] = None) -> str:
        """Run a race as the race engineer: wait for triggers and answer them."""
        return race_engineer_prompt(goal)

    mcp.prompt(name="race_engineer", description=inspect.cleandoc("Act as race engineer for player one's car in Backseat Champions: "
                                                 "loop on wait_for_trigger and answer each decision."))(race_engineer)
    return mcp


# ------------------------------------------------------------------------------------------------ main

_FLAGS = [
    ("--telemetry-host", "BC_TELEMETRY_HOST", "address to receive telemetry on (default 127.0.0.1)"),
    ("--telemetry-port", "BC_TELEMETRY_PORT", "telemetry UDP port (default 29770)"),
    ("--command-host", "BC_COMMAND_HOST", "the game's command address (default 127.0.0.1)"),
    ("--command-port", "BC_COMMAND_PORT", "the game's command UDP port (default 29771)"),
    ("--game-host", "BC_GAME_HOST", "only accept telemetry from this host when listening off loopback "
                                    "(default: the command host)"),
    ("--command-token", "BC_COMMAND_TOKEN", "Command Token for a game on another machine (12..47 UTF-8 bytes); "
                                            "prefer the environment variable, flags are visible to other users"),
    ("--command-timeout", "BC_COMMAND_TIMEOUT", "seconds to wait for a command reply (default 1.0)"),
    ("--results-port", "BC_RESULTS_PORT", "enable the race results receiver on this TCP port"),
    ("--results-host", "BC_RESULTS_HOST", "results receiver address (default 127.0.0.1)"),
    ("--results-token", "BC_RESULTS_TOKEN", "bearer token the game must send with results"),
    ("--results-dir", "BC_RESULTS_DIR", "folder for received results"),
    ("--results-keep", "BC_RESULTS_KEEP", "uploads to keep, oldest deleted first (default 100; 0 keeps all)"),
    ("--triggers", "BC_TRIGGERS", "enabled triggers, comma list (default card_offer,car_damaged,tires_wont_last,"
                                  "tire_screen; 'none' disables all)"),
    ("--tire-margin", "BC_TIRE_MARGIN", "tires_wont_last: predicted health threshold (default 0.05)"),
    ("--tire-floor", "BC_TIRE_FLOOR", "tires_wont_last: absolute health floor (default 0.10)"),
    ("--perf-drop", "BC_TRIGGER_PERF_DROP", "car_damaged heuristic: relative performance drop (default 0.15; 0 off)"),
    ("--wait-max-s", "BC_WAIT_MAX_S", "cap for wait_for_trigger timeout_s (default 55; never above 60)"),
]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="backseat-champions-mcp",
        description="Unofficial MCP server for the Backseat Champions Game API. Every option can also be set with "
                    "the environment variable shown.")
    for flag, env, text in _FLAGS:
        parser.add_argument(flag, dest=env, metavar=env, help=text)
    parser.add_argument("--allow-drive", action="store_true",
                        help="register the drive tools (BC_ALLOW_DRIVE=1). Driving withholds achievements, Steam "
                             "stats and leaderboard submissions for the whole race weekend.")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="streamable-http bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="streamable-http port (default 8000)")
    parser.add_argument("--log-level", default=(os.environ.get("BC_LOG_LEVEL") or "WARNING").strip(),
                        type=lambda v: v.strip().upper(), choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                        help="stderr log level, any case (BC_LOG_LEVEL; default WARNING)")
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def config_from_args(args, environ=None):
    env = dict(os.environ if environ is None else environ)
    for _, name, _ in _FLAGS:
        value = getattr(args, name, None)
        if value is not None:
            env[name] = value
    if getattr(args, "allow_drive", False):
        env["BC_ALLOW_DRIVE"] = "1"
    global PROGRESS_INTERVAL_S
    PROGRESS_INTERVAL_S = progress_interval(env)
    return Config.from_env(env).validate()


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    warnings.filterwarnings("ignore", message=".*deprecated as of 2026-07-28.*")
    try:
        config = config_from_args(args)
    except ValueError as e:
        print("backseat-champions-mcp: %s" % e, file=sys.stderr)
        return 2
    server = build_server(config, log_level=args.log_level)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
