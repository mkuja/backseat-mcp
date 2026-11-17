"""Trigger engine: decides when the race engineer (the model) has to be woken up.

The server never sends a gameplay command on its own. It watches the decoded telemetry and queues a *trigger* for
these conditions:

``card_offer``
    A new hand of upgrade cards is on offer for seat 0. Source: the ``CARD`` event (needs Decision Events). When
    Decision Events is off, the Player packet is used instead (points > 0 and a hand whose cards differ from the
    last hand seen, stable for ``card_debounce_s``). With Decision Events on, the Player packet still reports a hand
    that no CARD event announced within ``CARD_EVENT_GRACE_S`` (for instance a hand already on offer when this
    server started). One hand is one trigger even if both sources report it. A hand counts as closed only when the
    Player packet shows no hand (or another hand) for at least ``card_debounce_s`` of wall clock, and, unless the
    Player packet has confirmed the hand once, not before ``CARD_EVENT_GRACE_S`` after the offer (the Player packet
    can lag the CARD event by a few ticks).
``car_damaged``
    Definite: the puncture flag of seat 0's car set in two consecutive sampled packets of the same source (Player,
    or Car Telemetry when Player Data is off). It re-arms after the flag was clear for ``PUNCTURE_REARM_ST`` seconds
    of session time, or at a pit exit (a puncture still showing after the stop is reported again). Sampling is
    ~10 Hz, so a puncture flag shorter than ~0.2 s can be missed.
    Heuristic, labelled as such: the README says Car Status performance is "moved by weather, slipstream,
    abilities and failures" but has no failure flag, so a relative drop of performance areas of my car
    (``perf_drop``, default 15 % within ``perf_window_s`` of session time) is reported as a possible failure when it
    persists ``PERF_CONFIRM_ST`` seconds of session time, the field's median of the same area did not drop by half
    the threshold from ``perf_window_s`` before the drop until the confirmation (weather), and no ability of my car
    (boost, DRS, slipstream, recharge) was active. A drop while an ability is active is not reported (accepted miss).
    One trigger lists every area that dropped. History is kept for "my car" (each Car Status packet's own
    ``seat_car[0]``) and the field median, never per car index: the game re-orders car indices by race position.
``tires_wont_last``
    Tire health is predicted not to last until the end of the NEXT lap (predicted health <= ``tire_margin``), or,
    only when there is no forecast yet, health is already <= ``tire_floor`` (reason ``floor_no_forecast``). Wear per
    lap comes from my car's tire health at lap boundaries on the current tire set (median of the last three laps);
    the current lap is also extrapolated (from a fresh baseline, at least ``min_lap_share`` of a lap) and the higher
    of the two is used. All of it runs on the packets' ``session_time``, which stops while the game is paused.
    Silent when the car finished, is on its last lap (session has a lap count and laps_remaining <= 1), is in the
    pit lane, or a stop is already called. The last-lap silence needs Lap Data or Player Data (without either there
    is no lap count). Tire health is smoothed (median of the last 5 samples) and a new tire set is recognised only
    from a pit exit, a compound change or a large (>= 0.15), sustained rise of health; noise never starts one.
``tire_screen``
    A ``TIRE`` event for seat 0: the tire selection screen opened and ``pick_tire`` is now possible.
``session_ended``
    Always queued (not configurable): SEND, or FINI for seat 0, once per session; a synthetic one (code null, reason
    ``new_session_started``) when a new session starts before the old one reported its end.

Laps: ``laps_completed`` is the maximum of Lap Data ``lap`` (my car by the packet's own seat_car[0]) and Player
``total_laps - laps_remaining``, monotone within a session; ``laps_remaining = total_laps - laps_completed``. The LAPC
event's ``lap`` is ambiguous (completed laps or the new lap's number) and is only recorded (``lap_observations``).

Delivery (``collect``): at-least-once with a cursor the model acknowledges with ``since_seq``, plus at most
``MAX_RESEND`` rate-limited (``REMIND_S``) resends of open decisions. Entries that are superseded, belong to an old
session (except session_ended) or are decisions already closed are skipped, not returned.

Time base: session_time for game logic (tire wear, lap times, perf window/confirm, puncture re-arm, card "game
resumed"); the caller's monotonic ``now`` for debounces, pause detection and resends.

All thresholds are defaults chosen for this server, not game facts.
"""
import asyncio
import collections
import os
import statistics
import time

from . import protocol as P

TRIGGER_TYPES = ("card_offer", "car_damaged", "tires_wont_last", "tire_screen")
DEFAULT_ENABLED = ("card_offer", "car_damaged", "tires_wont_last", "tire_screen")
DECISION_TYPES = ("card_offer", "tire_screen")

REMIND_S = 20.0             # wall seconds between resends of an unacknowledged entry
MAX_RESEND = 3              # resends of an open decision (non-decision entries: 1 with since_seq, else 0)

HEALTH_JUMP = 0.15          # tire health rising this far above the smoothed value ...
HEALTH_JUMP_SAMPLES = 2     # ... in this many consecutive samples ...
HEALTH_JUMP_ST = 0.5        # ... spanning this many session seconds means a new set of tires (never on noise)
HEALTH_SMOOTH = 5           # tire health used for wear = median of the last samples on the current set
REARM_HYSTERESIS = 0.10     # the tire prediction must recover this far above the margin to re-arm (noise-proof)
PLAYER_FRESH_S = 1.0        # Player packet younger than this (wall) is preferred over Car Telemetry / Lap Data
ABILITY_GRACE_S = 1.0       # extra session seconds after an ability was active in which a perf drop is ignored
PERF_CONFIRM_ST = 1.0       # a performance drop must persist this long (session time) to be reported
MAX_SAMPLES = 3             # per-lap wear samples used for the median
HEALTH_FRESH_ST = 1.0       # a tire health sample this many session seconds old still counts at a lap boundary
CARD_EVENT_GRACE_S = 2.0    # wall: Player catch-up wait, and min age before an unconfirmed CARD hand can be cleared
CARD_RESUME_ST = 1.0        # session seconds after a CARD event without any Player sample: hand state unknown
PAUSE_AFTER_S = 1.5         # wall seconds of frozen session_time (packets still arriving) = paused
STOPPED_AFTER_S = 3.0       # wall seconds without any packet = stopped, not paused
PAUSE_HINT_S = 5.0          # paused this long with no hand known -> paused_hint
PUNCTURE_CONFIRM = 2        # consecutive sampled packets with the flag set
PUNCTURE_REARM_ST = 3.0     # session seconds the flag must be clear before a new puncture is reported
TIRE_MOVING_KMH = 5.0       # pre-race tire screen closes when my car moves faster than this ...
TIRE_MOVING_ST = 1.0        # ... for this many session seconds
LAP_OBSERVATIONS = 10
EVICTED_MEMORY = 1000


