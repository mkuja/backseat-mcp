"""MCP server for the Backseat Champions Game API (unofficial).

The model acts as race engineer. The server is trigger-driven: ``wait_for_trigger`` blocks until one of three
conditions needs a decision (a new card hand is offered, the car is damaged, the tires will not last another lap);
everything else is available through on-demand query tools. The server never sends a command on its own.

Run ``backseat-champions-mcp --help`` for options. Logs go to stderr; stdout carries only MCP messages.
"""
import argparse
import asyncio
import contextlib
import functools
import json
import logging
import math
import os
import sys
import time
import warnings
from typing import Annotated, Any, List, Optional, Union

from pydantic import Field

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from . import __version__
from . import protocol as P
from . import reference as R
from .client import MAX_BOOST_S, MAX_DRIVE_S, Config, GameClient, failure
from .results import ResultsReceiver

log = logging.getLogger("backseat_champions_mcp")

TRIGGERS_URI = "bc://triggers/latest"
WAIT_EVENT_MAX_S = 60.0
PROGRESS_INTERVAL_S = float(os.environ.get("BC_PROGRESS_INTERVAL_S") or 10.0)

INSTRUCTIONS = """\
Unofficial MCP server for the Backseat Champions Game API. You are the race engineer for player one's car (seat 0).
The car is driven by the game's AI; you manage it: pick and ban upgrade cards, call pit stops, choose tire
compounds, hold boost buttons. The server never acts on its own.

Workflow: call get_api_status once, then loop on wait_for_trigger. It returns when a decision is needed:
card_offer (pick_card / ban_card), car_damaged or tires_wont_last (decide set_pit(true); then pick_tire when the tire
screen opens). Query anything else on demand (get_race_snapshot, get_standings, get_player_state, ...).
Telemetry only exists during a session and only for the switches enabled in the game under Settings > Telemetry.
"""


# ------------------------------------------------------------------------------------------------ formatting

def clean(value, digits=3):
    """Round floats (NaN/inf -> None) recursively, for compact JSON."""
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round(value, digits)
    if isinstance(value, dict):
        return {k: clean(v, digits) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, digits) for v in value]
    return value


