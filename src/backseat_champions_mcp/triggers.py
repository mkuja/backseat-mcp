"""Trigger engine: decides when the race engineer (the model) has to be woken up.

The server never acts on its own. It watches the decoded telemetry and raises a *trigger* for three conditions
(plus one optional):

``card_offer``
    A new hand of upgrade cards is on offer for seat 0. Source: the ``CARD`` event (needs Decision Events). When
    Decision Events is off, the Player packet is used instead (points > 0 and a hand whose cards differ from the
    last hand seen, stable for a short debounce). With Decision Events on, the Player packet still reports a hand
    that no CARD event announced within ``CARD_EVENT_GRACE_S`` (for instance a hand already on offer when this
    server started). One hand is one trigger even if both sources report it: a
    ``CARD`` event for the same cards is dropped when the Player fallback already reported them, and a ``CARD``
    event with the same cards and the same points as the previous trigger (for instance after a ban) is dropped too.
``car_damaged``
    Definite: the puncture flag of seat 0's car going 0 -> 1 (Player, or Car Telemetry when Player Data is off).
    Heuristic, labelled as such: the README says Car Status performance is "moved by weather, slipstream,
    abilities and failures" but has no failure flag, so a sudden relative drop of one performance area of my car
    (``perf_drop``, default 15 % within ``perf_window_s``) that the other cars' same area does not share (weather)
    and that does not coincide with my car's abilities (boost, DRS, slipstream, recharge) is reported as a possible
    failure. Temporary upgrades expiring could look the same; the trigger says ``heuristic: true``.
``tires_wont_last``
    Tire health is predicted not to last until the end of the NEXT lap (the lap after the current one: if the car
    does not pit at the end of this lap it has to run that one too), i.e. predicted health <= ``tire_margin``, or
    health is already <= ``tire_floor``. Wear per lap comes from my car's tire health at lap boundaries on the
    current tire set (median of the last three laps); before one full lap is measured it is extrapolated from the
    wear so far in this lap and the elapsed share of a lap (observed lap time, else the strategy model's
    ``reference_lap_s``, else the track's ``lap_time_est_s``). Silent when the car finished, is on its last lap
    (laps_remaining <= 1, when the session has a lap count; without a fresh Player packet laps_remaining is
    total_laps - laps completed), is in the pit lane, or a stop is already called.
``tire_screen`` (off by default)
    A ``TIRE`` event for seat 0: the tire selection screen opened and ``pick_tire`` is now possible.

Every trigger is edge-triggered and re-arms: a new hand is a new occurrence; a puncture re-arms when the flag
clears or the car leaves the pit lane; the tire trigger re-arms after a pit exit / tire change or when the
prediction recovers above the margin (with a small hysteresis); a performance area re-arms when it recovers.

All thresholds are defaults chosen for this server, not game facts. They are tunable via environment variables.

The engine is synchronous and has no I/O; :class:`GameClient` feeds it from the telemetry callback. Times are
monotonic seconds supplied by the caller (tests pass synthetic clocks); lap times use the packets'
``session_time``.
"""
import asyncio
import collections
import statistics
import time

from . import protocol as P

TRIGGER_TYPES = ("card_offer", "car_damaged", "tires_wont_last", "tire_screen")
DEFAULT_ENABLED = ("card_offer", "car_damaged", "tires_wont_last")

HEALTH_JUMP = 0.02          # tire health rising by more than this means a new set of tires
REARM_HYSTERESIS = 0.03     # the tire prediction must recover this far above the margin to re-arm
PLAYER_FRESH_S = 1.0        # Player packet younger than this is preferred over Car Telemetry / Lap Data
ABILITY_GRACE_S = 1.0       # extra seconds after an ability was active in which a performance drop is ignored
MAX_SAMPLES = 3             # per-lap wear samples used for the median
CARD_EVENT_GRACE_S = 2.0    # with Decision Events on: a hand no CARD event announced is reported after this