class TriggerConfig:
    """Which triggers are enabled and the heuristic thresholds. All values are tunable defaults, not game facts."""

    def __init__(self, enabled=DEFAULT_ENABLED, perf_drop=0.15, perf_window_s=2.0, tire_margin=0.05,
                 tire_floor=0.10, card_debounce_s=0.25, min_lap_share=0.25, queue_size=200, remind_s=None):
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
        self.remind_s = None if remind_s is None else float(remind_s)
        if not (0.0 <= self.perf_drop < 1.0):
            raise ValueError("BC_TRIGGER_PERF_DROP must be in [0, 1) (0 disables the heuristic)")
        if not (0.0 <= self.tire_margin < 1.0 and 0.0 <= self.tire_floor < 1.0):
            raise ValueError("BC_TIRE_MARGIN and BC_TIRE_FLOOR must be in [0, 1)")
        if not self.perf_window_s > 0:
            raise ValueError("BC_TRIGGER_PERF_WINDOW_S must be > 0")
        if self.remind_s is not None and not self.remind_s > 0:
            raise ValueError("BC_REMIND_S must be > 0")

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
                   tire_floor=num("BC_TIRE_FLOOR", 0.10), remind_s=num("BC_REMIND_S", None))

    def public(self):
        return {
            "enabled": sorted(self.enabled),
            "available": list(TRIGGER_TYPES),
            "always_queued": ["session_ended"],
            "thresholds": {
                "tire_margin": self.tire_margin,
                "tire_floor": self.tire_floor,
                "perf_drop": self.perf_drop,
                "perf_window_s": self.perf_window_s,
                "perf_confirm_session_s": PERF_CONFIRM_ST,
                "card_debounce_s": self.card_debounce_s,
                "min_lap_share_for_extrapolation": self.min_lap_share,
                "puncture_rearm_session_s": PUNCTURE_REARM_ST,
            },
            "delivery": {"resend_after_s": self.remind_s or REMIND_S, "max_resends_open_decision": MAX_RESEND,
                         "max_resends_other_with_since_seq": 1},
            "note": "Thresholds are this server's tunable heuristics (BC_TIRE_MARGIN, BC_TIRE_FLOOR, "
                    "BC_TRIGGER_PERF_DROP, BC_TRIGGER_PERF_WINDOW_S), not values from the game. tires_wont_last "
                    "fires when the forecast for the end of the next lap is <= tire_margin; tire_floor is used "
                    "only while there is no forecast yet (reason floor_no_forecast).",
        }


def _r(value, digits=3):
    return None if value is None else round(value, digits)


def _cards_sig(hand):
    return tuple((c["upgrade"], c["facility"], c["tier"]) for c in hand)


def _sig_str(sig):
    if sig is None:
        return None
    return "|".join("%s/%s/%s" % c for c in sig)