def _finite(value):
    """Non-finite floats (a hostile or broken datagram's NaN/inf) -> None, so tool output is always valid JSON."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def _result(out):
    is_error = isinstance(out, dict) and out.get("ok") is False
    text = json.dumps(_finite(out), separators=(",", ":"), ensure_ascii=False, default=str)
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)


READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WAIT = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=False)


def _write(destructive, idempotent, title=None):
    return ToolAnnotations(title=title, read_only_hint=False, destructive_hint=destructive,
                           idempotent_hint=idempotent, open_world_hint=False)


# ------------------------------------------------------------------------------------------------ shared state

class AppState:
    """The game client (one per process) shared by every MCP session, started on first use."""

    def __init__(self, config):
        self.config = config
        self.client = GameClient(config)
        self.results = None
        if config.results_port is not None:
            self.results = ResultsReceiver(config.results_host, config.results_port, config.results_token,
                                           config.results_dir)
        self.users = 0
        self.lock = asyncio.Lock()
        self.last_ctx = None
        self.client.triggers.listeners.append(self._on_trigger)

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

    def remember(self, ctx):
        if ctx is not None:
            self.last_ctx = ctx

    def _on_trigger(self, trig):
        """Best effort: tell the client that bc://triggers/latest changed, and log the trigger."""
        ctx = self.last_ctx
        if ctx is None:
            return
        try:
            asyncio.get_running_loop().create_task(self._notify(ctx, trig))
        except RuntimeError:
            pass

    async def _notify(self, ctx, trig):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for send in (lambda: ctx.session.send_resource_updated(TRIGGERS_URI),
                         lambda: ctx.notify_resource_updated(TRIGGERS_URI),
                         lambda: ctx.session.send_log_message(
                             level="notice", data={"trigger": trig["type"], "seq": trig["seq"]},
                             logger="backseat_champions_mcp.triggers")):
                try:
                    await asyncio.wait_for(send(), 2.0)
                except Exception:  # the session may be gone, or the client may not take it
                    pass


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
                for value in list(args) + list(kwargs.values()):
                    if isinstance(value, Context):
                        state.remember(value)
                try:
                    out = await fn(*args, **kwargs)
                except ValueError as e:
                    out = failure("bad_argument", detail="(%s)" % e)
                return _result(out)
            mcp.add_tool(wrapper, name=name or fn.__name__, annotations=annotations, structured_output=False)
            return fn
        return decorator

    # ------------------------------------------------------------------ helpers

    def names():
        got = client.latest(P.PKT_PARTICIPANTS)
        if got is None:
            return {}
        return {i: c for i, c in enumerate(got[1]["cars"])}

    def label(car, info=None):
        info = names() if info is None else info
        out = {"car": car}
        p = info.get(car)
        if p:
            out.update(name=p["name"], car_type=p["car_type"], kind=p["kind"])
        if car == client.my_car():
            out["is_me"] = True
        return out

    def stale_note(pid, age):
        if age > client.stale_after(pid):
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
               "session_time_s": round(header["session_time"], 2)}
        note = stale_note(pid, age)
        if note:
            out["stale"] = True
            out["stale_note"] = note
        return out

    def select_cars(car, count):
        """List of car indices for a car selector: "me", "all", or an index."""
        if car is None:
            car = "me"
        if isinstance(car, str):
            key = car.strip().lower()
            if key in ("me", "mine", "self", "player", ""):
                me = client.my_car()
                if me is None:
                    raise ValueError("seat 0 has no car in this session (seat_car[0] is 255)")
                if me >= count:
                    raise ValueError("seat 0's car index %d is not among the %d cars of this packet" % (me, count))
                return [me]
            if key == "all":
                return list(range(count))
            if not key.isdigit():
                raise ValueError("car must be \"me\", \"all\" or a car index 0..%d, got %r" % (max(count - 1, 0), car))
            car = int(key)
        if isinstance(car, bool) or not (0 <= car < max(count, 1)) or car >= P.MAX_CARS:
            raise ValueError("car index %r is out of range: this session has %d cars (0..%d)"
                             % (car, count, count - 1))
        return [car]

    def energy_kind():
        got = client.latest(P.PKT_SESSION)
        return None if got is None else got[1]["energy"]

    CarParam = Annotated[Union[int, str], Field(
        description='Which car: "me" (player one\'s car, the default), "all" (every car in the session), or a car '
                    'index 0..15 (the car\'s position in the participant list, fixed for the session; see '
                    'get_participants).')]

    # ================================================================== status

    @tool(READ)
    async def get_api_status(ctx: Context) -> dict:
        """Check the connection to the game and what is switched on. Call this first.

        Sends the Game API `hello` command (and a `ping` for latency) to the game's command port and combines the
        Hello reply with this server's own state:
        - game: reachable, game_version, rate_hz, feature schema version/hash, `inputs` (player_input,
          driver_input, public_lobby) and `broadcast` switches (driver_data, player_data, decision_events,
          ai_observations).
        - switches_off: each broadcast/input switch that is off, with the tools that cannot work without it.
          Settings live in the game under Settings > Telemetry (the API is off by default).
        - telemetry listener: bound address, whether packets are arriving, last packet age, counts per packet
          type, session_uid, my car index (seat 0).
        - holds (running boost/drive repeats and any error that ended one), trigger summary, results receiver.
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
                    off.append({"switch": info["label"], "kind": "broadcast", "affects": info["tools"]})
            for key, info in R.INPUT_SWITCHES.items():
                if not h[key]:
                    off.append({"switch": info["label"], "kind": "input", "affects": info["tools"]})
            game["switches_off"] = off
            if off:
                warnings_.append("Switch these on under %s to use the affected tools: %s."
                                 % (R.SETTINGS_PATH, ", ".join(o["switch"] for o in off)))
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
        listener["num_cars"] = client.num_cars
        out["telemetry"] = listener
        if not listener["bound"]:
            out["telemetry"]["problem"] = client.listener_failure()
        elif not listener["receiving"]:
            warnings_.append("No telemetry is arriving. The game sends telemetry only during a session (practice, "
                             "qualifying or race).")
        out["holds"] = client.hold_status()
        trig = client.triggers
        out["triggers"] = {"enabled": sorted(trig.config.enabled), "queued": len(trig.triggers),
                           "last_seq": trig.seq, "undelivered": len(trig.pending()[0])}
        if state.results is not None:
            out["results_receiver"] = state.results.status()
        out["config"] = config.public()
        if warnings_:
            out["warnings"] = warnings_
        return out

    @tool(READ)
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
        out.update(num_cars=header["num_cars"], my_car=header["seat_car"][0], game_version=header["game_version"],
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
        car (index 0..15, fixed for the session; the index every other tool uses), name (display name; other
        online players' names included), car_type (Formula, ClassicFormula, ModernFormula, Endurance, GT2, Stock,
        FormulaOldskool, FormulaLegacy), kind (ai, local = a player on this machine, remote = a player on another
        machine), player_id (not the car index), is_me for seat 0's car.
        """
        got, err = await need(P.PKT_PARTICIPANTS, what="Participants")
        if err:
            return err
        header, body, age = got
        out = base(P.PKT_PARTICIPANTS, header, age)
        me = client.my_car()
        out["cars"] = [dict({"car": i}, **c, **({"is_me": True} if i == me else {}))
                       for i, c in enumerate(body["cars"])]
        return out

    @tool(READ)
    async def get_standings(ctx: Context) -> dict:
        """The running order: every car sorted by position, with names, laps, gaps and pit state.

        Source: Lap Data packet (id 2; at the configured rate; needs Driver Data), names from Participants.
        Per row: position (overall, from 1), class_position, car, name, car_type, kind, laps (laps completed),
        gap_to_leader_s, interval_ahead_s, interval_behind_s (seconds; the game does not document the sign of
        interval_behind and real captures show it negative, so read it as a magnitude), pit_stops, in_pits
        (in the pit lane), finished, pit_planned (only ever true for this machine's cars: a rival's call is never
        shown), api_driving (an API client is driving that car), is_me.
        """
        got, err = await need(P.PKT_LAP_DATA, what="Lap Data")
        if err:
            return err
        header, body, age = got
        info = names()
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
        if not info:
            out["note"] = "No Participants packet yet (sent every 5 s): names are missing."
        return clean(out, 3)

    @tool(READ)
    async def get_car_telemetry(
            ctx: Context, car: CarParam = "me",
            include_wheels: Annotated[bool, Field(description="Include the four wheels (front two, then rear two): "
                                                             "on_track, grounded, slip_forward, slip_sideways.")] = False
    ) -> dict:
        """Speed, applied pedal and steer, tires and energy of one car or all cars.

        Source: Car Telemetry packet (id 1; at the configured rate; needs Driver Data). Per car: speed_kmh,
        pedal (-1 full brake to 1 full throttle, as applied), steer (-1 left to 1 right, as applied),
        tire_compound (Standard, Boost, Experimental, Fast, Durable, Wet, Golden), tire_health (1 new, 0 worn
        out), puncture, energy (boost or fuel left, 0 to 1; energy_kind says which), wheels_on_track and
        wheels_grounded (count of 4), and with include_wheels the per-wheel data (slip is unitless and 0 while
        off the ground).
        """
        got, err = await need(P.PKT_CAR_TELEMETRY, what="Car Telemetry")
        if err:
            return err
        header, body, age = got
        info = names()
        cars = []
        for i in select_cars(car, len(body["cars"])):
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
        info = names()
        cars = []
        for i in select_cars(car, len(body["cars"])):
            c = body["cars"][i]
            row = label(i, info)
            row.update(position_m=clean(c["position"], 2), velocity_m_s=clean(c["velocity"], 2),
                       speed_m_s=round(math.sqrt(sum(v * v for v in c["velocity"])), 2),
                       angular_velocity_rad_s=clean(c["angular_velocity"], 3), pitch_deg=round(c["pitch"], 2),
                       roll_deg=round(c["roll"], 2), yaw_deg=round(c["yaw"], 2))
            cars.append(row)
        out = base(P.PKT_MOTION, header, age)
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
        info = names()
        cars = []
        for i in select_cars(car, len(body["cars"])):
            row = label(i, info)
            row["performance"] = body["cars"][i]["performance"]
            cars.append(row)
        out = base(P.PKT_CAR_STATUS, header, age)
        out["cars"] = cars
        if body["cars"]:
            out["field_median"] = {a: sorted(c["performance"][a] for c in body["cars"])[len(body["cars"]) // 2]
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
        info = names()
        cars = []
        for i in select_cars(car, len(body["cars"])):
            c = body["cars"][i]
            row = label(i, info)
            row["active"] = [k for k in ("speed_boost", "cornering_boost", "super_boost", "party_mode", "drs",
                                         "boost_overspeed", "recharging") if c[k]]
            row.update(slipstream=c["slipstream"], recharge_left=c["recharge_left"])
            if i == client.my_car() or (info.get(i) or {}).get("kind") == "local":
                row["unlocked"] = [k for k, f in (("super_boost", "super_boost_unlocked"),
                                                  ("party_mode", "party_mode_unlocked"),
                                                  ("drs", "drs_unlocked")) if c[f]]
                row.update(api_boosting=c["api_boosting"], ban_tokens=c["ban_tokens"])
            cars.append(row)
        out = base(P.PKT_CAR_ABILITIES, header, age)
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
            include_strategy_blocks: Annotated[bool, Field(
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
        (seconds), reference_lap_s (seconds).
        """
        got, err = await need(P.PKT_PLAYER, 0, what="Player")
        if err:
            return err
        header, body, age = got
        out = base(P.PKT_PLAYER, header, age)
        out.update(player_view(body, include_strategy_blocks))
        return clean(out)

    @tool(READ)
    async def get_pending_decisions(ctx: Context) -> dict:
        """What currently needs an answer: unspent points and the hand, the pit call, the tire screen.

        Combines Player (id 4; Player Data), Car Abilities (ban tokens) and the latest seat-0 events
        (CARD, PITW, TIRE; Decision Events):
        - cards: open (points > 0 and a hand), points, hand, ban_tokens, source. Answer with pick_card(index) or
          ban_card(index).
        - pit: pit_planned, can_call_now, in_pits, and the latest PITW event (sent each new lap). Answer with
          set_pit(true/false).
        - tire_screen: the latest TIRE event if it has not been answered or closed (pre_race, current, allowed
          compounds, picks_left). The open/closed state is only known from events: the screen is presumed closed
          after a successful pick_tire, a tire_select_closed error, leaving the pit lane, or session end. Answer
          with pick_tire(compound).
        """
        out = {"ok": True}
        got = client.latest(P.PKT_PLAYER, 0)
        ab = client.latest(P.PKT_CAR_ABILITIES)
        me = client.my_car()
        tokens = None
        if ab is not None and me is not None and me < len(ab[1]["cars"]):
            tokens = ab[1]["cars"][me]["ban_tokens"]
        if got is not None:
            header, body, age = got
            out["age_s"] = round(age, 2)
            out["cards"] = {"open": body["points"] > 0 and bool(body["hand"]), "points": body["points"],
                            "hand": body["hand"], "ban_tokens": tokens, "source": "Player packet"}
            out["pit"] = {"pit_planned": body["pit_planned"], "can_call_now": body["can_pit"],
                          "in_pits": body["in_pits"], "laps_remaining": body["laps_remaining"]}
        elif client.card_event is not None:
            e = client.card_event
            out["cards"] = {"open": True, "points": e["points"], "hand": e["hand"], "ban_tokens": tokens,
                            "source": "CARD event (Player Data is off)"}
        else:
            out["cards"] = None
        if client.pit_event is not None:
            out.setdefault("pit", {})["last_pit_window_event"] = client.public_event(client.pit_event)
        out["tire_screen"] = (client.public_event(client.tire_event) if client.tire_event is not None
                              else {"open": False})
        if client.tire_event is not None:
            out["tire_screen"]["open"] = True
        if got is None and client.card_event is None and client.pit_event is None:
            fail = await client.no_data_failure(P.PKT_PLAYER, "Player")
            out["note"] = fail.get("explanation")
            out["hint"] = fail.get("hint")
        out["undelivered_triggers"] = len(client.triggers.pending()[0])
        return clean(out)

    @tool(READ)
    async def get_racing_line(
            ctx: Context,
            max_points: Annotated[int, Field(ge=1, le=20, description="How many of the next checkpoints to return "
                                                                      "(1..20).")] = 20
    ) -> dict:
        """The track ahead of your car: the next checkpoints with the racing line point and both track edges.

        Source: Racing Line packet (id 13) for seat 0 (at the configured rate; needs Driver Data). Points run in
        driving order from the checkpoint the car is heading for; during a pit stop they follow the pit lane.
        Per point: racing_line, left, right ([x, y, z] world metres, as in Motion) and width_m (distance between
        the edges). car_position_m is the car's current Motion position when available.
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
        if motion is not None and body["car"] < len(motion[1]["cars"]):
            out["car_position_m"] = motion[1]["cars"][body["car"]]["position"]
        return clean(out, 2)

    @tool(READ)
    async def get_ai_observations(
            ctx: Context, car: CarParam = "me",
            include_obs: Annotated[bool, Field(description="Include the driving net's input vector (up to 339 "
                                                          "floats per car; layout belongs to the net, identified "
                                                          "by `policy`). Large; off by default.")] = False
    ) -> dict:
        """The AI driver's network for one car or all cars: policy id, last action and (optionally) its inputs.

        Source: AI Observations packet (id 8; one per car at the configured rate; needs the AI Observations
        switch, off by default, about 1.1 MB/s at 50 Hz; only sent by the machine that simulates the cars:
        single player, splitscreen or the online host). Per car: policy (first 12 hex digits of the net's
        SHA-256), policy_name, obs_frame (advances each time the net decides), action {pedal, steer} the net last
        chose, obs_count, api_driving, and obs when include_obs is true.
        """
        if isinstance(car, str) and car.strip().lower() == "all":
            keys = client.keys(P.PKT_AI_OBSERVATIONS)
        else:
            keys = select_cars(car, max(client.num_cars, 1))
        cars = []
        info = names()
        newest = None
        for key in keys:
            got = client.latest(P.PKT_AI_OBSERVATIONS, key)
            if got is None:
                continue
            header, body, age = got
            newest = got if newest is None or age < newest[2] else newest
            row = label(body["car"], info)
            row.update(age_s=round(age, 2), policy=body["policy"], policy_name=body["policy_name"],
                       obs_frame=body["obs_frame"], action={"pedal": body["action"][0], "steer": body["action"][1]},
                       obs_count=len(body["obs"]), api_driving=body["api_driving"])
            if include_obs:
                row["obs"] = clean(body["obs"], 4)
            cars.append(row)
        if not cars:
            return await client.no_data_failure(P.PKT_AI_OBSERVATIONS, "AI Observations")
        out = base(P.PKT_AI_OBSERVATIONS, newest[0], newest[2])
        out["cars"] = cars
        return clean(out)

    # ================================================================== events

    EventCodes = Annotated[Optional[List[str]], Field(
        description="Only these event codes (any of SSTA, SEND, CARD, TIRE, LAPC, PITW, PITI, PITO, FINI); "
                    "omit for all.")]

    @tool(READ)
    async def get_events(
            ctx: Context,
            since_seq: Annotated[int, Field(ge=0, description="Return events with seq greater than this "
                                                              "(0 = everything buffered).")] = 0,
            codes: EventCodes = None,
            limit: Annotated[int, Field(ge=1, le=500, description="At most this many (the newest).")] = 50,
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
        found = client.events_after(since_seq, codes, limit)
        out = {"ok": True, "events": [client.public_event(e) for e in found], "last_seq": client.event_seq,
               "buffer_starts_at_seq": client.oldest_seq()}
        if not client.events and client.event_seq == 0:
            if client._tele_transport is None:
                return client.listener_failure()
            out["note"] = ("No events received yet. Events need Decision Events and a running session; "
                           "get_api_status shows the switches.")
        return out

    @tool(WAIT)
    async def wait_for_event(
            ctx: Context,
            codes: EventCodes = None,
            timeout_s: Annotated[float, Field(gt=0, le=WAIT_EVENT_MAX_S, description="How long to wait, seconds "
                                                                                     "(max 60).")] = 30.0,
            since_seq: Annotated[Optional[int], Field(ge=0, description="Return events after this seq (an event "
                                                                        "that already arrived counts). Omit to wait "
                                                                        "for events newer than now.")] = None,
    ) -> dict:
        """Block until a matching game event arrives (or timeout_s passes).

        Same events as get_events (needs Decision Events). Typical use: wait_for_event(codes=["TIRE"]) after
        set_pit(true), to know when the tire screen opens and pick_tire becomes possible. Returns
        {ok, matched: true, events, last_seq} or {ok, matched: false, last_seq} on timeout. Sends MCP progress
        notifications while waiting. Prefer wait_for_trigger for the main loop.
        """
        if client._tele_transport is None:
            return client.listener_failure()

        async def tick(elapsed, total):
            with contextlib.suppress(Exception):
                await ctx.report_progress(elapsed, total, "waiting for event")

        found = await client.wait_for_event(codes, timeout_s, since_seq, on_tick=tick,
                                            tick=min(PROGRESS_INTERVAL_S, timeout_s))
        out = {"ok": True, "matched": bool(found), "events": [client.public_event(e) for e in found],
               "last_seq": client.event_seq}
        if not found:
            out["note"] = "No matching event within %.0f s." % timeout_s
            if not client.in_session():
                out["note"] += " No telemetry is arriving (no session, or the game / API is off)."
        return out

    # ================================================================== snapshot

    def snapshot_context():
        """Compact race context for trigger results and the snapshot."""
        ctx_out = {}
        me = client.my_car()
        ctx_out["my_car"] = me
        sess = client.latest(P.PKT_SESSION)
        if sess is not None:
            s = sess[1]
            ctx_out["session"] = {"type": s["session_type"], "scene": s["scene"], "total_laps": s["total_laps"],
                                  "weather": s["weather"], "energy_kind": s["energy"],
                                  "public_lobby": s["public_lobby"]}
        lap = client.latest(P.PKT_LAP_DATA)
        if lap is not None and me is not None and me < len(lap[1]["cars"]):
            c = lap[1]["cars"][me]
            ctx_out.update(position=c["position"], laps_completed=c["lap"], gap_to_leader_s=c["gap_to_leader"],
                           interval_ahead_s=c["interval_ahead"], interval_behind_s=c["interval_behind"],
                           in_pits=c["in_pits"], finished=c["finished"], pit_stops=c["pit_stops"])
            order = sorted(range(len(lap[1]["cars"])), key=lambda i: lap[1]["cars"][i]["position"] or 99)
            info = names()
            k = order.index(me)
            if k > 0:
                ctx_out["car_ahead"] = label(order[k - 1], info)
            if k + 1 < len(order):
                ctx_out["car_behind"] = label(order[k + 1], info)
            ctx_out["cars_in_race"] = len(order)
        pl = client.latest(P.PKT_PLAYER, 0)
        if pl is not None:
            b = pl[1]
            ctx_out.update(laps_remaining=b["laps_remaining"], points=b["points"],
                           tire={"compound": b["tire_compound"], "health": b["tire_health"],
                                 "puncture": b["puncture"]},
                           energy=b["energy"], pit_planned=b["pit_planned"], can_pit=b["can_pit"])
        else:
            tel = client.latest(P.PKT_CAR_TELEMETRY)
            if tel is not None and me is not None and me < len(tel[1]["cars"]):
                c = tel[1]["cars"][me]
                ctx_out.update(tire={"compound": c["tire_compound"], "health": c["tire_health"],
                                     "puncture": c["puncture"]}, energy=c["energy"])
        age = client.telemetry_age()
        ctx_out["telemetry_age_s"] = None if age is None else round(age, 2)
        return clean(ctx_out)

    @tool(READ)
    async def get_race_snapshot(ctx: Context) -> dict:
        """One compact overview of the race for your car: session, position, gaps, tires, energy, decisions.

        Combines Session, Lap Data, Participants, Player and Car Telemetry (whichever are switched on): session
        type/scene/total_laps/weather/energy_kind, position, laps_completed, laps_remaining, gaps (seconds; see
        get_standings about interval_behind's sign), car_ahead / car_behind (car index, name), tire (compound,
        health 1..0, puncture), energy (0..1), points, pit_planned, can_pit, in_pits; plus pending decisions
        (cards on offer, open tire screen), the tire-wear forecast of the trigger engine, undelivered triggers
        and running boost/drive holds.
        """
        if client._tele_transport is None:
            return client.listener_failure()
        if client.latest(P.PKT_SESSION) is None and client.latest(P.PKT_LAP_DATA) is None \
                and client.latest(P.PKT_PLAYER, 0) is None:
            return await client.no_data_failure(P.PKT_SESSION, "race")
        out = {"ok": True}
        out.update(snapshot_context())
        pl = client.latest(P.PKT_PLAYER, 0)
        if pl is not None and pl[1]["points"] > 0 and pl[1]["hand"]:
            out["cards_on_offer"] = pl[1]["hand"]
        if client.tire_event is not None:
            out["tire_screen_open"] = client.public_event(client.tire_event)
        out["tire_forecast"] = clean(client.triggers.tire_prediction())
        out["undelivered_triggers"] = len(client.triggers.pending()[0])
        holds = client.hold_status()
        if holds:
            out["holds"] = holds
        return out

    # ================================================================== reference

    @tool(READ)
    async def get_reference(
            ctx: Context,
            topic: Annotated[str, Field(description="One of: all, " + ", ".join(R.TOPICS)
                                                    + ". Aliases: errors, events, cameras, tires, cards, "
                                                      "switches.")] = "all"
    ) -> dict:
        """Static reference tables from the Game API documentation (no game needed).

        Topics: compounds, upgrades, facilities, session_types, car_types, performance_areas, camera_types,
        participant_kinds, error_reasons (every refusal reason with explanation and what to do), event_codes,
        packets (id, size, rate, needed switch), commands (id, size, payload, needed switch, reply), settings
        (in-game switches and which tools need them), rules (seat rule, public lobby, holds, drive
        consequences), triggers (what fires each trigger, the switches it needs, which are heuristic). Also
        available as resources bc://reference/{topic}.
        """
        try:
            return {"ok": True, "topic": topic, "data": R.reference(topic)}
        except KeyError:
            return failure("bad_argument", detail="(unknown topic %r; one of: all, %s)" % (topic, ", ".join(R.TOPICS)))

    # ================================================================== triggers

    @tool(WAIT)
    async def wait_for_trigger(
            ctx: Context,
            timeout_s: Annotated[Optional[float], Field(
                gt=0, description="Longest wait in seconds; default and maximum are BC_WAIT_MAX_S (300 unless "
                                  "configured). Your MCP client's own tool timeout may be lower (Claude Code: "
                                  "MCP_TOOL_TIMEOUT); keep timeout_s below it.")] = None,
            since_seq: Annotated[Optional[int], Field(
                ge=0, description="Return triggers with seq greater than this. Omit to get every trigger not yet "
                                  "returned by wait_for_trigger (the normal loop).")] = None,
    ) -> dict:
        """Block until the race needs a decision from you; this is the main loop of the race engineer.

        Returns immediately if a trigger is already queued and not yet delivered. Enabled triggers (BC_TRIGGERS;
        default card_offer, car_damaged, tires_wont_last; optional tire_screen):
        - card_offer: a new hand of upgrade cards (points, hand with index/upgrade/facility/tier/banned,
          ban_tokens). From the CARD event, or from the Player packet when Decision Events is off. Answer with
          pick_card(index) / ban_card(index).
        - car_damaged: signal "puncture" (definite: the puncture flag went 0 -> 1) or "performance_drop"
          (heuristic: one performance area of your car fell suddenly while the field's did not and no ability
          was active; heuristic=true). Consider set_pit(true).
        - tires_wont_last (heuristic forecast): tire health is predicted to fall to BC_TIRE_MARGIN (0.05) or below
          by the end of the NEXT lap, or is already at/below BC_TIRE_FLOOR (0.10). Payload: tire_health,
          wear_per_lap and how it was derived, predicted_health_end_of_next_lap, laps_remaining, can_pit,
          pit_planned, compound, compound_picks_left, pit_lane_time_loss_s. Silent on the last lap, in the pit
          lane, after the flag or when a stop is already called. Decide set_pit(true), then pick_tire when the
          tire screen opens.
        - tire_screen: the tire selection screen opened (pre_race, current, allowed, picks_left): pick_tire.
        Result when triggered: {triggered: true, triggers: [{seq, type, age_s, superseded, lap, ...payload}],
        context: {position, laps_completed, laps_remaining, gaps, car_ahead/behind, tire, energy, points, ...}}.
        Older triggers can be superseded (a newer hand, or the car pitted since): act on the newest.
        On SEND/FINI (session over / you took the flag) it returns {triggered: false, status: "session_ended"}.
        On timeout {triggered: false, status: "timeout", ...}: just call it again. Sends MCP progress
        notifications while waiting. The server never acts by itself: nothing happens unless you call a command.
        """
        if client._tele_transport is None:
            return client.listener_failure()
        cap = config.wait_max_s
        timeout = cap if timeout_s is None else min(timeout_s, cap)
        engine = client.triggers
        start = time.monotonic()
        deadline = start + timeout
        last_progress = start
        while True:
            trig, end = engine.pending(since_seq)
            if trig or end:
                engine.mark_delivered(trig, end)
                now = time.monotonic()
                out = {"ok": True, "triggered": bool(trig)}
                if trig:
                    out["triggers"] = [clean(engine.public(t, now)) for t in trig]
                if end:
                    out["status"] = "session_ended"
                    out["session_end"] = engine.end_info
                    out["next"] = ("The session is over for you. Call wait_for_trigger again to wait for the next "
                                   "session's triggers.")
                out["context"] = snapshot_context()
                out["last_seq"] = engine.seq
                return out
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            fut = engine.waiter()
            try:
                await asyncio.wait_for(fut, min(remaining, PROGRESS_INTERVAL_S))
            except asyncio.TimeoutError:
                pass
            finally:
                engine.discard_waiter(fut)
            now = time.monotonic()
            if now - last_progress >= PROGRESS_INTERVAL_S * 0.99 and now < deadline:
                last_progress = now
                with contextlib.suppress(Exception):
                    await ctx.report_progress(round(now - start, 1), round(timeout, 1), "waiting for a trigger")
        out = {"ok": True, "triggered": False, "status": "timeout", "waited_s": round(time.monotonic() - start, 1),
               "last_seq": engine.seq, "context": snapshot_context(),
               "next": "Nothing needed a decision. Call wait_for_trigger again."}
        if not client.in_session():
            out["telemetry"] = ("No telemetry is arriving: the game sends none outside a session, or the API / "
                                "its switches are off (see get_api_status).")
        return out

    @tool(READ)
    async def get_triggers(
            ctx: Context,
            since_seq: Annotated[int, Field(ge=0, description="Triggers with seq greater than this.")] = 0,
            limit: Annotated[int, Field(ge=1, le=200, description="At most this many (the newest).")] = 20,
    ) -> dict:
        """Recent triggers of the current session, without waiting and without marking them delivered.

        Includes delivered and undelivered ones. Each: seq, type (card_offer, car_damaged, tires_wont_last, tire_screen), age_s, superseded, lap,
        session_time_s and the payload described in wait_for_trigger. The queue is cleared when a new session
        starts (sequence numbers keep increasing). Also: last_seq, delivered_up_to_seq.
        """
        engine = client.triggers
        now = time.monotonic()
        return {"ok": True, "triggers": [clean(engine.public(t, now)) for t in engine.after(since_seq, limit)],
                "last_seq": engine.seq, "delivered_up_to_seq": engine.delivered_seq}

    @tool(READ)
    async def get_trigger_config(ctx: Context) -> dict:
        """The trigger engine's configuration and live state, to judge how much to trust a trigger.

        Returns enabled triggers, thresholds (tire_margin, tire_floor, perf_drop, perf_window_s; all tunable
        heuristics of this server, not game values) and state: which source card offers come from (CARD event or
        Player fallback), the last card offer, puncture flag and arming, alarmed performance areas, and the tire
        forecast (current health, per-lap wear samples on this tire set, wear_per_lap and how it was derived, lap
        time and its source, predicted health at the end of the next lap, armed / silenced reason).
        """
        out = {"ok": True}
        out.update(config.triggers.public())
        out["state"] = clean(client.triggers.state(), 4)
        return out

    # ================================================================== commands

    @tool(_write(True, False, "Pick an upgrade card"))
    async def pick_card(
            ctx: Context,
            index: Annotated[int, Field(ge=0, le=3, description="Card index in the current hand, 0..3 (the `index` "
                                                                "field of the hand in the card_offer trigger, "
                                                                "get_pending_decisions or get_player_state).")],
    ) -> dict:
        """Pick a card from the hand on offer, spending one upgrade point (command pick_card, id 2, seat 0).

        Needs Player Input, points > 0 and a card that is not banned. The game answers Ack or Error; possible
        errors: no_points, invalid_index, cards_unavailable, card_banned, input_disabled, public_lobby, no_player.
        Returns {ok, picked: the card as last seen in the hand (when known)} or {ok: false, error_reason,
        explanation, hint}. After a pick the game may offer a new hand (a new card_offer trigger) while points
        remain.
        """
        card = None
        got = client.latest(P.PKT_PLAYER, 0)
        hand = got[1]["hand"] if got is not None else (client.card_event or {}).get("hand", [])
        for c in hand or []:
            if c["index"] == index:
                card = c
        result = await client.pick_card(index)
        if result["ok"] and card is not None:
            result["picked"] = card
        return result

    @tool(_write(True, False, "Ban an upgrade card"))
    async def ban_card(
            ctx: Context,
            index: Annotated[int, Field(ge=0, le=3, description="Card index in the current hand, 0..3.")],
    ) -> dict:
        """Ban a card in the current hand, spending one ban token (command ban_card, id 7, seat 0).

        This is the card screen's own ban. Unlike a pick it keeps your turn: you still have to pick a card. It
        cannot ban the last card left to pick. Needs Player Input. Errors: ban_refused (no ban tokens left, last
        card left to pick, or a card that can't be banned), invalid_index, input_disabled, public_lobby,
        no_player. Ban tokens left: get_car_abilities / get_pending_decisions (ban_tokens).
        """
        return await client.ban_card(index)

    @tool(_write(False, True, "Call or cancel a pit stop"))
    async def set_pit(
            ctx: Context,
            pit: Annotated[bool, Field(description="true calls a pit stop, false cancels a called one.")],
    ) -> dict:
        """Call (true) or cancel (false) a pit stop for your car (command set_pit, id 3, seat 0).

        Needs Player Input. Check can_call_now (get_player_state) / can_pit (trigger payload or PITW event)
        first: a call the car can't make is refused with `refused`. Other errors: input_disabled, public_lobby,
        no_player. When the car enters the pits the tire screen opens (TIRE event): then use pick_tire. The game
        does not document what it fits if no compound is picked.
        """
        return await client.set_pit(pit)

    @tool(_write(True, False, "Pick a tire compound"))
    async def pick_tire(
            ctx: Context,
            compound: Annotated[Union[str, int], Field(
                description="Compound name (case-insensitive): Standard, Boost, Experimental, Fast, Durable, Wet, "
                            "Golden; or its index 0..6.")],
    ) -> dict:
        """Pick a tire compound while the tire screen is open (command pick_tire, id 4, seat 0).

        The tire screen opens on the starting grid, in the qualifying box and at a pit stop (TIRE event: its
        `allowed` list and `picks_left` say what can be chosen; null = no limit). Needs Player Input and an open
        tire screen. Errors: tire_select_closed (screen not open: wait_for_event(codes=["TIRE"])),
        compound_refused (not allowed now / no picks left), invalid_compound, input_disabled, public_lobby.
        """
        try:
            index = P.compound_index(compound)
        except ValueError as e:
            return failure("bad_argument", "pick_tire", "(%s)" % e)
        result = await client.pick_tire(index)
        result["compound"] = P.COMPOUNDS[index]
        return result

    @tool(_write(False, False, "Hold boost buttons"))
    async def boost(
            ctx: Context,
            speed: Annotated[bool, Field(description="Hold the speed boost button.")] = False,
            cornering: Annotated[bool, Field(description="Hold the cornering boost button. With speed this becomes "
                                                         "a super boost once the car has unlocked it.")] = False,
            party_mode: Annotated[bool, Field(description="Hold party mode (only exists in fuel races, once "
                                                          "unlocked).")] = False,
            duration_s: Annotated[float, Field(gt=0, le=MAX_BOOST_S, description="How long to hold, seconds "
                                                                                 "(max 30).")] = 2.0,
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
            camera: Annotated[Union[str, int], Field(
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
                pedal: Annotated[float, Field(ge=-1.0, le=1.0, description="-1 full brake to 1 full throttle.")],
                steer: Annotated[float, Field(ge=-1.0, le=1.0, description="-1 full left to 1 full right.")],
                duration_s: Annotated[float, Field(gt=0, le=MAX_DRIVE_S, description="How long to hold, seconds "
                                                                                    "(max 10).")] = 1.0,
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

        @tool(READ)
        async def list_race_results(
                ctx: Context,
                limit: Annotated[int, Field(ge=1, le=200, description="At most this many, newest first.")] = 20,
        ) -> dict:
            """Race weekends the game uploaded to this server's results receiver, newest first.

            The game uploads a finished race weekend only when you host and http://127.0.0.1:<BC_RESULTS_PORT>/
            (with BC_RESULTS_TOKEN as its token, if set) is listed under Race Results in Settings > Telemetry.
            Nothing is retried by the game. Per upload: id (use with get_race_result), received (local time),
            bytes, game_version, session_count, and problems if the document looked malformed.
            """
            out = {"ok": True, "receiver": state.results.status(), "results": []}
            for rid in store.ids()[:limit]:
                headers = store.load_headers(rid)        # not the document: it can be up to 256 MB
                entry = {"id": rid, "bytes": store.size(rid),
                         "game_version": headers.get("x-bc-game-version"),
                         "session_count": headers.get("x-bc-session-count")}
                if headers.get("problems"):
                    entry["problems"] = headers["problems"]
                out["results"].append(entry)
            return out

        @tool(READ)
        async def get_race_result(
                ctx: Context,
                result_id: Annotated[str, Field(description='An id from list_race_results, or "latest".')] = "latest",
                include_decisions: Annotated[bool, Field(description="Include the decision log entries (without "
                                                                      "their feature vectors).")] = False,
                max_decisions: Annotated[int, Field(ge=1, le=1000, description="Decision entries per session when "
                                                                               "included.")] = 50,
        ) -> dict:
            """One uploaded race weekend: per session the classification, plus a summary of the decision log.

            Document format 2 (schema/race-results in the game's GameAPI folder). Per session: kind (qualifying /
            race), track, started_at / ended_at (Unix ms), classification (position, class_position, car_id,
            car_number, car_class, name, driver human/ai, local_seat, grid, laps, gap_to_leader_s, best_lap_s;
            steam ids are left out), decisions_count by decision_type (card, pit, tire) and chosen_by. A pick made by
            hand is not logged by the game; whether API picks count as by hand is not documented.
            """
            rid = store.resolve(result_id)
            if rid is None:
                return failure("bad_argument", detail="(no stored upload %r; see list_race_results)" % result_id)
            # parsing up to 256 MB of JSON must not stall the telemetry listener and the event loop
            document, headers = await asyncio.to_thread(store.load, rid)
            if not isinstance(document, dict):
                return failure("bad_argument", detail="(upload %s is not a JSON object)" % rid)
            out = {"ok": True, "id": rid, "format": document.get("format"),
                   "game_version": document.get("game_version"), "public_lobby": document.get("public_lobby"),
                   "timepoint": document.get("timepoint"), "sessions": []}
            for s in document.get("sessions") or []:
                if not isinstance(s, dict):
                    continue
                rows = []
                for c in s.get("classification") or []:
                    if isinstance(c, dict):
                        rows.append({k: v for k, v in c.items() if k != "steam_id"})
                decisions = [d for d in (s.get("decisions") or []) if isinstance(d, dict)]
                counts = {}
                for d in decisions:
                    key = "%s/%s" % (d.get("decision_type"), d.get("chosen_by"))
                    counts[key] = counts.get(key, 0) + 1
                entry = {"kind": s.get("kind"), "track": s.get("track"), "started_at": s.get("started_at"),
                         "ended_at": s.get("ended_at"), "race_id": s.get("race_id"), "classification": rows,
                         "decisions_count": len(decisions), "decisions_by_type": counts}
                if include_decisions:
                    entry["decisions"] = [{k: v for k, v in d.items() if k not in ("state", "offered")}
                                          for d in decisions[:max_decisions]]
                out["sessions"].append(entry)
            if headers.get("problems"):
                out["problems"] = headers["problems"]
            return clean(out)

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
        """The most recent trigger of the current session (null when none)."""
        engine = client.triggers
        latest = engine.triggers[-1] if engine.triggers else None
        return json.dumps({"trigger": None if latest is None else clean(engine.public(latest)),
                           "last_seq": engine.seq}, separators=(",", ":"))

    mcp.resource(TRIGGERS_URI, name="latest_trigger", mime_type="application/json",
                 description="The most recent trigger (card_offer, car_damaged, tires_wont_last, tire_screen). "
                             "The server sends resource-updated notifications for it when it can.")(_latest_trigger)

    def race_engineer() -> str:
        """Run a race as the race engineer: wait for triggers and answer them."""
        return RACE_ENGINEER_PROMPT

    mcp.prompt(name="race_engineer", description="Act as race engineer for player one's car in Backseat Champions: "
                                                 "loop on wait_for_trigger and answer each decision.")(race_engineer)
    return mcp


RACE_ENGINEER_PROMPT = """\
You are the race engineer for player one's car in Backseat Champions. The car is driven by the game's AI; you make the
strategic calls through the backseat-champions tools. The server never acts by itself.

1. Call get_api_status. If the game is unreachable or switches are off, tell me what to change (Settings >
   Telemetry) and stop. Note whether Decision Events and Player Input are on.
2. Loop: call wait_for_trigger. On status "timeout" call it again. On "session_ended", summarise the race briefly
   and call it again to wait for the next session.
3. For each trigger (act on the newest; skip superseded ones):
   - card_offer: compare the cards (upgrade, facility, tier) with the race situation in the context
     (get_race_snapshot / get_player_state / get_session if you need more). Optionally ban_card(index) first (it
     costs a ban token and keeps your turn), then pick_card(index). Use only what the data says; do not invent
     what an upgrade does.
   - car_damaged: a puncture is definite; a performance_drop is a heuristic. Decide whether to call set_pit(true)
     (only if can_pit).
   - tires_wont_last: the forecast says the tires will not last until the end of the next lap. Weigh laps_remaining,
     pit_lane_time_loss_s and position, then decide set_pit(true).
   - After calling a stop: wait for the tire screen (a tire_screen trigger if enabled, else
     wait_for_event(codes=["TIRE"])) and pick_tire with one of `allowed` that has picks left.
4. If a command fails, read error_reason / hint and adapt; do not retry blindly.
Keep each turn short: one line on what you decided and why.
"""


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
    ("--triggers", "BC_TRIGGERS", "enabled triggers, comma list (default card_offer,car_damaged,tires_wont_last; "
                                  "optional tire_screen; 'none' disables all)"),
    ("--tire-margin", "BC_TIRE_MARGIN", "tires_wont_last: predicted health threshold (default 0.05)"),
    ("--tire-floor", "BC_TIRE_FLOOR", "tires_wont_last: absolute health floor (default 0.10)"),
    ("--perf-drop", "BC_TRIGGER_PERF_DROP", "car_damaged heuristic: relative performance drop (default 0.15; 0 off)"),
    ("--wait-max-s", "BC_WAIT_MAX_S", "cap for wait_for_trigger timeout_s (default 300)"),
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
    parser.add_argument("--log-level", default=os.environ.get("BC_LOG_LEVEL", "WARNING"),
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
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