class TriggerConfig:
    """Which triggers are enabled and the heuristic thresholds. All values are tunable defaults, not game facts."""

    def __init__(self, enabled=DEFAULT_ENABLED, perf_drop=0.15, perf_window_s=2.0, tire_margin=0.05,
                 tire_floor=0.10, card_debounce_s=0.25, min_lap_share=0.25, queue_size=200):
        enabled = [e.strip().lower() for e in enabled if e and e.strip()]
        unknown = [e for e in enabled if e not in TRIGGER_TYPES]
        if unknown:
            raise ValueError("unknown trigger(s) %s; known: %s" % (", ".join(unknown), ", ".join(TRIGGER_TYPES)))
        self.enabled = frozenset(enabled)
        self.perf_drop = float(perf_drop)
        self.perf_window_s = float(perf_window_s)
        self.tire_margin = float(tire_margin)
        self.tire_floor = float(tire_floor)
        self.card_debounce_s = float(card_debounce_s)
        self.min_lap_share = float(min_lap_share)
        self.queue_size = int(queue_size)
        if not (0.0 <= self.perf_drop < 1.0):
            raise ValueError("BC_TRIGGER_PERF_DROP must be in [0, 1) (0 disables the heuristic)")
        if not (0.0 <= self.tire_margin < 1.0 and 0.0 <= self.tire_floor < 1.0):
            raise ValueError("BC_TIRE_MARGIN and BC_TIRE_FLOOR must be in [0, 1)")
        if self.perf_window_s <= 0:
            raise ValueError("BC_TRIGGER_PERF_WINDOW_S must be > 0")

    @classmethod
    def from_env(cls, env):
        def num(name, default):
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                raise ValueError("%s must be a number, got %r" % (name, raw))

        raw = env.get("BC_TRIGGERS")
        enabled = DEFAULT_ENABLED if raw is None or not raw.strip() else raw.split(",")
        if raw is not None and raw.strip().lower() == "none":
            enabled = ()
        return cls(enabled=enabled, perf_drop=num("BC_TRIGGER_PERF_DROP", 0.15),
                   perf_window_s=num("BC_TRIGGER_PERF_WINDOW_S", 2.0), tire_margin=num("BC_TIRE_MARGIN", 0.05),
                   tire_floor=num("BC_TIRE_FLOOR", 0.10))

    def public(self):
        return {
            "enabled": sorted(self.enabled),
            "available": list(TRIGGER_TYPES),
            "thresholds": {
                "tire_margin": self.tire_margin,
                "tire_floor": self.tire_floor,
                "perf_drop": self.perf_drop,
                "perf_window_s": self.perf_window_s,
                "card_debounce_s": self.card_debounce_s,
                "min_lap_share_for_extrapolation": self.min_lap_share,
            },
            "note": "Thresholds are this server's tunable heuristics (BC_TIRE_MARGIN, BC_TIRE_FLOOR, "
                    "BC_TRIGGER_PERF_DROP, BC_TRIGGER_PERF_WINDOW_S), not values from the game.",
        }


def _r(value, digits=3):
    return None if value is None else round(value, digits)


def _cards_sig(hand):
    return tuple((c["upgrade"], c["facility"], c["tier"]) for c in hand)