class TriggerEngine:
    def __init__(self, config=None, clock=time.monotonic):
        self.config = config or TriggerConfig()
        self.clock = clock
        self.remind_s = self.config.remind_s if self.config.remind_s is not None \
            else float(os.environ.get("BC_REMIND_S") or REMIND_S)
        self.seq = 0
        self.triggers = collections.deque()            # bounded by hand (eviction is tracked)
        self.maxlen = max(10, self.config.queue_size)
        self.acked = 0                                  # highest seq acknowledged with since_seq
        self.listeners = []          # callables(trigger dict), called synchronously when a trigger fires
        self._waiters = set()
        self.decision_events = None  # from hello: True / False / None (unknown)
        self.sessions = 0
        self._collected = False      # first collect() of the process ignores since_seq (server restart, X4)
        self._since_mode = False     # since_seq has been passed (non-decision entries get one resend)
        self._evicted = collections.deque(maxlen=EVICTED_MEMORY)   # (seq, returns) of entries pushed out
        self._pkt_no = 0
        self._last_feed_wall = None
        self._st_changed_wall = None
        self._reset_state(None)

    # -------------------------------------------------------------------------------------------- state

    def _reset_state(self, uid):
        self.session_uid = uid
        self.my_car = None
        self.events_seen = False
        self.session_time = None
        self.frame = None
        self.total_laps = None       # Session / Player; 0 = no lap count
        self.laps_remaining = None   # total_laps - laps_completed (total > 0)
        self.lap = None              # laps completed (Lap Data / Player), monotone in a session
        self.lap_source = None
        self._lapdata_lap = None
        self._lapdata_wall = None
        self._player_completed = None
        self.lap_observations = collections.deque(maxlen=LAP_OBSERVATIONS)
        self.finished = False
        self.in_pits = None
        self.pit_planned = None
        self.can_pit = None
        self.health = None           # median of the last HEALTH_SMOOTH samples on the current tire set
        self.health_raw = None
        self._health_win = collections.deque(maxlen=HEALTH_SMOOTH)
        self._jump = None            # (first session_time, [samples]) of a pending health jump
        self.health_st = None        # session_time of the latest tire health sample
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
        self._last_card = None
        self._card_candidate = None  # (sig, first_seen wall-clock, points, hand)
        self._player_sig = None      # sig of the newest Player sample's hand (None = no hand)
        self._player_points = None
        self._player_no = None       # packet number of the newest Player sample
        # tire screen
        self._tire = None            # {"open", "pre_race", "current", "allowed", "picks_left", "seq", ...}
        self._moving_since_st = None
        # car_damaged: puncture
        self._puncture_armed = True
        self._punct_seen_count = 0
        self._punct_source = None
        self._punct_clear_since_st = None
        # car_damaged: performance ("me" and "field" median per Car Status packet, never per car index)
        self._perf_hist = {"me": collections.deque(), "field": collections.deque()}   # deque[(st, [8 floats])]
        self._perf_alarm = {}        # area index -> baseline while alarmed (re-arms on recovery)
        self._perf_pending = {}      # area index -> {"t0", "base", "cur", "drop"} awaiting confirmation
        self._ability_at = None      # session_time an ability of my car was last seen active
        self._ability_active = False
        # tires
        self._tires_armed = True
        self._stint_id = 0
        self._pit_no = 0             # pit exits this session (car_damaged is superseded only by a pit stop)
        self._stint = None
        self._lap_times = collections.deque(maxlen=5)
        self._last_boundary = None   # (lap, session_time) of the last lap boundary seen, any stint
        self._last_eval = None
        self._ended = False          # session_ended already queued for this session

    def new_session(self, uid):
        """A new session_uid. The queue and the cursors are kept (old entries are reported as old-session); a
        session that never reported its end gets a synthetic session_ended."""
        if self.session_uid is not None and not self._ended:
            self._ended = True
            self._append("session_ended", self.clock() if self._last_feed_wall is None else self._last_feed_wall,
                         {"code": None, "position": None, "reason": "new_session_started",
                          "detail": "A new session started before this one reported its end (SEND / FINI)."})
        self.sessions += 1
        self._reset_state(uid)

    def set_decision_events(self, on):
        self.decision_events = on

    # -------------------------------------------------------------------------------------------- feeding

    def feed(self, packet_id, header, body, now=None):
        """Feed one decoded packet (header and body dicts from :mod:`protocol`)."""
        now = self.clock() if now is None else now
        if header.get("session_uid") != self.session_uid:
            self._last_feed_wall = now
            self.new_session(header.get("session_uid"))
        self._pkt_no += 1
        self._last_feed_wall = now
        st = header.get("session_time", self.session_time)
        if st != self.session_time or self._st_changed_wall is None:
            self._st_changed_wall = now
        self.session_time = st
        self.frame = header.get("frame", self.frame)
        seat_car = header.get("seat_car") or [None]
        if seat_car[0] is not None:
            self.my_car = seat_car[0]
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

    def paused(self, now=None):
        """Packets arriving while session_time has not advanced for PAUSE_AFTER_S (wall)."""
        now = self.clock() if now is None else now
        if self._last_feed_wall is None or self._st_changed_wall is None or self.session_time is None:
            return False
        if now - self._last_feed_wall >= STOPPED_AFTER_S:
            return False
        return now - self._st_changed_wall >= PAUSE_AFTER_S

    def _player_fresh(self, now):
        return self.player_at is not None and now - self.player_at < PLAYER_FRESH_S

    @staticmethod
    def _own_car(header, cars):
        car = (header.get("seat_car") or [None])[0]
        if car is None or not 0 <= car < len(cars):
            return None
        return car

    def _feed_player(self, header, body, now):
        if body.get("seat") != 0:
            return
        self.player_at = now
        self.my_car = body.get("car", self.my_car)
        self.can_pit = body["can_pit"]
        self.compound_picks_left = body["compound_picks_left"]
        self.points = body["points"]
        self.hand = body["hand"]
        if body.get("strategy"):
            self.reference_lap_s = body["strategy"].get("reference_lap_s") or self.reference_lap_s
        self._set_pits(body["in_pits"], body["pit_planned"], now)
        self._set_tires(body["tire_health"], body["tire_compound"], body["puncture"], now, "player")
        total = body.get("total_laps")
        if total is not None:
            self.total_laps = total
        if total and body.get("laps_remaining") is not None:
            self._player_completed = max(0, total - body["laps_remaining"])
            self._set_lap(self._player_completed, now, "player")
        self._update_laps_remaining()
        self._player_cards(body["points"], body["hand"], now)
        self._eval_tires(now)

    def _feed_telemetry(self, header, body, now):
        car = self._own_car(header, body["cars"])
        if car is None:
            return
        c = body["cars"][car]
        self._track_moving(c.get("speed_kmh"), now)
        if self._player_fresh(now):
            return
        self._set_tires(c["tire_health"], c["tire_compound"], c["puncture"], now, "car_telemetry")
        self._eval_tires(now)

    def _feed_lap_data(self, header, body, now):
        car = self._own_car(header, body["cars"])
        if car is None:
            return
        c = body["cars"][car]
        self.finished = self.finished or bool(c["finished"])
        if not self._player_fresh(now):
            self._set_pits(c["in_pits"], c["pit_planned"], now)
        self._lapdata_lap = c["lap"]
        self._lapdata_wall = now
        self._set_lap(c["lap"], now, "lap_data")
        self._update_laps_remaining()
        self._eval_tires(now)

    def _feed_session(self, header, body, now):
        self._check_cards(now)
        if self.total_laps is None or not self._player_fresh(now):
            self.total_laps = body["total_laps"]
            self._update_laps_remaining()
        track = body.get("track")
        if track:
            self.lap_time_est_s = track.get("lap_time_est_s") or None
            self.pit_lane_time_loss_s = track.get("pit_lane_time_loss_s")

    def _feed_abilities(self, header, body, now):
        car = self._own_car(header, body["cars"])
        if car is None:
            return
        c = body["cars"][car]
        self.ban_tokens = c["ban_tokens"]
        active = any(c[k] for k in ("speed_boost", "cornering_boost", "super_boost", "party_mode", "drs",
                                    "boost_overspeed", "recharging")) or c["slipstream"] > 0.05
        was = self._ability_active
        self._ability_active = bool(active)
        if active or was:                # an ability that just ended was active until about now
            self._ability_at = self.session_time if self.session_time is not None else now

    def _feed_event(self, header, body, now):
        self.events_seen = True
        code, seat = body.get("code"), body.get("seat")
        if code == "SEND" or (code == "FINI" and seat == 0):
            self.finished = self.finished or code == "FINI"
            self._close_tire("session ended" if code == "SEND" else "finished")
            if self._ended:
                return               # FINI then SEND (or a repeat): one session_ended per session
            self._ended = True
            self._append("session_ended", now, {"code": code, "position": body.get("position")})
            return
        if seat != 0:
            return
        if code == "CARD":
            if body.get("points", 0) > 0 and body.get("hand"):
                self._card_offer(body["points"], body["hand"], now, "event")
        elif code == "TIRE":
            self._tire_open(body, now)
        elif code == "LAPC":
            # Ambiguous meaning (laps completed vs the new lap's number): a timing hint only, never a lap count.
            self.lap_observations.append({"lapc_lap": body.get("lap"), "lap_data_lap": self._lapdata_lap,
                                          "player_laps_completed": self._player_completed,
                                          "session_time_s": _r(self.session_time, 2)})
            if self._tire is not None and self._tire["open"] and self._tire["pre_race"]:
                self._close_tire("race started (LAPC)")
            self._eval_tires(now)
        elif code == "PITO":
            self._close_tire("left the pit lane (PITO)")
            if self.in_pits:
                self._set_pits(False, self.pit_planned, now)
            else:
                self._pit_exit(now)
        elif code == "PITI":
            self._set_pits(True, self.pit_planned, now)

    # -------------------------------------------------------------------------------------------- card_offer

    def _player_cards(self, points, hand, now):
        sig = _cards_sig(hand) if points > 0 and hand else None
        self._player_sig, self._player_points, self._player_no = sig, points, self._pkt_no
        lc = self._last_card
        if lc is not None and not lc["cleared"]:
            if sig == lc["sig"]:
                lc["confirmed_by_player"] = True
                lc["_clear_since"] = None
                lc["points"] = points
            elif lc["_clear_since"] is None:
                lc["_clear_since"] = now
        if sig is None or (lc is not None and not lc["cleared"] and lc["sig"] == sig):
            self._card_candidate = None
        elif self._card_candidate is None or self._card_candidate[0] != sig:
            self._card_candidate = (sig, now, points, hand)
        self._check_cards(now)

    def _check_cards(self, now):
        """Debounced clearing of the open hand and reporting of a hand seen only in the Player packet (wall clock).

        Also called from Session / Car Status: the game stops time and the Player packet while a hand waits for
        the player, so the debounces must be able to complete without another Player packet."""
        lc = self._last_card
        if lc is not None and not lc["cleared"] and lc["_clear_since"] is not None:
            if (now - lc["_clear_since"] >= self.config.card_debounce_s
                    and (lc["confirmed_by_player"] or now - lc["fired_wall"] >= CARD_EVENT_GRACE_S)):
                lc["cleared"] = True
                lc["closed_because"] = ("Player shows no hand" if self._player_sig is None
                                        else "Player shows a different hand")
        cand = self._card_candidate
        if cand is None:
            return
        sig, first, points, hand = cand
        if lc is not None and not lc["cleared"] and lc["sig"] == sig:
            self._card_candidate = None
            return
        fallback = not self.events_seen and self.decision_events is not True
        wait = self.config.card_debounce_s if fallback else CARD_EVENT_GRACE_S
        if now - first >= wait:
            self._card_candidate = None
            self._card_offer(points, hand, now, "player" if fallback else "player_catch_up")

    def _card_offer(self, points, hand, now, source):
        sig = _cards_sig(hand)
        last = self._last_card
        if last is not None and not last["cleared"] and last["sig"] == sig:
            if source != "event":
                return                    # the Player packet still shows the hand already reported
            if not last["answered"] and last["source"] == "event" and last["points"] == points:
                return                    # repeated CARD (after a ban): same cards, same points
            if not last["answered"] and last["source"] != "event":
                return                    # late CARD for the hand the Player packet already reported
        if last is not None and not last["cleared"]:
            last["cleared"] = True        # replaced by another hand
            last["closed_because"] = "a new hand was offered"
        seq = None
        if "card_offer" in self.config.enabled:
            trig = self._fire("card_offer", now, {
                "source": {"event": "CARD event", "player": "Player packet (Decision Events off)",
                           "player_catch_up": "Player packet (a hand no CARD event announced)"}[source],
                "points": points, "hand": hand, "ban_tokens": self.ban_tokens,
                "action": "pick_card(index, seq) spends a point on a card; ban_card(index) spends a ban token and "
                          "keeps your turn (not on the last card left to pick)."})
            seq = trig["seq"]
        confirmed = source != "event" or (self._player_sig == sig and self._player_no is not None)
        self._last_card = {"sig": sig, "points": points, "hand": hand, "seq": seq, "source": source,
                           "fired_wall": now, "fired_st": self.session_time, "fired_frame": self.frame,
                           "fired_no": self._pkt_no, "confirmed_by_player": source != "event" and confirmed,
                           "answered": False, "cleared": False, "closed_because": None, "_clear_since": None}

    def card_state(self, now=None):
        """The current hand: ``open`` True (waiting for a pick), False (no hand / answered / closed) or None
        (unknown: the game resumed and no Player packet confirms or denies the hand)."""
        now = self.clock() if now is None else now
        lc = self._last_card
        out = {"type": "card_offer", "open": False, "points": None, "hand": None, "seq": None, "sig": None,
               "in_pits": self.in_pits, "source": None, "closed_because": None, "ban_tokens": self.ban_tokens}
        if lc is None:
            if self._player_sig is not None:
                out.update(open=None, points=self._player_points, hand=self.hand, sig=_sig_str(self._player_sig),
                           source="Player packet (not reported yet)")
            else:
                out["closed_because"] = "no hand seen"
            return out
        out.update(points=lc["points"], hand=lc["hand"], seq=lc["seq"], sig=_sig_str(lc["sig"]),
                   source=lc["source"])
        if lc["answered"]:
            out.update(open=False, closed_because="answered (pick_card)")
        elif lc["cleared"]:
            out.update(open=False, closed_because=lc["closed_because"])
        elif self._player_no is not None and self._player_no > lc["fired_no"]:
            out["open"] = True                     # a Player sample since the offer; clearing is debounced
            if self._player_sig is not None and self._player_sig != lc["sig"]:
                out["player_shows_other_hand"] = True
        elif (self.paused(now) or lc["fired_st"] is None or self.session_time is None
              or self.session_time - lc["fired_st"] < CARD_RESUME_ST):
            out["open"] = True
        else:
            out.update(open=None, closed_because="game resumed and no Player packet confirms the hand "
                                                 "(Player Data off?)")
        if out["open"] is False and self._player_sig is not None and self._player_sig != lc["sig"]:
            out.update(open=None, points=self._player_points, hand=self.hand, seq=None,
                       sig=_sig_str(self._player_sig), source="Player packet (not reported yet)", closed_because=None)
        return out

    def answer_card(self, result):
        """pick_card result: the hand is answered on ok or no_points, not on cards_unavailable (L5)."""
        if not isinstance(result, dict):
            return
        if result.get("ok") or result.get("error_reason") == "no_points":
            if self._last_card is not None:
                self._last_card["answered"] = True

    def hand_sig_for_seq(self, seq):
        for t in self.triggers:
            if t["seq"] == seq and t["type"] == "card_offer":
                return _sig_str(_cards_sig(t["hand"]))
        return None

    # -------------------------------------------------------------------------------------------- tire screen

    def _tire_open(self, body, now):
        seq = None
        if "tire_screen" in self.config.enabled:
            seq = self._fire("tire_screen", now, {
                "pre_race": body.get("pre_race"), "current": body.get("current"),
                "allowed": body.get("allowed"), "picks_left": body.get("picks_left"),
                "action": "pick_tire(compound) with one of 'allowed' (and picks left), while the screen is open."
            })["seq"]
        self._tire = {"open": True, "pre_race": body.get("pre_race"), "current": body.get("current"),
                      "allowed": body.get("allowed"), "picks_left": body.get("picks_left"), "seq": seq,
                      "closed_because": None}
        self._moving_since_st = None

    def _close_tire(self, why):
        if self._tire is not None and self._tire["open"]:
            self._tire["open"] = False
            self._tire["closed_because"] = why

    def _track_moving(self, speed, now):
        t = self._tire
        if t is None or not t["open"] or not t["pre_race"] or speed is None or self.session_time is None:
            self._moving_since_st = None
            return
        if speed > TIRE_MOVING_KMH:
            if self._moving_since_st is None:
                self._moving_since_st = self.session_time
            elif self.session_time - self._moving_since_st >= TIRE_MOVING_ST:
                self._close_tire("car is moving (race started)")
        else:
            self._moving_since_st = None

    def tire_state(self, now=None):
        t = self._tire
        if t is None:
            return {"type": "tire_screen", "open": False, "pre_race": None, "current": None, "allowed": None,
                    "picks_left": None, "seq": None, "closed_because": "no tire screen seen"}
        out = {"type": "tire_screen"}
        out.update(t)
        return out

    def answer_tire(self, result):
        if isinstance(result, dict) and result.get("ok"):
            self._close_tire("answered (pick_tire)")

    def open_decisions(self, now=None):
        out = []
        card = self.card_state(now)
        if card["open"] is not False:
            out.append(card)
        tire = self.tire_state(now)
        if tire["open"]:
            out.append(tire)
        return out

    def paused_hint(self, paused, frozen_for_s):
        if not paused or (frozen_for_s or 0) <= PAUSE_HINT_S:
            return None
        if self.card_state()["open"] is True or self._player_sig is not None:
            return None
        return ("the game is paused but this server has not seen the hand (restart during a pause?) - ask the "
                "user to pick on screen or check the game")

    # -------------------------------------------------------------------------------------------- damage

    def _set_tires(self, health, compound, puncture, now, source):
        prev_compound = self.compound
        self.health_raw = health
        self.health_st = self.session_time
        self.compound = compound
        if self._stint is None:
            self._health_win.clear()
            self._health_win.append(health)
            self.health = health
            self._new_stint(now, "first sample")
        elif prev_compound is not None and compound != prev_compound:
            self._health_win.clear()
            self._health_win.append(health)
            self.health = health
            self._new_stint(now, "compound changed")
        elif not self._health_win:
            # first sample on the set after a pit exit: it is the set's start
            self._health_win.append(health)
            self.health = health
            if not self._stint["boundaries"]:
                self._stint["start_health"], self._stint["start_t"] = health, self.session_time
        elif self.health is not None and health >= self.health + HEALTH_JUMP:
            # a large rise: a new set only if it is sustained (noise never starts a stint)
            t = self.session_time if self.session_time is not None else now
            if self._jump is None:
                self._jump = (t, [])
            self._jump[1].append(health)
            if len(self._jump[1]) >= HEALTH_JUMP_SAMPLES and t - self._jump[0] >= HEALTH_JUMP_ST:
                t0, samples = self._jump
                self._jump = None
                self._health_win.clear()
                self._health_win.append(health)
                self.health = health
                self._new_stint(now, "tire health rose")
                # the set started at the first sample of the rise, not at its confirmation
                self._stint["start_health"], self._stint["start_t"] = samples[0], t0
        else:
            self._jump = None
            self._health_win.append(health)
            self.health = statistics.median(self._health_win)
        self._puncture(puncture, health, compound, now, source)
        st = self._stint
        if st is not None and st["boundaries"] and st["boundaries"][-1][1] is None and self.session_time is not None:
            # the lap boundary came without a fresh health sample: this first sample after it is the baseline
            lap, _, _, _ = st["boundaries"][-1]
            st["boundaries"][-1] = (lap, self.health, self.session_time, False)

    def _puncture(self, puncture, health, compound, now, source):
        st = self.session_time
        if puncture:
            if self._punct_source != source:
                self._punct_seen_count = 0
            self._punct_source = source
            self._punct_seen_count += 1
            self._punct_clear_since_st = None
            if self._punct_seen_count >= PUNCTURE_CONFIRM and self._puncture_armed:
                self._puncture_armed = False
                if "car_damaged" in self.config.enabled:
                    self._fire("car_damaged", now, {
                        "signal": "puncture", "heuristic": False, "source": source,
                        "tire_health": _r(health), "compound": compound,
                        "detail": "The puncture flag (tire_flags bit 0) of your car is set (two consecutive "
                                  "samples)."})
        else:
            self._punct_seen_count = 0
            self._punct_source = source
            if self._punct_clear_since_st is None:
                self._punct_clear_since_st = st
            if (not self._puncture_armed and st is not None and self._punct_clear_since_st is not None
                    and st - self._punct_clear_since_st >= PUNCTURE_REARM_ST):
                self._puncture_armed = True
        self.puncture = puncture

    def _feed_status(self, header, body, now):
        self._check_cards(now)
        cfg = self.config
        cars = body["cars"]
        window = cfg.perf_window_s
        car = self._own_car(header, cars)
        if car is None:
            return
        # Session time, not wall-clock: the game stops time while a card hand waits, and Car Status keeps coming.
        t = self.session_time if self.session_time is not None else now
        others = [c for i, c in enumerate(cars) if i != car]
        rows = {"me": [cars[car]["performance"][a] for a in P.PERFORMANCE_AREAS]}
        if others:
            rows["field"] = [statistics.median(c["performance"][a] for c in others) for a in P.PERFORMANCE_AREAS]
        keep = window + PERF_CONFIRM_ST + 1.0
        for key, values in rows.items():
            hist = self._perf_hist[key]
            if hist and hist[-1][0] == t:
                hist[-1] = (t, values)           # paused: same instant again
            else:
                hist.append((t, values))
            while hist and t - hist[0][0] > keep:
                hist.popleft()
        if cfg.perf_drop <= 0 or "car_damaged" not in cfg.enabled:
            return
        mine = self._perf_hist["me"]
        if len(mine) < 2:
            return
        current = mine[-1][1]
        thr = cfg.perf_drop
        ability = self._ability_active or (self._ability_at is not None and t - self._ability_at <= window
                                           + ABILITY_GRACE_S)
        confirmed = []
        for a, area in enumerate(P.PERFORMANCE_AREAS):
            cur = current[a]
            if a in self._perf_alarm:
                if cur >= self._perf_alarm[a] * (1 - thr / 2):
                    del self._perf_alarm[a]
                continue
            pend = self._perf_pending.get(a)
            if pend is not None:
                if ability or self.in_pits or cur > pend["base"] * (1 - thr):
                    del self._perf_pending[a]     # recovered, ability, or pit lane: not a failure
                    continue
                if self._field_dropped(a, pend["t0"] - window, t, thr / 2):
                    del self._perf_pending[a]     # the field dropped too: weather
                    continue
                if t - pend["t0"] >= PERF_CONFIRM_ST:
                    del self._perf_pending[a]
                    confirmed.append((a, area, pend["base"], cur))
                continue
            base = self._base(mine, a, t - window)
            if base is None or base <= 0:
                continue
            drop = (base - cur) / base
            if drop < thr or ability or self.in_pits:
                continue
            if self._field_dropped(a, t - window, t, thr / 2):
                continue
            self._perf_pending[a] = {"t0": t, "base": base}
        if not confirmed:
            return
        for a, _, base, _ in confirmed:
            self._perf_alarm[a] = base
        areas = [{"area": area, "from": _r(base), "to": _r(cur), "drop_fraction": _r((base - cur) / base)}
                 for _, area, base, cur in confirmed]
        first = areas[0]
        self._fire("car_damaged", now, {
            "signal": "performance_drop", "heuristic": True, "area": first["area"], "areas": areas,
            "from": first["from"], "to": first["to"], "drop_fraction": first["drop_fraction"],
            "window_s": window, "confirmed_after_s": PERF_CONFIRM_ST, "other_cars_compared": len(others),
            "detail": "Possible failure: these performance areas fell by more than the threshold within the window "
                      "and stayed down for %.1f s of session time, while the field's median of the same areas did "
                      "not fall and no ability of your car was active. The game reports no failure flag; this is a "
                      "heuristic (an expiring temporary upgrade could look the same)." % PERF_CONFIRM_ST})

    @staticmethod
    def _base(hist, area, since):
        vals = [v[area] for t, v in list(hist)[:-1] if t >= since]
        return max(vals) if vals else None

    def _field_dropped(self, area, since, until, limit):
        hist = [(t, v[area]) for t, v in self._perf_hist["field"] if since <= t <= until]
        if len(hist) < 2:
            return False
        base = hist[0][1]
        peak = base
        for _, v in hist[1:]:
            if peak > 0 and (peak - v) / peak >= limit:
                return True
            peak = max(peak, v)
        return False

    # -------------------------------------------------------------------------------------------- pits / laps

    def _set_pits(self, in_pits, pit_planned, now):
        was = self.in_pits
        self.in_pits = in_pits
        self.pit_planned = pit_planned
        if in_pits and self._stint is not None:
            self._stint["pitted"] = True
        if was and not in_pits:
            self._close_tire("left the pit lane")
            self._pit_exit(now)

    def _pit_exit(self, now):
        self._pit_no += 1
        self._jump = None
        self._health_win.clear()             # the next sample starts the new set's smoothing
        self._new_stint(now, "pit exit")
        self._puncture_armed = True
        self._punct_seen_count = 0
        self.puncture = None         # a puncture still showing after the stop is reported again
        self._perf_alarm.clear()
        self._perf_pending.clear()

    def _update_laps_remaining(self):
        if self.total_laps and self.lap is not None:
            self.laps_remaining = max(0, self.total_laps - self.lap)
        else:
            self.laps_remaining = None

    def _set_lap(self, lap, now, source):
        """A laps-completed candidate (Lap Data or Player total - remaining); the count is monotone."""
        if lap is None:
            return
        if self.lap is None:
            self.lap = lap           # first sight (perhaps mid-lap): not a boundary
            self.lap_source = source
            self._update_laps_remaining()
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
        self.lap_source = source
        self._update_laps_remaining()       # before any _eval_tires (G1)
        self._last_boundary = (lap, t)
        st = self._stint
        if st is not None and t is not None:
            # A health sample older than HEALTH_FRESH_ST session seconds (Player / Car Telemetry missing at the
            # boundary) is not this boundary's health: the first sample after it becomes the baseline instead.
            fresh = (self.health is not None and self.health_st is not None
                     and abs(t - self.health_st) <= HEALTH_FRESH_ST)
            if st["boundaries"] and fresh:
                plap, ph, _, exact = st["boundaries"][-1]
                dlap = lap - plap
                if dlap >= 1 and not st["pitted"] and exact and ph is not None:
                    sample = (ph - self.health) / dlap
                    if sample >= 0:
                        st["samples"].append(sample)
                        del st["samples"][:-MAX_SAMPLES]
            st["boundaries"].append((lap, self.health if fresh else None, t, fresh))
            del st["boundaries"][:-MAX_SAMPLES - 1]
            st["pitted"] = False

    def laps(self, now=None):
        now = self.clock() if now is None else now
        return {"laps_completed": self.lap, "total_laps": self.total_laps, "laps_remaining": self.laps_remaining,
                "lap_source": self.lap_source,
                "lap_data_age_s": None if self._lapdata_wall is None else round(now - self._lapdata_wall, 2),
                "lap_observations": list(self.lap_observations)}

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
        n = len(st["samples"])
        measured = statistics.median(st["samples"]) if n else None
        extrapolated, how = self._extrapolate(st)
        if measured is not None:
            # Wear is not linear (it can accelerate as the tire wears), so a current lap wearing faster than the
            # measured laps wins; a slower current lap never lowers the measured rate (bursty wear, L6).
            if extrapolated is not None and extrapolated > measured:
                return extrapolated, "%s; higher than the median of the last %d full lap(s) (%.4f)" \
                    % (how, n, measured), n
            return measured, "median of the last %d full lap(s) on this set" % n, n
        if extrapolated is not None:
            return extrapolated, how, 0
        return None, "not enough data yet (needs a full lap, or %d%% of one with a known lap time)" \
            % round(self.config.min_lap_share * 100), 0

    def _extrapolate(self, st):
        """Wear per lap from this lap so far: health at the lap's start (or the first fresh sample after it) minus
        health now, over the share of a lap of session time since then. Session time stops while the game is
        paused, so a pause does not dilute the rate."""
        lap_s, src = self.lap_time()
        if st["boundaries"]:
            _, ref_h, ref_t, _ = st["boundaries"][-1]
        else:
            ref_h, ref_t = st["start_health"], st["start_t"]
        t = self.session_time
        if lap_s and ref_h is not None and ref_t is not None and t is not None and t > ref_t:
            share = (t - ref_t) / lap_s
            if share >= self.config.min_lap_share:
                wear = max(0.0, ref_h - self.health) / share
                return wear, "extrapolated from %.0f%% of a lap of session time (lap time: %s)" % (share * 100, src)
        return None, None

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
        if self.total_laps and self.laps_remaining is not None:
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
        floor = predicted is None and self.health <= cfg.tire_floor
        bad = floor or (predicted is not None and predicted <= cfg.tire_margin)
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
            "reason": "floor_no_forecast" if floor else "prediction",
            "tire_margin": cfg.tire_margin, "tire_floor": cfg.tire_floor,
            "laps_remaining": self.laps_remaining, "laps_completed": self.lap, "total_laps": self.total_laps,
            "can_pit": self.can_pit, "pit_planned": self.pit_planned,
            "compound": self.compound, "compound_picks_left": self.compound_picks_left,
            "pit_lane_time_loss_s": _r(self.pit_lane_time_loss_s, 2),
            "lap_time_s": _r(lap_s, 2), "lap_time_source": lap_src,
            "heuristic": True,
            "action": "Decide whether to call a stop with set_pit(true) (needs can_pit); the tire screen then "
                      "arrives as a tire_screen trigger.",
        })
        self._fire("tires_wont_last", now, payload)

    # -------------------------------------------------------------------------------------------- queue

    def _append(self, kind, now, payload):
        self.seq += 1
        trig = {"seq": self.seq, "type": kind, "session_uid": self.session_uid,
                "session_time_s": _r(self.session_time, 2), "lap": self.lap, "frame": self.frame, "_at": now,
                "_stint": self._stint_id, "_pit": self._pit_no, "_session_no": self.sessions, "returns": 0, "returned_at": None}
        trig.update(payload)
        while len(self.triggers) >= self.maxlen:
            old = self.triggers.popleft()
            self._evicted.append((old["seq"], old["returns"]))
        self.triggers.append(trig)
        for listener in list(self.listeners):
            try:
                listener(trig)
            except Exception:  # a listener must never break telemetry processing
                pass
        self._wake()
        return trig

    def _fire(self, kind, now, payload):
        payload = dict(payload)
        if kind == "car_damaged":
            payload.update(can_pit=self.can_pit, pit_planned=self.pit_planned, in_pits=self.in_pits,
                           laps_remaining=self.laps_remaining)
        elif kind == "card_offer":
            payload.update(in_pits=self.in_pits)
        return self._append(kind, now, payload)

    def _wake(self):
        for waiter in list(self._waiters):
            if not waiter.done():
                waiter.set_result(None)
        self._waiters.clear()

    def _old_session(self, e):
        return e["_session_no"] != self.sessions

    def _superseded(self, e):
        kind = e["type"]
        if kind == "session_ended":
            return False
        if kind in ("card_offer", "tire_screen", "tires_wont_last"):
            if any(t["seq"] > e["seq"] and t["type"] == kind for t in self.triggers):
                return True
        if kind == "tires_wont_last" and e["_stint"] != self._stint_id:
            return True
        if kind == "car_damaged" and e["_pit"] != self._pit_no:
            return True
        return False

    def _open(self, e, now=None):
        if self._old_session(e):
            return False if e["type"] in DECISION_TYPES else None
        if e["type"] == "card_offer":
            cs = self.card_state(now)
            return cs["open"] if cs["seq"] == e["seq"] else False
        if e["type"] == "tire_screen":
            ts = self._tire
            return bool(ts and ts["seq"] == e["seq"] and ts["open"])
        return None

    def _dead(self, e, now):
        """(dead, why) — why in ("superseded", "old_session")."""
        if self._old_session(e) and e["type"] != "session_ended":
            return True, "old_session"
        if self._superseded(e):
            return True, "superseded"
        if e["type"] in DECISION_TYPES and self._open(e, now) is False:
            return True, "superseded"
        return False, None

    def _cap(self, e, now):
        if e["type"] in DECISION_TYPES:
            return MAX_RESEND
        return 1 if self._since_mode else 0

    def public(self, trig, now=None):
        now = self.clock() if now is None else now
        out = {k: v for k, v in trig.items()
               if not k.startswith("_") and k not in ("session_uid", "returns", "returned_at")}
        out["age_s"] = round(now - trig["_at"], 2)
        old = self._old_session(trig)
        out["superseded"] = bool(old and trig["type"] != "session_ended") or self._superseded(trig) \
            or (trig["type"] in DECISION_TYPES and self._open(trig, now) is False)
        out["open"] = self._open(trig, now)
        if old:
            out["stale_session"] = True
        if trig["type"] == "car_damaged" and trig["_pit"] != self._pit_no:
            out["after_pit_stop"] = True
        if trig["type"] == "session_ended":
            out["session_uid"] = trig["session_uid"]
        return out

    def collect(self, since_seq=None, now=None):
        """Apply the acknowledgement (``since_seq``) and return the entries due now (see module docstring)."""
        now = self.clock() if now is None else now
        notes = []
        if not self._collected:
            self._collected = True
            if since_seq:
                notes.append("since_seq %d ignored: first call since this server started (it may have restarted); "
                             "everything still queued is sent" % since_seq)
            if since_seq is not None:
                self._since_mode = True
        elif since_seq is not None:
            self._since_mode = True
            if since_seq > self.seq:
                notes.append("since_seq %d is ahead of the newest trigger (%d): the server restarted; "
                             "acknowledgement reset" % (since_seq, self.seq))
                self.acked = 0
            else:
                self.acked = max(self.acked, since_seq)
        evicted = [s for s, returns in self._evicted if s > self.acked and (returns == 0 or self._since_mode)]
        if evicted:
            notes.append("%d triggers evicted before they were acknowledged (the queue keeps the newest %d)"
                         % (len(evicted), self.maxlen))
            self.acked = max(self.acked, max(evicted))
        self._evicted.clear()
        skipped = {"superseded": 0, "old_session": 0}
        classes = []                      # (entry, kind) kind in due/dead/exhausted/pending
        for e in self.triggers:
            if e["seq"] <= self.acked:
                continue
            dead, why = self._dead(e, now)
            if dead:
                skipped[why] += 1
                classes.append((e, "dead"))
                continue
            if e["returns"] == 0:
                classes.append((e, "due"))
            elif e["returns"] > self._cap(e, now):
                classes.append((e, "exhausted"))
            elif now - e["returned_at"] >= self.remind_s:
                classes.append((e, "due"))
            else:
                classes.append((e, "pending"))
        # last_seq only advances over a contiguous run of entries that are returned now, dead or exhausted: it
        # never covers an entry that was returned earlier and still awaits its resend (that result may have been
        # lost), so acknowledging last_seq never drops an entry the model has not seen.
        entries = []
        last_seq = self.acked
        blocked = False
        for e, k in classes:
            if k == "pending":
                blocked = True
                continue
            if k == "due":
                resent = e["returns"] > 0
                e["returns"] += 1
                e["returned_at"] = now
                row = self.public(e, now)
                row["resent"] = resent
                entries.append(row)
            if not blocked:
                last_seq = max(last_seq, e["seq"])
        if entries:
            status = "session_ended" if entries[-1]["type"] == "session_ended" else "triggered"
        else:
            status = None
        return {"entries": entries, "last_seq": last_seq, "acked": self.acked, "skipped": skipped,
                "status": status, "notes": notes}

    def next_due_in(self, now=None):
        """Seconds until the earliest resend becomes due (0 if something is due now), or None."""
        now = self.clock() if now is None else now
        best = None
        for e in self.triggers:
            if e["seq"] <= self.acked or self._dead(e, now)[0]:
                continue
            if e["returns"] == 0:
                return 0.0
            if e["returns"] > self._cap(e, now):
                continue
            wait = max(0.0, e["returned_at"] + self.remind_s - now)
            best = wait if best is None else min(best, wait)
        return best

    def undelivered(self):
        """Entries never returned by collect() and still alive."""
        now = self.clock()
        return sum(1 for e in self.triggers if e["returns"] == 0 and e["seq"] > self.acked
                   and not self._dead(e, now)[0])

    def after(self, since_seq=0, limit=None):
        found = [t for t in self.triggers if t["seq"] > since_seq]
        if limit is not None and len(found) > limit:
            found = found[-limit:]
        return found

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
        card = self.card_state()
        return {
            "session_uid": self.session_uid, "my_car": self.my_car,
            "decision_events": self.decision_events, "events_seen_this_session": self.events_seen,
            "card_offer_source": "CARD event" if (self.events_seen or self.decision_events)
            else "Player packet fallback",
            "card": {k: card[k] for k in ("open", "seq", "points", "source", "closed_because")},
            "tire_screen": {k: v for k, v in self.tire_state().items() if k in ("open", "seq", "pre_race",
                                                                                 "closed_because")},
            "puncture": self.puncture, "puncture_armed": self._puncture_armed,
            "performance_areas_alarmed": [P.PERFORMANCE_AREAS[a] for a in sorted(self._perf_alarm)],
            "performance_areas_pending": [P.PERFORMANCE_AREAS[a] for a in sorted(self._perf_pending)],
            "laps": self.laps(),
            "lap_observations": list(self.lap_observations),
            "lapc_lap_meaning": "unknown (recorded in laps.lap_observations; not used as a lap count)",
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
            "queued": len(self.triggers), "last_seq": self.seq, "acked_up_to_seq": self.acked,
            "undelivered": self.undelivered(),
        }