class TriggerEngine:
    def __init__(self, config=None, clock=time.monotonic):
        self.config = config or TriggerConfig()
        self.clock = clock
        self.seq = 0
        self.triggers = collections.deque(maxlen=max(10, self.config.queue_size))
        self.delivered_seq = 0
        self.end_count = 0
        self.end_delivered = 0
        self.end_info = None
        self.listeners = []          # callables(trigger dict), called synchronously when a trigger fires
        self._waiters = set()
        self.decision_events = None  # from hello: True / False / None (unknown)
        self.sessions = 0
        self._reset_state(None)

    # -------------------------------------------------------------------------------------------- state

    def _reset_state(self, uid):
        self.session_uid = uid
        self.my_car = None
        self.events_seen = False
        self.session_time = None
        self.total_laps = None       # Session / Player; 0 = no lap count
        self.laps_remaining = None
        self.lap = None              # laps completed (Lap Data / LAPC)
        self.finished = False
        self.in_pits = None
        self.pit_planned = None
        self.can_pit = None
        self.health = None
        self.compound = None
        self.compound_picks_left = None
        self.puncture = None
        self.player_at = None
        self.reference_lap_s = None
        self.lap_time_est_s = None
        self.pit_lane_time_loss_s = None
        self.ban_tokens = None
        self.points = None
        self.hand = None
        # card_offer
        self._last_card = None       # {"sig", "points", "source", "cleared", "seq"}
        self._card_candidate = None  # (sig, first_seen, points, hand) for the Player fallback
        self._player_hand_sig = None
        # car_damaged
        self._puncture_armed = True
        self._perf_hist = collections.defaultdict(collections.deque)   # car -> deque[(t, [8 floats])]
        self._perf_alarm = {}        # area index -> baseline value while alarmed
        self._ability_at = None
        # tires
        self._tires_armed = True
        self._stint_id = 0
        self._stint = None
        self._lap_times = collections.deque(maxlen=5)
        self._last_boundary = None   # (lap, session_time) of the last lap boundary seen, any stint
        self._last_eval = None

    def new_session(self, uid):
        """A new session_uid: forget everything about the old session, including queued triggers."""
        self.sessions += 1
        self._reset_state(uid)
        self.triggers.clear()
        self.delivered_seq = self.seq
        self.end_delivered = self.end_count
        self.end_info = None

    def set_decision_events(self, on):
        self.decision_events = on

    # -------------------------------------------------------------------------------------------- feeding

    def feed(self, packet_id, header, body, now=None):
        """Feed one decoded packet (header and body dicts from :mod:`protocol`)."""
        now = self.clock() if now is None else now
        if header.get("session_uid") != self.session_uid:
            self.new_session(header.get("session_uid"))
        seat_car = header.get("seat_car") or [None]
        if seat_car[0] is not None:
            self.my_car = seat_car[0]
        self.session_time = header.get("session_time", self.session_time)
        handler = {
            P.PKT_PLAYER: self._feed_player,
            P.PKT_CAR_TELEMETRY: self._feed_telemetry,
            P.PKT_LAP_DATA: self._feed_lap_data,
            P.PKT_CAR_STATUS: self._feed_status,
            P.PKT_SESSION: self._feed_session,
            P.PKT_EVENT: self._feed_event,
            P.PKT_CAR_ABILITIES: self._feed_abilities,
        }.get(packet_id)
        if handler is not None:
            handler(header, body, now)

    def _player_fresh(self, now):
        return self.player_at is not None and now - self.player_at < PLAYER_FRESH_S

    def _feed_player(self, header, body, now):
        if body.get("seat") != 0:
            return
        self.player_at = now
        self.my_car = body.get("car", self.my_car)
        self.total_laps = body["total_laps"]
        self.laps_remaining = body["laps_remaining"]
        self.can_pit = body["can_pit"]
        self.compound_picks_left = body["compound_picks_left"]
        self.points = body["points"]
        self.hand = body["hand"]
        if body.get("strategy"):
            self.reference_lap_s = body["strategy"].get("reference_lap_s") or self.reference_lap_s
        self._set_pits(body["in_pits"], body["pit_planned"], now)
        self._set_tires(body["tire_health"], body["tire_compound"], body["puncture"], now, "player")
        self._player_cards(body["points"], body["hand"], now)
        self._eval_tires(now)

    def _feed_telemetry(self, header, body, now):
        car = self.my_car
        if car is None or car >= len(body["cars"]) or self._player_fresh(now):
            return
        c = body["cars"][car]
        self._set_tires(c["tire_health"], c["tire_compound"], c["puncture"], now, "car_telemetry")
        self._eval_tires(now)

    def _feed_lap_data(self, header, body, now):
        car = self.my_car
        if car is None or car >= len(body["cars"]):
            return
        c = body["cars"][car]
        self.finished = c["finished"]
        if not self._player_fresh(now):
            self._set_pits(c["in_pits"], c["pit_planned"], now)
        self._set_lap(c["lap"], now)
        self._derive_laps_remaining(now)
        self._eval_tires(now)

    def _derive_laps_remaining(self, now):
        """Without a fresh Player packet, laps_remaining = total_laps - laps completed (the capture shows the
        Player packet's own value obeys that: 24 total, 2 completed, 22 remaining), so the last lap is still
        recognised when Player Data is off."""
        if self._player_fresh(now) or not self.total_laps or self.lap is None:
            return
        self.laps_remaining = max(0, self.total_laps - self.lap)

    def _feed_session(self, header, body, now):
        if self.total_laps is None or not self._player_fresh(now):
            self.total_laps = body["total_laps"]
        track = body.get("track")
        if track:
            self.lap_time_est_s = track.get("lap_time_est_s") or None
            self.pit_lane_time_loss_s = track.get("pit_lane_time_loss_s")

    def _feed_abilities(self, header, body, now):
        car = self.my_car
        if car is None or car >= len(body["cars"]):
            return
        c = body["cars"][car]
        self.ban_tokens = c["ban_tokens"]
        active = any(c[k] for k in ("speed_boost", "cornering_boost", "super_boost", "party_mode", "drs",
                                    "boost_overspeed", "recharging")) or c["slipstream"] > 0.05
        if active:
            self._ability_at = now

    def _feed_event(self, header, body, now):
        self.events_seen = True
        code, seat = body.get("code"), body.get("seat")
        if code == "SEND" or (code == "FINI" and seat == 0):
            self.end_count += 1
            self.end_info = {"code": code, "position": body.get("position"),
                             "session_time_s": _r(header.get("session_time"), 2)}
            self.finished = self.finished or code == "FINI"
            self._wake()
            return
        if code == "SSTA":
            self.end_info = None
            return
        if seat != 0:
            return
        if code == "CARD":
            if body.get("points", 0) > 0 and body.get("hand"):
                self._card_offer(body["points"], body["hand"], now, "event")
        elif code == "TIRE":
            if "tire_screen" in self.config.enabled:
                self._fire("tire_screen", now, {
                    "pre_race": body.get("pre_race"), "current": body.get("current"),
                    "allowed": body.get("allowed"), "picks_left": body.get("picks_left"),
                    "action": "pick_tire(compound) with one of 'allowed' (and picks left), while the screen is open."})
        elif code == "LAPC":
            self._set_lap(body.get("lap"), now)
            self._derive_laps_remaining(now)
            self._eval_tires(now)
        elif code == "PITO":
            if self.in_pits:
                self._set_pits(False, self.pit_planned, now)
            else:
                self._pit_exit(now)
        elif code == "PITI":
            self._set_pits(True, self.pit_planned, now)

    # -------------------------------------------------------------------------------------------- card_offer

    def _player_cards(self, points, hand, now):
        if points <= 0 or not hand:
            self._player_hand_sig = None
            self._card_candidate = None
            if self._last_card is not None:
                self._last_card["cleared"] = True
            return
        sig = _cards_sig(hand)
        if self._last_card is not None and sig != self._last_card["sig"]:
            self._last_card["cleared"] = True     # a different hand is showing: the next CARD is new
        self._player_hand_sig = sig
        fallback = not self.events_seen and self.decision_events is not True
        wait = self.config.card_debounce_s if fallback else CARD_EVENT_GRACE_S
        if self._last_card is not None and not self._last_card["cleared"] and self._last_card["sig"] == sig:
            return
        cand = self._card_candidate
        if cand is None or cand[0] != sig:
            self._card_candidate = (sig, now)
            cand = self._card_candidate
        if now - cand[1] >= wait:
            self._card_candidate = None
            self._card_offer(points, hand, now, "player" if fallback else "player_catch_up")

    def _card_offer(self, points, hand, now, source):
        sig = _cards_sig(hand)
        last = self._last_card
        if last is not None and not last["cleared"] and last["sig"] == sig:
            if source != "event" or last["source"] != "event" or last["points"] == points:
                return
        if "card_offer" not in self.config.enabled:
            self._last_card = {"sig": sig, "points": points, "source": source, "cleared": False, "seq": None}
            return
        trig = self._fire("card_offer", now, {
            "source": {"event": "CARD event", "player": "Player packet (Decision Events off)",
                       "player_catch_up": "Player packet (a hand no CARD event announced)"}[source],
            "points": points, "hand": hand, "ban_tokens": self.ban_tokens,
            "action": "pick_card(index) spends a point on a card; ban_card(index) spends a ban token and keeps "
                      "your turn (not on the last card left to pick)."})
        self._last_card = {"sig": sig, "points": points, "source": source, "cleared": False, "seq": trig["seq"]}

    # -------------------------------------------------------------------------------------------- damage

    def _set_tires(self, health, compound, puncture, now, source):
        prev_health, prev_compound = self.health, self.compound
        self.health = health
        self.compound = compound
        if self._stint is None:
            self._new_stint(now, "first sample")
        elif prev_compound is not None and compound != prev_compound:
            self._new_stint(now, "compound changed")
        elif prev_health is not None and health > prev_health + HEALTH_JUMP:
            self._new_stint(now, "tire health rose")
        # puncture edge
        if puncture and not self.puncture and self._puncture_armed:
            self._puncture_armed = False
            if "car_damaged" in self.config.enabled:
                self._fire("car_damaged", now, {
                    "signal": "puncture", "heuristic": False, "source": source,
                    "tire_health": _r(health), "compound": compound,
                    "detail": "The puncture flag (tire_flags bit 0) of your car went from 0 to 1."})
        if not puncture:
            self._puncture_armed = True
        self.puncture = puncture

    def _feed_status(self, header, body, now):
        cfg = self.config
        cars = body["cars"]
        window = cfg.perf_window_s
        for i, c in enumerate(cars):
            hist = self._perf_hist[i]
            hist.append((now, [c["performance"][a] for a in P.PERFORMANCE_AREAS]))
            while hist and now - hist[0][0] > window:
                hist.popleft()
        car = self.my_car
        if cfg.perf_drop <= 0 or car is None or car >= len(cars) or "car_damaged" not in cfg.enabled:
            return
        mine = self._perf_hist[car]
        if len(mine) < 2:
            return
        current = mine[-1][1]
        for a, area in enumerate(P.PERFORMANCE_AREAS):
            cur = current[a]
            if a in self._perf_alarm:
                if cur >= self._perf_alarm[a] * (1 - cfg.perf_drop / 2):
                    del self._perf_alarm[a]
                continue
            drop, base = self._drop(mine, a)
            if drop < cfg.perf_drop:
                continue
            others = [self._drop(self._perf_hist[i], a)[0] for i in range(len(cars))
                      if i != car and len(self._perf_hist[i]) >= 2]
            shared = [d for d in others if d >= cfg.perf_drop / 2]
            if others and len(shared) * 2 >= len(others):
                continue                          # most of the field dropped too: weather, not a failure
            if self._ability_at is not None and now - self._ability_at <= window + ABILITY_GRACE_S:
                continue                          # boost / DRS / slipstream moves performance on its own
            if self.in_pits:
                continue
            self._perf_alarm[a] = base
            self._fire("car_damaged", now, {
                "signal": "performance_drop", "heuristic": True, "area": area,
                "from": _r(base), "to": _r(cur), "drop_fraction": _r(drop),
                "window_s": window, "other_cars_dropping": len(shared), "other_cars_compared": len(others),
                "detail": "Possible failure: this performance area fell by more than the threshold within the window "
                          "while most other cars' same area did not, and no ability of your car was active. The "
                          "game reports no failure flag; this is a heuristic (an expiring temporary upgrade could "
                          "look the same)."})

    @staticmethod
    def _drop(hist, area):
        if len(hist) < 2:
            return 0.0, None
        base = max(v[area] for _, v in list(hist)[:-1])
        cur = hist[-1][1][area]
        if base <= 0:
            return 0.0, base
        return max(0.0, (base - cur) / base), base

    # -------------------------------------------------------------------------------------------- pits / laps

    def _set_pits(self, in_pits, pit_planned, now):
        was = self.in_pits
        self.in_pits = in_pits
        self.pit_planned = pit_planned
        if in_pits and self._stint is not None:
            self._stint["pitted"] = True
        if was and not in_pits:
            self._pit_exit(now)

    def _pit_exit(self, now):
        self._new_stint(now, "pit exit")
        self._puncture_armed = True
        self.puncture = None         # a puncture still showing after the stop is reported again
        self._perf_alarm.clear()

    def _set_lap(self, lap, now):
        if lap is None:
            return
        if self.lap is None:
            self.lap = lap           # first sight (perhaps mid-lap): not a boundary
            return
        if lap <= self.lap:
            return
        prev = self._last_boundary
        t = self.session_time
        if prev is not None and t is not None and prev[1] is not None:
            dlap = lap - prev[0]
            if dlap >= 1 and t > prev[1]:
                self._lap_times.append((t - prev[1]) / dlap)
        self.lap = lap
        self._last_boundary = (lap, t)
        st = self._stint
        if st is not None and self.health is not None and t is not None:
            if st["boundaries"]:
                plap, ph, _ = st["boundaries"][-1]
                dlap = lap - plap
                if dlap >= 1 and not st["pitted"]:
                    sample = (ph - self.health) / dlap
                    if sample >= 0:
                        st["samples"].append(sample)
                        del st["samples"][:-MAX_SAMPLES]
            st["boundaries"].append((lap, self.health, t))
            del st["boundaries"][:-MAX_SAMPLES - 1]
            st["pitted"] = False

    def _new_stint(self, now, why):
        self._stint_id += 1
        self._stint = {"id": self._stint_id, "why": why, "compound": self.compound, "start_health": self.health,
                       "start_t": self.session_time, "boundaries": [], "samples": [], "pitted": False}
        if why != "first sample":
            self._tires_armed = True

    # -------------------------------------------------------------------------------------------- tires

    def lap_time(self):
        """(seconds, source) of the lap time used for extrapolation, or (None, None)."""
        if self._lap_times:
            return statistics.median(self._lap_times), "observed"
        if self.reference_lap_s:
            return self.reference_lap_s, "reference_lap_s"
        if self.lap_time_est_s:
            return self.lap_time_est_s, "lap_time_est_s"
        return None, None

    def wear_estimate(self):
        """(wear per lap, how derived, samples) for the current tire set."""
        st = self._stint
        if st is None or self.health is None:
            return None, "no tire data", 0
        if st["samples"]:
            return statistics.median(st["samples"]), "median of the last %d full lap(s) on this set" \
                % len(st["samples"]), len(st["samples"])
        lap_s, src = self.lap_time()
        if st["boundaries"]:
            _, ref_h, ref_t = st["boundaries"][-1]
        else:
            ref_h, ref_t = st["start_health"], st["start_t"]
        t = self.session_time
        if lap_s and ref_h is not None and ref_t is not None and t is not None and t > ref_t:
            share = (t - ref_t) / lap_s
            if share >= self.config.min_lap_share:
                wear = max(0.0, ref_h - self.health) / share
                return wear, "extrapolated from %.0f%% of a lap (lap time: %s)" % (share * 100, src), 0
        return None, "not enough data yet (needs a full lap, or %d%% of one with a known lap time)" \
            % round(self.config.min_lap_share * 100), 0

    def _share_into_lap(self):
        lap_s, _ = self.lap_time()
        if not lap_s or self._last_boundary is None or self._last_boundary[1] is None or self.session_time is None:
            return None
        return max(0.0, min(1.0, (self.session_time - self._last_boundary[1]) / lap_s))

    def tire_prediction(self):
        """The tire forecast used by ``tires_wont_last``."""
        wear, how, n = self.wear_estimate()
        share = self._share_into_lap()
        remaining_current = 1.0 if share is None else 1.0 - share
        lookahead = remaining_current + 1.0
        has_laps = bool(self.total_laps) and self.laps_remaining is not None
        if has_laps:
            race_left = max(0.0, (self.laps_remaining - 1) + remaining_current)
            lookahead = min(lookahead, race_left)
        predicted = None if wear is None or self.health is None else self.health - wear * lookahead
        return {"tire_health": _r(self.health), "wear_per_lap": _r(wear, 4), "wear_derived": how,
                "full_lap_samples": n, "share_of_current_lap_done": _r(share, 2),
                "lookahead_laps": _r(lookahead, 2), "predicted_health_end_of_next_lap": _r(predicted)}

    def _silenced(self):
        if self.finished:
            return "finished"
        if self.total_laps and self.laps_remaining is not None and self.laps_remaining <= 1:
            return "last lap"
        if self.in_pits:
            return "in the pit lane"
        if self.pit_planned:
            return "pit stop already called"
        return None

    def _eval_tires(self, now):
        if self.health is None or self.my_car is None:
            return
        cfg = self.config
        pred = self.tire_prediction()
        predicted = pred["predicted_health_end_of_next_lap"]
        low = self.health <= cfg.tire_floor
        bad = low or (predicted is not None and predicted <= cfg.tire_margin)
        self._last_eval = dict(pred, silenced=self._silenced(), armed=self._tires_armed)
        if not self._tires_armed:
            ok_pred = predicted is None or predicted > cfg.tire_margin + REARM_HYSTERESIS
            if ok_pred and self.health > cfg.tire_floor + REARM_HYSTERESIS:
                self._tires_armed = True
            return
        if not bad or self._silenced() or "tires_wont_last" not in cfg.enabled:
            return
        self._tires_armed = False
        lap_s, lap_src = self.lap_time()
        payload = dict(pred)
        payload.update({
            "reason": "floor" if low else "prediction",
            "tire_margin": cfg.tire_margin, "tire_floor": cfg.tire_floor,
            "laps_remaining": self.laps_remaining, "can_pit": self.can_pit, "pit_planned": self.pit_planned,
            "compound": self.compound, "compound_picks_left": self.compound_picks_left,
            "pit_lane_time_loss_s": _r(self.pit_lane_time_loss_s, 2),
            "lap_time_s": _r(lap_s, 2), "lap_time_source": lap_src,
            "heuristic": True,
            "action": "Decide whether to call a stop with set_pit(true) (needs can_pit); then pick_tire when the "
                      "tire screen opens.",
        })
        self._fire("tires_wont_last", now, payload)

    # -------------------------------------------------------------------------------------------- queue

    def _fire(self, kind, now, payload):
        self.seq += 1
        trig = {"seq": self.seq, "type": kind, "session_uid": self.session_uid,
                "session_time_s": _r(self.session_time, 2), "lap": self.lap, "_at": now,
                "_stint": self._stint_id}
        trig.update(payload)
        self.triggers.append(trig)
        for listener in list(self.listeners):
            try:
                listener(trig)
            except Exception:  # a listener must never break telemetry processing
                pass
        self._wake()
        return trig

    def _wake(self):
        for waiter in list(self._waiters):
            if not waiter.done():
                waiter.set_result(None)
        self._waiters.clear()

    def public(self, trig, now=None):
        now = self.clock() if now is None else now
        out = {k: v for k, v in trig.items() if not k.startswith("_") and k != "session_uid"}
        out["age_s"] = round(now - trig["_at"], 2)
        later = [t for t in self.triggers if t["seq"] > trig["seq"] and t["type"] == trig["type"]]
        superseded = bool(later)
        if trig["type"] == "tires_wont_last" and trig["_stint"] != self._stint_id:
            superseded = True        # the car has had new tires (or left the pits) since
        out["superseded"] = superseded
        return out

    def after(self, since_seq=0, limit=None):
        found = [t for t in self.triggers if t["seq"] > since_seq]
        if limit is not None and len(found) > limit:
            found = found[-limit:]
        return found

    def pending(self, since_seq=None):
        """``(triggers, session_end)`` not yet delivered (or after ``since_seq``)."""
        cursor = self.delivered_seq if since_seq is None else since_seq
        trig = [t for t in self.triggers if t["seq"] > cursor]
        end = self.end_count > self.end_delivered
        return trig, end

    def mark_delivered(self, trig, end):
        if trig:
            self.delivered_seq = max(self.delivered_seq, trig[-1]["seq"])
        if end:
            self.end_delivered = self.end_count

    def waiter(self):
        fut = asyncio.get_running_loop().create_future()
        self._waiters.add(fut)
        return fut

    def discard_waiter(self, fut):
        self._waiters.discard(fut)

    def state(self):
        """Internal state for ``get_trigger_config``."""
        st = self._stint
        lap_s, lap_src = self.lap_time()
        last_card = None
        if self._last_card is not None:
            last_card = {"seq": self._last_card["seq"], "points": self._last_card["points"],
                         "source": self._last_card["source"], "hand_still_showing": not self._last_card["cleared"]}
        return {
            "session_uid": self.session_uid, "my_car": self.my_car,
            "decision_events": self.decision_events, "events_seen_this_session": self.events_seen,
            "card_offer_source": "CARD event" if (self.events_seen or self.decision_events)
            else "Player packet fallback",
            "last_card_offer": last_card,
            "puncture": self.puncture, "puncture_armed": self._puncture_armed,
            "performance_areas_alarmed": [P.PERFORMANCE_AREAS[a] for a in sorted(self._perf_alarm)],
            "tires": {
                "armed": self._tires_armed, "silenced_because": self._silenced(),
                "compound": self.compound, "stint_id": self._stint_id,
                "stint_started_because": st["why"] if st else None,
                "per_lap_samples": [_r(s, 4) for s in st["samples"]] if st else [],
                "lap_time_s": _r(lap_s, 2), "lap_time_source": lap_src,
                "laps_completed": self.lap, "laps_remaining": self.laps_remaining,
                "in_pits": self.in_pits, "pit_planned": self.pit_planned,
                **self.tire_prediction(),
            },
            "queued": len(self.triggers), "last_seq": self.seq, "delivered_up_to_seq": self.delivered_seq,
        }
