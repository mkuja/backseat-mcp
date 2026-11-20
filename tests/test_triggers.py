"""Trigger engine with synthetic packet sequences (decoded dicts, synthetic clock)."""
import asyncio

import pytest

from backseat_champions_mcp import protocol as P
from backseat_champions_mcp.triggers import TriggerConfig, TriggerEngine

AREAS = P.PERFORMANCE_AREAS


def card(upgrade, facility="Speed", tier=0, banned=False, index=0):
    return {"index": index, "upgrade": upgrade, "facility": facility, "tier": tier, "banned": banned}


def hand(*upgrades, banned=()):
    return [card(u, index=i, banned=i in banned) for i, u in enumerate(upgrades)]


class Sim:
    """Builds decoded packets for one session with my car at index 3 of 6.

    ``self.lap`` is laps COMPLETED; the packets carry the lap semantics observed in game 0.88.2: Lap Data ``lap`` =
    the lap being driven (completed + 1) and Player ``laps_remaining`` = laps after the current one."""

    def __init__(self, enabled=("card_offer", "car_damaged", "tires_wont_last"), **cfg):
        self.t = 100.0
        self.st = 0.0
        self.frame = 0
        self.uid = 1
        self.me = 3
        self.n = 6
        self.engine = TriggerEngine(TriggerConfig(enabled=enabled, **cfg), clock=lambda: self.t)
        self.lap = 0
        self.health = 1.0
        self.compound = "Standard"
        self.puncture = False
        self.in_pits = False
        self.pit_planned = False
        self.total_laps = 10
        self.points = 0
        self.hand = []
        self.perf = [[1.0] * 8 for _ in range(self.n)]
        self.speed = 100.0

    def header(self):
        self.frame += 1
        return {"session_uid": self.uid, "seat_car": [self.me, None, None, None], "session_time": self.st,
                "frame": self.frame}

    def advance(self, dt, paused=False):
        self.t += dt
        if not paused:
            self.st += dt

    def session(self, total_laps=None):
        self.engine.feed(P.PKT_SESSION, self.header(), {"session_type": "Race", "energy": "boost", "track": None,
                                                         "total_laps": self.total_laps if total_laps is None
                                                         else total_laps})

    def player(self, **kw):
        body = {"seat": 0, "car": self.me, "points": self.points, "hand": self.hand, "upgrades_applied": 0,
                "total_laps": self.total_laps, "laps_remaining": max(0, self.total_laps - self.lap - 1),
                "pit_planned": self.pit_planned, "can_pit": True, "in_pits": self.in_pits, "pit_stops": 0,
                "tire_compound": self.compound, "puncture": self.puncture, "tire_health": self.health,
                "compound_picks_left": {c: None for c in P.COMPOUNDS}, "energy": 1.0, "strategy": None}
        body.update(kw)
        self.engine.feed(P.PKT_PLAYER, self.header(), body)

    def lap_data(self):
        cars = [{"position": i + 1, "class_position": i + 1, "lap": self.lap + 1 if i == self.me else 1,
                 "in_pits": self.in_pits if i == self.me else False, "finished": False,
                 "pit_planned": self.pit_planned if i == self.me else False, "api_driving": False, "pit_stops": 0,
                 "gap_to_leader": 0.0, "interval_ahead": 0.0, "interval_behind": 0.0} for i in range(self.n)]
        self.engine.feed(P.PKT_LAP_DATA, self.header(), {"cars": cars})

    def telemetry(self, puncture=None, health=None):
        cars = [{"speed_kmh": self.speed, "pedal": 1.0, "steer": 0.0, "wheels": [], "tire_compound": self.compound,
                 "puncture": (self.puncture if puncture is None else puncture) if i == self.me else False,
                 "tire_health": (self.health if health is None else health) if i == self.me else 1.0,
                 "energy": 1.0} for i in range(self.n)]
        self.engine.feed(P.PKT_CAR_TELEMETRY, self.header(), {"cars": cars})

    def status(self):
        cars = [{"performance": dict(zip(AREAS, self.perf[i]))} for i in range(self.n)]
        self.engine.feed(P.PKT_CAR_STATUS, self.header(), {"cars": cars})

    def abilities(self, **active):
        cars = []
        for i in range(self.n):
            c = {k: False for k in ("speed_boost", "cornering_boost", "super_boost", "party_mode", "drs",
                                    "boost_overspeed", "recharging", "super_boost_unlocked", "party_mode_unlocked",
                                    "drs_unlocked", "api_boosting")}
            c.update(ban_tokens=2, slipstream=0.0, recharge_left=0.0)
            if i == self.me:
                c.update(active)
            cars.append(c)
        self.engine.feed(P.PKT_CAR_ABILITIES, self.header(), {"cars": cars})

    def event(self, code, seat=0, **details):
        body = {"code": code, "seat": seat}
        body.update(details)
        self.engine.feed(P.PKT_EVENT, self.header(), body)

    def fired(self, kind=None):
        return [t for t in self.engine.triggers if kind is None or t["type"] == kind]


# ------------------------------------------------------------------------------------------------ card_offer

def test_one_trigger_per_hand_with_both_sources():
    s = Sim()
    s.engine.set_decision_events(True)
    s.points, s.hand = 3, hand("EnginePower", "Downforce", "TireGrip")
    s.player()
    s.event("CARD", points=3, hand=s.hand)
    for _ in range(20):
        s.advance(0.1)
        s.player()
    assert len(s.fired("card_offer")) == 1
    t = s.fired("card_offer")[0]
    assert t["points"] == 3 and len(t["hand"]) == 3 and t["source"] == "CARD event"
    # a ban keeps the hand: a repeated CARD with the same cards and points is not a new hand
    s.hand = hand("EnginePower", "Downforce", "TireGrip", banned=(1,))
    s.player()
    s.event("CARD", points=3, hand=s.hand)
    assert len(s.fired("card_offer")) == 1
    # a pick: one point less and a new hand
    s.points, s.hand = 2, hand("Gearbox", "Cooling", "Reroll")
    s.event("CARD", points=2, hand=s.hand)
    s.player()
    assert len(s.fired("card_offer")) == 2
    # the same cards dealt again after another pick (points differ) are still a new hand
    s.points = 1
    s.event("CARD", points=1, hand=s.hand)
    assert len(s.fired("card_offer")) == 3


def test_player_fallback_when_decision_events_off():
    s = Sim()
    s.engine.set_decision_events(False)
    s.points, s.hand = 2, hand("EnginePower", "Downforce")
    s.player()
    assert not s.fired()                      # debounce
    s.advance(0.3)
    s.player()
    assert len(s.fired("card_offer")) == 1 and "Player packet" in s.fired()[0]["source"]
    for _ in range(10):
        s.advance(0.1)
        s.player()
    assert len(s.fired("card_offer")) == 1
    # a CARD event for the same hand (the switch was on after all) is not a second trigger
    s.event("CARD", points=2, hand=s.hand)
    assert len(s.fired("card_offer")) == 1
    # a stale hand flashing with fewer points after a pick does not fire before the new hand is stable
    s.engine.events_seen = False
    s.points = 1
    s.player()
    s.hand = hand("Gearbox", "Cooling")
    s.player()
    s.advance(0.1)
    s.player()
    assert len(s.fired("card_offer")) == 1
    s.advance(0.3)
    s.player()
    assert len(s.fired("card_offer")) == 2


def test_catch_up_when_decision_events_on():
    s = Sim()
    s.engine.set_decision_events(True)
    s.points, s.hand = 2, hand("EnginePower")
    for _ in range(15):
        s.advance(0.1)
        s.player()
    assert not s.fired()                      # a CARD event is expected first
    for _ in range(10):
        s.advance(0.1)
        s.player()
    fired = s.fired("card_offer")
    assert len(fired) == 1 and "no CARD event" in fired[0]["source"]
    s.event("CARD", points=2, hand=s.hand)    # late event for the same hand
    assert len(s.fired("card_offer")) == 1


def test_card_offer_disabled():
    s = Sim(enabled=("tires_wont_last",))
    s.event("CARD", points=2, hand=hand("EnginePower"))
    assert not s.fired()


# ------------------------------------------------------------------------------------------------ car_damaged

def test_puncture_edge_and_rearm():
    s = Sim()
    s.player()
    s.puncture = True
    s.player()
    assert not s.fired("car_damaged")          # one sample is not enough (G6)
    s.advance(0.1)
    s.player()
    assert [t["signal"] for t in s.fired("car_damaged")] == ["puncture"]
    assert s.fired()[0]["heuristic"] is False
    for _ in range(5):
        s.advance(0.1)
        s.player()
    assert len(s.fired("car_damaged")) == 1
    s.puncture = False
    for _ in range(35):                        # clear for >= 3 s of session time re-arms
        s.advance(0.1)
        s.player()
    s.puncture = True
    s.player()
    s.advance(0.1)
    s.player()
    assert len(s.fired("car_damaged")) == 2
    # still punctured after a pit stop: reported again
    s.in_pits = True
    s.player()
    s.in_pits = False
    s.player()
    s.advance(0.1)
    s.player()
    assert len(s.fired("car_damaged")) == 3


def test_puncture_from_car_telemetry_when_player_data_off():
    s = Sim()
    s.telemetry(puncture=False)
    s.advance(0.1)
    s.telemetry(puncture=True)
    s.advance(0.1)
    s.telemetry(puncture=True)
    assert len(s.fired("car_damaged")) == 1 and s.fired()[0]["source"] == "car_telemetry"
    # with a fresh Player packet, Car Telemetry is ignored (no flapping between sources)
    s.puncture = True
    s.player()
    s.telemetry(puncture=False)
    s.player()
    assert len(s.fired("car_damaged")) == 1


def test_puncture_flapping_one_trigger():
    s = Sim()
    s.player()
    for i in range(40):                        # flag toggling every sample for 4 s
        s.advance(0.1)
        s.puncture = (i % 2 == 0)
        s.player()
    assert len(s.fired("car_damaged")) == 0    # never two consecutive samples
    s2 = Sim()
    s2.player()
    for i in range(40):                        # 2 samples on, 2 off, ... (rises every 0.4 s)
        s2.advance(0.1)
        s2.puncture = (i // 2) % 2 == 0
        s2.player()
    assert len(s2.fired("car_damaged")) == 1


def test_puncture_rearm_after_clear_3s():
    s = Sim()
    s.puncture = True
    for _ in range(3):
        s.advance(0.1)
        s.player()
    assert len(s.fired("car_damaged")) == 1
    s.puncture = False
    for _ in range(25):                        # clear 2.5 s: not re-armed
        s.advance(0.1)
        s.player()
    s.puncture = True
    for _ in range(3):
        s.advance(0.1)
        s.player()
    assert len(s.fired("car_damaged")) == 1
    s.puncture = False
    for _ in range(35):
        s.advance(0.1)
        s.player()
    s.puncture = True
    for _ in range(3):
        s.advance(0.1)
        s.player()
    assert len(s.fired("car_damaged")) == 2


def test_car_damaged_payload_has_pit_fields():
    """D19: car_damaged carries can_pit, pit_planned, in_pits and laps_remaining."""
    s = Sim()
    s.lap = 2
    s.lap_data()
    s.puncture = True
    s.player()
    s.advance(0.1)
    s.player()
    t = s.fired("car_damaged")[0]
    assert t["can_pit"] is True and t["pit_planned"] is False and t["in_pits"] is False
    assert t["laps_remaining"] == 8


def _steady(s, seconds=3.0):
    for _ in range(int(seconds / 0.5)):
        s.advance(0.5)
        s.status()


def test_performance_drop_heuristic():
    s = Sim()
    _steady(s)
    s.perf[s.me][1] = 0.7                     # EngineTorque -30 % on my car only
    s.advance(0.5)
    s.status()
    assert not s.fired()                      # must persist PERF_CONFIRM_ST (1 s of session time)
    s.advance(0.5)
    s.status()
    assert not s.fired()
    s.advance(0.5)
    s.status()
    fired = s.fired("car_damaged")
    assert len(fired) == 1 and fired[0]["signal"] == "performance_drop" and fired[0]["heuristic"] is True
    assert fired[0]["area"] == "EngineTorque" and fired[0]["drop_fraction"] == pytest.approx(0.3)
    assert [a["area"] for a in fired[0]["areas"]] == ["EngineTorque"]
    _steady(s)
    assert len(s.fired("car_damaged")) == 1   # alarmed until it recovers
    s.perf[s.me][1] = 1.0
    _steady(s)
    s.perf[s.me][1] = 0.6
    _steady(s, 1.5)
    assert len(s.fired("car_damaged")) == 2


def test_performance_drop_keyed_by_packet_seat_car():
    """The game re-orders indices: history follows each packet's own seat_car[0], not an index."""
    s = Sim()
    _steady(s)
    s.perf[0], s.perf[s.me] = s.perf[s.me], s.perf[0]
    s.me = 0                                   # my car moved to index 0, values unchanged
    _steady(s)
    assert not s.fired()


def test_performance_drop_silent_when_field_drops():
    s = Sim()
    _steady(s)
    for p in s.perf:
        p[2] = 0.6                            # Downforce falls for everyone (weather)
    _steady(s)
    assert not s.fired()


def test_weather_hits_me_early_no_fire():
    s = Sim()
    _steady(s)
    s.perf[s.me][2] = 0.7                     # weather reaches my car one Car Status packet early
    s.advance(0.5)
    s.status()
    for i, p in enumerate(s.perf):
        p[2] = 0.7
    _steady(s)
    assert not s.fired()


def test_two_areas_one_trigger():
    s = Sim()
    _steady(s)
    s.perf[s.me][1] = 0.7
    s.perf[s.me][3] = 0.6
    _steady(s)
    fired = s.fired("car_damaged")
    assert len(fired) == 1
    assert sorted(a["area"] for a in fired[0]["areas"]) == sorted([AREAS[1], AREAS[3]])


def test_damage_not_superseded_by_damage():
    s = Sim()
    _steady(s)
    s.perf[s.me][1] = 0.7
    _steady(s)
    s.puncture = True
    s.player()
    s.advance(0.1)
    s.player()
    fired = s.fired("car_damaged")
    assert len(fired) == 2
    assert s.engine.public(fired[0])["superseded"] is False
    out = s.engine.collect(None)
    assert [e["seq"] for e in out["entries"]] == [fired[0]["seq"], fired[1]["seq"]]
    # a pit stop (new stint) supersedes both
    s.in_pits = True
    s.player()
    s.in_pits = False
    s.puncture = False
    s.player()
    pub = s.engine.public(fired[0])
    assert pub["superseded"] is True and pub["after_pit_stop"] is True


def test_boost_end_no_fire():
    s = Sim()
    _steady(s)
    s.abilities(speed_boost=True)
    s.perf[s.me][1] = 1.3
    _steady(s, 2.0)
    s.abilities()
    s.perf[s.me][1] = 1.0                     # boost ends: back to normal is a "drop" of 23 %
    _steady(s, 5.0)
    assert not s.fired()


def test_slipstream_no_fire():
    s = Sim()
    _steady(s)
    s.abilities(slipstream=0.5)
    s.perf[s.me][4] = 0.6
    _steady(s, 1.0)
    s.abilities(slipstream=0.0)
    s.perf[s.me][4] = 1.0
    _steady(s, 4.0)
    assert not s.fired()


def test_performance_drop_silent_while_ability_active_or_disabled():
    s = Sim()
    _steady(s)
    s.abilities(speed_boost=True)
    s.perf[s.me][1] = 0.7
    _steady(s, 1.5)
    assert not s.fired()
    s2 = Sim(perf_drop=0)
    _steady(s2)
    s2.perf[s2.me][1] = 0.5
    _steady(s2)
    assert not s2.fired()


def test_slow_decline_is_not_a_failure():
    s = Sim()
    for _ in range(40):
        s.perf[s.me][0] *= 0.98
        s.advance(0.5)
        s.status()
    assert not s.fired()


# ------------------------------------------------------------------------------------------------ tires

def run_laps(s, laps, lap_s=30.0, wear=0.2, step=1.0, until=None):
    """Drive the sim: health falls linearly, lap counter advances every lap_s."""
    steps = int(laps * lap_s / step)
    for _ in range(steps):
        s.advance(step)
        if not s.in_pits:
            s.health = max(0.0, s.health - wear * step / lap_s)
        if s.st >= (s.lap + 1) * lap_s + s.lap_offset:
            s.lap += 1
        s.player()
        s.lap_data()
        if until is not None and until(s):
            return


def new_tire_sim(**kw):
    s = Sim(**kw)
    s.lap_offset = 0.0
    s.player()
    s.lap_data()
    return s


def test_tire_prediction_fires_on_the_right_lap():
    s = new_tire_sim()
    run_laps(s, 6, until=lambda s: s.fired("tires_wont_last"))
    fired = s.fired("tires_wont_last")
    assert len(fired) == 1
    t = fired[0]
    # health at lap boundary k is 1 - 0.2 k; with 0.2 per lap the end of the next lap is 0.4 below that:
    # first <= 0.05 when k = 3 (0.4 now, 0.0 predicted)
    assert t["laps_completed"] == 3 and t["lap"] == 4 and t["current_lap"] == 4
    assert t["reason"] == "prediction"
    assert t["wear_per_lap"] == pytest.approx(0.2, abs=0.01)
    assert t["predicted_health_end_of_next_lap"] <= 0.05
    assert t["laps_remaining"] == 7 and t["can_pit"] is True and t["compound"] == "Standard"
    run_laps(s, 1)
    assert len(s.fired("tires_wont_last")) == 1    # not re-fired while nothing changed


def test_tire_floor_without_estimate():
    s = new_tire_sim()
    s.health = 0.09
    for _ in range(3):                         # health is a median of the last samples
        s.advance(0.1)
        s.player()
    fired = s.fired("tires_wont_last")
    assert len(fired) == 1 and fired[0]["reason"] == "floor_no_forecast" and fired[0]["wear_per_lap"] is None


def test_tire_extrapolation_within_first_lap():
    s = new_tire_sim()
    s.lap_offset = 0.0
    s.player(strategy={"reference_lap_s": 30.0})
    # very fast wear: 0.5 per lap; before any full lap, extrapolation from the elapsed share must fire
    run_laps(s, 1, wear=0.5, until=lambda s: s.fired("tires_wont_last"))
    fired = s.fired("tires_wont_last")
    assert len(fired) == 1 and "extrapolated" in fired[0]["wear_derived"]
    assert s.lap == 0


def test_tire_silent_when_pit_planned_in_pits_or_last_lap():
    s = new_tire_sim()
    s.pit_planned = True
    run_laps(s, 5)
    assert not s.fired("tires_wont_last")
    s2 = new_tire_sim()
    s2.in_pits = True
    s2.health = 0.05
    s2.player()
    assert not s2.fired()
    s3 = new_tire_sim()
    s3.total_laps = 4
    run_laps(s3, 4.5)                               # would fire at lap 3, which is the last lap
    assert not s3.fired("tires_wont_last")


def test_tire_silent_on_last_lap_without_player_data():
    """Regression (review): with only Driver Data (Car Telemetry + Lap Data + Session), laps_remaining was never
    set, so tires_wont_last could fire on the last lap."""
    s = Sim()
    s.total_laps = 3
    sess = {"session_type": "Race", "energy": "boost", "total_laps": 3, "track": None}
    for lap in range(3):
        s.lap = lap
        s.advance(1.0)
        s.engine.feed(P.PKT_SESSION, s.header(), sess)
        s.lap_data()
        s.telemetry(health=0.9)
    assert s.engine.laps_remaining == 1 and s.engine.player_at is None
    for _ in range(3):
        s.advance(0.1)
        s.telemetry(health=0.05)                     # below the floor, but on the last lap
    s.lap_data()
    assert not s.fired("tires_wont_last")
    s2 = Sim()                              # same, but not on the last lap: must still fire
    s2.total_laps = 5
    for lap in range(3):
        s2.lap = lap
        s2.advance(1.0)
        s2.engine.feed(P.PKT_SESSION, s2.header(), dict(sess, total_laps=5))
        s2.lap_data()
        s2.telemetry(health=0.9)
    for _ in range(3):
        s2.advance(0.1)
        s2.telemetry(health=0.05)
    assert len(s2.fired("tires_wont_last")) == 1


def test_tire_fires_when_the_pit_call_is_cancelled():
    s = new_tire_sim()
    s.pit_planned = True
    run_laps(s, 3.5)
    assert not s.fired()
    s.pit_planned = False
    s.player()
    assert len(s.fired("tires_wont_last")) == 1


def test_tire_rearm_after_pit_exit():
    s = new_tire_sim()
    run_laps(s, 6, until=lambda s: s.fired("tires_wont_last"))
    assert len(s.fired("tires_wont_last")) == 1
    first_seq = s.fired()[0]["seq"]
    s.pit_planned = True
    s.player()
    s.in_pits = True
    s.player()
    s.lap_data()
    s.in_pits = False
    s.pit_planned = False
    s.health = 1.0
    s.compound = "Durable"
    s.player()
    s.lap_data()
    public = s.engine.public(s.fired()[0])
    assert public["superseded"] is True
    s.lap_offset = s.st - (s.lap + 1) * 30.0 + 30.0  # laps continue every 30 s from here
    run_laps(s, 6, until=lambda s: len(s.fired("tires_wont_last")) > 1)
    fired = s.fired("tires_wont_last")
    assert len(fired) == 2 and fired[1]["seq"] > first_seq and fired[1]["compound"] == "Durable"


def test_no_lap_count_session_is_not_silenced():
    s = new_tire_sim()
    s.total_laps = 0
    s.health = 0.05
    for _ in range(3):
        s.advance(0.1)
        s.player(laps_remaining=0)
    assert len(s.fired("tires_wont_last")) == 1


def test_floor_only_without_forecast():
    """G9: below the floor but the forecast says the tires last: no trigger; the floor is used only without one."""
    s = new_tire_sim()
    run_laps(s, 3, wear=0.01)                  # a measured wear of 0.01 per lap
    assert s.engine.wear_estimate()[0] == pytest.approx(0.01, abs=0.002)
    s.health = 0.09                            # (a jump down; the forecast 0.09 - 2 x 0.01 > margin)
    s.player()
    assert not s.fired("tires_wont_last")
    assert s.engine.tire_prediction()["predicted_health_end_of_next_lap"] > 0.05


def test_tires_silent_on_last_lap_player_fresh():
    """G1: Lap Data reaches the last lap before the next (fresh) Player packet: laps_remaining is recomputed
    before the tire evaluation, so no trigger on the last lap."""
    s = new_tire_sim()
    s.total_laps = 5
    lap_s, wear, step = 30.0, 0.17, 1.0
    for _ in range(int(4.5 * lap_s / step)):
        s.advance(step)
        s.health = max(0.0, s.health - wear * step / lap_s)
        if s.st >= (s.lap + 1) * lap_s:
            s.lap += 1
        s.lap_data()                           # Lap Data first ...
        s.player()                             # ... the Player packet of the same tick after it
    assert s.engine.laps_remaining == 1
    assert not s.fired("tires_wont_last")


def test_laps_remaining_without_player():
    """G8: Session total + Lap Data give laps_remaining with Player Data off."""
    s = Sim()
    s.total_laps = 7
    s.session()
    s.lap = 4
    s.lap_data()
    laps = s.engine.laps()
    assert laps["laps_completed"] == 4 and laps["total_laps"] == 7 and laps["laps_remaining"] == 3
    assert laps["lap_source"] == "lap_data" and s.engine.player_at is None


def raw_lap(s, lap_number):
    """Packets showing the raw lap number the game sends for the lap being driven (Lap Data lap, LAPC lap)."""
    s.lap = lap_number - 1


def test_lap_counting_observed_0882_eight_lap_race():
    """Real observation (0.88.2, 8 laps at China, from the server's own lap_observations): LAPC 1 at the start
    (session time 392.6), LAPC 9 at the flag; Lap Data lap = the current lap, lagging LAPC by one sample, 9 after
    the flag; Player total_laps - laps_remaining = the current lap. 0.2.0 read Lap Data lap as laps completed: one
    too many completed, one too few remaining, and 9/8 completed at the end."""
    s = Sim()
    s.total_laps = 8
    s.st = 390.0
    s.session()
    raw_lap(s, 1)                      # grid: Lap Data already says lap 1
    s.lap_data()
    s.player()
    assert s.engine.laps()["laps_completed"] == 0
    s.st = 392.6
    s.event("LAPC", lap=1, position=8)
    laps = s.engine.laps()
    assert (laps["current_lap"], laps["laps_completed"], laps["laps_remaining"]) == (1, 0, 8), laps
    for n in range(2, 9):
        s.advance(90.0)
        s.event("LAPC", lap=n, position=8)          # Lap Data still shows n - 1 (one sample behind)
        raw_lap(s, n - 1)
        s.lap_data()
        laps = s.engine.laps()
        assert (laps["current_lap"], laps["laps_completed"], laps["laps_remaining"]) == (n, n - 1, 9 - n), (n, laps)
        raw_lap(s, n)
        s.lap_data()
        s.player()                                   # raw laps_remaining = 8 - n (laps after the current one)
        laps = s.engine.laps()
        assert (laps["current_lap"], laps["laps_completed"], laps["laps_remaining"]) == (n, n - 1, 9 - n), (n, laps)
    assert s.engine.laps()["final_lap"] is True and s.engine._silenced() == "last lap"
    s.advance(90.0)
    s.event("LAPC", lap=9, position=8)
    s.event("FINI", position=8)
    raw_lap(s, 9)
    s.lap_data()
    laps = s.engine.laps()
    assert laps["laps_completed"] == 8 and laps["laps_remaining"] == 0 and laps["current_lap"] is None, laps
    obs = laps["lap_observations"][-1]
    assert obs["lapc_lap"] == 9 and "observed in 0.88.2" in laps["lap_semantics"]


def test_lap_counting_player_only_and_clamped():
    """Player Data alone: total - laps_remaining = the current lap (laps_remaining 0 on the final lap); values never
    go beyond total_laps or below 0."""
    s = Sim()
    s.total_laps = 8
    s.player(laps_remaining=8)                      # pre-race oddity: would be -1 completed
    assert s.engine.laps()["laps_completed"] == 0
    s.player(laps_remaining=1)                      # driving lap 7
    laps = s.engine.laps()
    assert (laps["current_lap"], laps["laps_completed"], laps["laps_remaining"]) == (7, 6, 2), laps
    s.player(laps_remaining=0)                      # final lap
    assert s.engine.laps()["laps_remaining"] == 1 and s.engine.laps()["current_lap"] == 8
    raw_lap(s, 12)
    s.lap_data()                                    # nonsense above the total: clamped
    assert s.engine.laps()["laps_completed"] == 8 and s.engine.laps()["laps_remaining"] == 0


def test_tires_wont_last_fires_on_lap_7_of_8():
    """Real observation: in 0.2.0 the last-lap silence started on lap 7 of 8 (Player total - remaining read as
    completed), so tires_wont_last stayed silent while two laps remained. It must fire on lap 7 and stay silent on
    lap 8, the final lap."""
    s = new_tire_sim()
    s.total_laps = 8
    raw_lap(s, 7)
    s.lap_data()
    s.player()                                       # Lap Data 7, Player laps_remaining 1
    assert s.engine.laps_remaining == 2 and s.engine._silenced() is None
    s.health = 0.05
    for _ in range(3):
        s.advance(0.1)
        s.player()
    assert len(s.fired("tires_wont_last")) == 1
    t = s.fired("tires_wont_last")[0]
    assert t["current_lap"] == 7 and t["laps_completed"] == 6 and t["laps_remaining"] == 2
    s2 = new_tire_sim()
    s2.total_laps = 8
    raw_lap(s2, 8)
    s2.lap_data()
    s2.player()                                      # Lap Data 8, Player laps_remaining 0: the final lap
    assert s2.engine.laps_remaining == 1
    s2.health = 0.05
    for _ in range(3):
        s2.advance(0.1)
        s2.player()
    assert not s2.fired("tires_wont_last")


# ------------------------------------------------------------------------------------------------ pit call verification

def pit_sim():
    s = new_tire_sim(enabled=("tires_wont_last",))
    s.total_laps = 8
    s.st = 455.0
    raw_lap(s, 1)
    s.lap_data()
    s.player()
    return s


def test_pit_call_lost_at_lap_boundary_observed_0882():
    """Real observation (0.88.2): set_pit(true) Ack at session time 460.5, ~1.5 s before the lap-2 boundary
    (LAPC / PITW at ~462); afterwards Player pit_planned was false. The server must report pit_call_lost, queue a
    note and re-arm tires_wont_last, and never re-send anything."""
    s = pit_sim()
    s.health = 0.02
    for _ in range(3):
        s.advance(0.1)
        s.player()
    assert len(s.fired("tires_wont_last")) == 1       # answered below with set_pit(true)
    s.st = 460.5
    s.player()
    s.engine.note_pit_command(True, {"ok": True})
    assert s.engine.pit_state()["api_call"]["state"] == "pending"
    s.advance(0.1)
    s.player()                                        # the game had not shown pit_planned yet
    s.st = 462.0
    s.event("LAPC", lap=2, position=8)
    s.event("PITW", lap=2, pit_planned=False, can_pit=True)
    assert not s.engine.pit_call_lost                 # Player lags the boundary: not judged yet
    for _ in range(15):
        s.advance(0.1)
        raw_lap(s, 2)
        s.lap_data()
        s.player()                                    # pit_planned false
    ps = s.engine.pit_state()
    assert ps["pit_call_lost"] is True and ps["api_call"]["state"] == "lost", ps
    assert ps["api_call"]["pitw_pit_planned"] is False
    notes = s.engine.take_notes()
    assert len(notes) == 1 and "pit_call_lost" in notes[0] and "460.5" in notes[0]
    assert s.engine.take_notes() == []
    fired = s.fired("tires_wont_last")
    assert len(fired) == 2 and fired[1]["pit_call_lost"] is True     # re-armed: the model is woken again
    # a second call after the boundary sticks: lost clears, the plan is the API's
    s.engine.note_pit_command(True, {"ok": True})
    s.pit_planned = True
    s.advance(0.1)
    s.player()
    ps = s.engine.pit_state()
    assert ps["pit_call_lost"] is False and ps["api_call"]["state"] == "confirmed" and ps["pit_planned_by"] == "api"
    # the car enters the pits: the call did its job
    s.in_pits = True
    s.advance(0.1)
    s.player()
    assert "api_call" not in s.engine.pit_state()


def test_pit_call_confirmed_not_lost_and_cancel_not_lost():
    """A call that the game keeps (pit_planned true after the boundary) is never lost; nor is a call the API
    cancelled with set_pit(false)."""
    s = pit_sim()
    s.engine.note_pit_command(True, {"ok": True})
    s.pit_planned = True
    s.advance(0.1)
    s.player()
    s.event("LAPC", lap=2, position=8)
    for _ in range(20):
        s.advance(0.1)
        s.player()
    assert s.engine.pit_state()["api_call"]["state"] == "confirmed" and not s.engine.pit_call_lost
    s.engine.note_pit_command(False, {"ok": True})
    s.pit_planned = False
    s.event("LAPC", lap=3, position=8)
    for _ in range(20):
        s.advance(0.1)
        s.player()
    assert not s.engine.pit_call_lost and s.engine.take_notes() == []
    # a refused call is not verified at all
    s.engine.note_pit_command(True, {"ok": False, "error_reason": "refused"})
    assert "api_call" not in s.engine.pit_state()


def test_pit_planned_by_game_without_api_call_observed_0882():
    """Real observation (0.88.2): later in the race pit_planned became true with can_pit false and no API call
    (the game's own strategy planned the stop): pit_planned_by "game"."""
    s = pit_sim()
    assert s.engine.pit_state()["pit_planned_by"] is None
    s.pit_planned = True
    s.advance(0.1)
    s.player(can_pit=False)
    assert s.engine.pit_state()["pit_planned_by"] == "game"
    s.engine._tires_armed = True
    s.health = 0.02
    s.advance(0.1)
    s.player(can_pit=False)
    assert not s.fired("tires_wont_last")              # a stop is planned: silent
    s.pit_planned = False
    s.advance(0.1)
    s.player()
    assert s.engine.pit_state()["pit_planned_by"] is None


def test_car_damaged_payload_has_pit_fields():
    s = Sim(enabled=("car_damaged",))
    s.player()
    s.puncture = True
    s.health = 0.0                                     # observed 0.88.2: health 0.0 set the puncture flag
    s.player()
    s.player()
    t = s.fired("car_damaged")[0]
    assert t["pit_planned_by"] is None and t["pit_call_lost"] is False and t["tire_health"] == 0.0


def test_extrapolation_uses_session_time_across_pause():
    """L6: a pause (session time frozen, wall clock running) does not dilute the extrapolated wear."""
    s = new_tire_sim()
    s.player(strategy={"reference_lap_s": 30.0})
    for _ in range(120):                       # 40 % of a lap at 0.3 per lap, 10 Hz
        s.advance(0.1)
        s.health -= 0.3 / 300
        s.player(strategy={"reference_lap_s": 30.0})
    before = s.engine.wear_estimate()[0]
    for _ in range(60):                        # paused for 60 s of wall clock
        s.advance(1.0, paused=True)
        s.status()
        s.player(strategy={"reference_lap_s": 30.0})
    after = s.engine.wear_estimate()[0]
    # (the smoothing median catches up during the pause; the rate is not diluted by the 60 s of wall clock)
    assert before == pytest.approx(0.3, abs=0.02) and after == pytest.approx(0.3, abs=0.02) and after >= before
    assert "of session time" in s.engine.wear_estimate()[1]


def test_bursty_wear_not_reduced():
    """L6: the measured rate is kept when the current lap so far wears more slowly (max of the two)."""
    s = new_tire_sim()
    run_laps(s, 2, wear=0.3)
    measured = s.engine.wear_estimate()[0]
    assert measured == pytest.approx(0.3, abs=0.01)
    run_laps(s, 0.6, wear=0.05)                # slow part of a bursty lap
    assert s.engine.wear_estimate()[0] == pytest.approx(measured)


# ------------------------------------------------------------------------------------------------ tire screen

def test_tire_screen_only_when_enabled():
    s = Sim()
    s.event("TIRE", pre_race=False, current="Wet", allowed=["Standard", "Fast"], picks_left={})
    assert not s.fired()
    assert s.engine.tire_state()["open"] is True and s.engine.tire_state()["seq"] is None
    s2 = Sim(enabled=("tire_screen",))
    s2.event("TIRE", pre_race=True, current="Wet", allowed=["Standard", "Fast"], picks_left={"Fast": 2})
    s2.event("TIRE", seat=1, pre_race=True, current="Wet", allowed=[], picks_left={})
    fired = s2.fired("tire_screen")
    assert len(fired) == 1 and fired[0]["allowed"] == ["Standard", "Fast"] and fired[0]["pre_race"] is True


def test_tire_screen_enabled_by_default():
    assert "tire_screen" in TriggerConfig().enabled
    assert TriggerConfig.from_env({}).enabled == {"card_offer", "car_damaged", "tires_wont_last", "tire_screen"}


def test_tire_state_closing_rules():
    s = Sim(enabled=("tire_screen",))
    s.in_pits = True
    s.lap_data()
    s.event("TIRE", pre_race=False, current="Standard", allowed=["Fast"], picks_left={})
    seq = s.engine.seq
    assert s.engine.tire_state()["open"] is True and s.engine.tire_state()["seq"] == seq
    assert [d["type"] for d in s.engine.open_decisions()] == ["tire_screen"]
    s.event("PITO")
    assert s.engine.tire_state()["open"] is False and "PITO" in s.engine.tire_state()["closed_because"]
    out = s.engine.collect(None)
    assert out["entries"] == [] and out["skipped"]["superseded"] == 1
    # answered
    s.event("TIRE", pre_race=False, current="Standard", allowed=["Fast"], picks_left={})
    s.engine.answer_tire({"ok": False, "error_reason": "compound_refused"})
    assert s.engine.tire_state()["open"] is True
    s.engine.answer_tire({"ok": True})
    assert s.engine.tire_state()["open"] is False
    # pre-race: closes when the car moves for 1 s of session time, or on LAPC
    s.event("TIRE", pre_race=True, current="Standard", allowed=["Fast"], picks_left={})
    s.speed = 0.0
    s.telemetry()
    s.speed = 50.0
    for _ in range(5):
        s.advance(0.1)
        s.telemetry()
    assert s.engine.tire_state()["open"] is True
    for _ in range(7):
        s.advance(0.1)
        s.telemetry()
    assert s.engine.tire_state()["open"] is False
    s.event("TIRE", pre_race=True, current="Standard", allowed=["Fast"], picks_left={})
    s.event("LAPC", lap=1)
    assert s.engine.tire_state()["open"] is False


# ------------------------------------------------------------------------------------------------ cards (state)

H1 = ("EnginePower", "Downforce", "TireGrip")
H2 = ("Gearbox", "Cooling", "Reroll")


def test_card_event_then_lagging_player_one_offer():
    """G2: the Player packet lags the CARD event (still points 0) by 0, 1, 3 or 6 samples: one offer, and the
    hand never looks closed to collect()."""
    for lag in (0, 1, 3, 6):
        s = Sim()
        s.engine.set_decision_events(True)
        s.player()
        s.event("CARD", points=1, hand=hand(*H1))
        for _ in range(lag):
            s.advance(0.1)
            s.player()                          # points 0, no hand yet
            assert s.engine.card_state()["open"] is True
        s.points, s.hand = 1, hand(*H1)
        for _ in range(40):
            s.advance(0.1)
            s.player()
        assert len(s.fired("card_offer")) == 1, lag
        assert s.engine.card_state()["open"] is True
        out = s.engine.collect(None)
        assert len(out["entries"]) == 1 and out["entries"][0]["open"] is True


def test_hand_flicker_shorter_than_debounce_no_refire():
    """G5: Decision Events off; a transient other hand for 0.15 s (< 0.25 s debounce) does not re-fire H1."""
    s = Sim()
    s.engine.set_decision_events(False)
    s.points, s.hand = 1, hand(*H1)
    for _ in range(20):
        s.advance(0.1)
        s.player()
    assert len(s.fired("card_offer")) == 1
    s.hand = hand(*H2)
    for _ in range(3):
        s.advance(0.05)
        s.player()
    s.hand = hand(*H1)
    for _ in range(20):
        s.advance(0.1)
        s.player()
    assert len(s.fired("card_offer")) == 1
    assert s.engine.card_state()["open"] is True


def test_card_state_player_data_off_unknown_after_resume():
    s = Sim()
    s.engine.set_decision_events(True)
    s.status()
    s.event("CARD", points=2, hand=hand(*H1))
    seq = s.engine.seq
    cs = s.engine.card_state()
    assert cs["open"] is True and cs["seq"] == seq and cs["points"] == 2 and cs["sig"]
    for _ in range(10):                        # paused: session time frozen, wall running
        s.advance(0.5, paused=True)
        s.status()
    assert s.engine.paused() and s.engine.card_state()["open"] is True
    for _ in range(3):                         # resumed, no Player packet
        s.advance(0.5)
        s.status()
    cs = s.engine.card_state()
    assert cs["open"] is None and "resumed" in cs["closed_because"]
    assert [d["type"] for d in s.engine.open_decisions()] == ["card_offer"]


def test_answer_card_semantics():
    s = Sim()
    s.engine.set_decision_events(True)
    s.points, s.hand = 1, hand(*H1)
    s.event("CARD", points=1, hand=s.hand)
    s.player()
    seq = s.engine.seq
    assert s.engine.hand_sig_for_seq(seq) == s.engine.card_state()["sig"]
    assert s.engine.hand_sig_for_seq(seq + 99) is None
    s.engine.answer_card({"ok": False, "error_reason": "cards_unavailable"})
    assert s.engine.card_state()["open"] is True                  # L5: the hand stays open
    s.engine.answer_card({"ok": True})
    assert s.engine.card_state()["open"] is False
    for _ in range(10):                        # the Player packet still shows the old hand for a while
        s.advance(0.1)
        s.player()
    assert len(s.fired("card_offer")) == 1
    assert s.engine.collect(None)["entries"] == []
    # the next hand (same cards dealt again) is a new offer
    s.event("CARD", points=1, hand=s.hand)
    assert len(s.fired("card_offer")) == 2
    s.engine.answer_card({"ok": False, "error_reason": "no_points"})
    assert s.engine.card_state()["open"] is False


def test_card_cleared_by_player_after_debounce():
    s = Sim()
    s.engine.set_decision_events(True)
    s.points, s.hand = 1, hand(*H1)
    s.event("CARD", points=1, hand=s.hand)
    s.player()
    s.points, s.hand = 0, []                   # picked on screen
    s.advance(0.1)
    s.player()
    assert s.engine.card_state()["open"] is True
    s.advance(0.3)
    s.player()
    cs = s.engine.card_state()
    assert cs["open"] is False and cs["closed_because"] == "Player shows no hand"


def test_paused_hint():
    s = Sim()
    s.status()
    assert s.engine.paused_hint(True, 6.0) and "paused" in s.engine.paused_hint(True, 6.0)
    assert s.engine.paused_hint(True, 3.0) is None and s.engine.paused_hint(False, 60.0) is None
    s.event("CARD", points=1, hand=hand(*H1))
    assert s.engine.paused_hint(True, 6.0) is None


# ------------------------------------------------------------------------------------------------ session end

def test_fini_then_send_one_end():
    """D2: FINI for seat 0 then SEND: one session_ended."""
    s = Sim()
    s.event("FINI", seat=0, position=3)
    s.event("SEND", seat=None)
    s.event("FINI", seat=1, position=4)
    ends = s.fired("session_ended")
    assert len(ends) == 1 and ends[0]["code"] == "FINI" and ends[0]["position"] == 3
    out = s.engine.collect(None)
    assert out["status"] == "session_ended" and len(out["entries"]) == 1


def test_session_end_survives_new_session():
    """G4: SEND and queued triggers of the old session are not wiped by the next session."""
    s = Sim()
    s.event("CARD", points=1, hand=hand(*H1))
    s.event("SEND", seat=None)
    seq = s.engine.seq
    s.uid = 2
    s.player()
    assert s.engine.session_uid == 2 and len(s.engine.triggers) == 2
    out = s.engine.collect(0)
    assert [e["type"] for e in out["entries"]] == ["session_ended"]
    assert out["entries"][0]["stale_session"] is True and out["entries"][0]["session_uid"] == 1
    assert out["skipped"]["old_session"] == 1 and out["last_seq"] == seq
    s.event("CARD", points=1, hand=hand(*H1))
    assert s.engine.triggers[-1]["seq"] == seq + 1     # sequence numbers keep increasing
    assert len(s.fired("session_ended")) == 1          # SEND was seen: no synthetic end


def test_synthetic_end_without_send():
    s = Sim()
    s.player()
    s.uid = 2
    s.player()
    ends = s.fired("session_ended")
    assert len(ends) == 1 and ends[0]["code"] is None and ends[0]["reason"] == "new_session_started"
    assert ends[0]["session_uid"] == 1
    s.uid = 3
    s.event("SEND", seat=None)
    assert [e["reason"] if e["code"] is None else e["code"] for e in s.fired("session_ended")] == \
        ["new_session_started", "new_session_started", "SEND"]


# ------------------------------------------------------------------------------------------------ delivery

def _puncture(s):
    s.puncture = True
    s.player()
    s.advance(0.1)
    s.player()


def test_collect_basic_ack_and_status():
    s = Sim()
    assert s.engine.collect(None) == {"entries": [], "last_seq": 0, "acked": 0,
                                      "skipped": {"superseded": 0, "old_session": 0}, "status": None,
                                      "notes": []}
    _puncture(s)
    out = s.engine.collect(0)
    assert out["status"] == "triggered" and out["entries"][0]["resent"] is False
    assert out["entries"][0]["open"] is None and out["last_seq"] == 1
    assert s.engine.triggers[0]["returns"] == 1
    assert s.engine.collect(None)["entries"] == []
    out = s.engine.collect(1)
    assert out["entries"] == [] and s.engine.acked == 1
    assert s.engine.next_due_in() is None


def test_first_call_of_process_ignores_since_seq():
    s = Sim()
    _puncture(s)
    out = s.engine.collect(57)                 # model's cursor from a previous server process
    assert len(out["entries"]) == 1 and out["notes"] and "57" in out["notes"][0]
    # later: since_seq ahead of the newest seq resets the acknowledgement
    s.engine.collect(1)
    assert s.engine.acked == 1
    out = s.engine.collect(500)
    assert s.engine.acked == 0 and any("ahead" in n for n in out["notes"])


def test_puncture_dropped_resent_once_with_since_seq_never_without():
    s = Sim()
    _puncture(s)
    first = s.engine.collect(0)                # result lost by the client bridge
    assert len(first["entries"]) == 1
    assert s.engine.collect(0)["entries"] == []          # not immediately again
    assert s.engine.next_due_in() == pytest.approx(20.0)
    s.t += 20.0
    out = s.engine.collect(0)
    assert len(out["entries"]) == 1 and out["entries"][0]["resent"] is True
    s.t += 20.0
    assert s.engine.collect(0)["entries"] == []          # exactly once
    assert s.engine.next_due_in() is None
    # without since_seq (legacy): at most once
    s2 = Sim()
    _puncture(s2)
    assert len(s2.engine.collect(None)["entries"]) == 1
    for _ in range(5):
        s2.t += 20.0
        assert s2.engine.collect(None)["entries"] == []
    assert s2.engine.next_due_in() is None


def test_stale_since_seq_stuck_is_bounded():
    """A model that keeps passing an old since_seq while an open card waits: at most 1 + MAX_RESEND immediate
    returns, at least REMIND_S apart."""
    from backseat_champions_mcp.triggers import MAX_RESEND, REMIND_S
    s = Sim()
    s.engine.set_decision_events(True)
    s.points, s.hand = 1, hand(*H1)
    s.event("CARD", points=1, hand=s.hand)
    s.player()
    returns = []
    for _ in range(int(300 / 3)):              # poll every 3 s for 300 s with since_seq=0
        s.advance(3.0, paused=True)
        s.status()
        s.player()
        out = s.engine.collect(0)
        if out["entries"]:
            returns.append(s.t)
    assert len(returns) == 1 + MAX_RESEND
    assert all(b - a >= REMIND_S for a, b in zip(returns, returns[1:]))


def test_open_card_resend_and_closed_card_skipped():
    s = Sim()
    s.engine.set_decision_events(True)
    s.points, s.hand = 1, hand(*H1)
    s.event("CARD", points=1, hand=s.hand)
    s.player()
    assert len(s.engine.collect(None)["entries"]) == 1
    s.t += 20.0
    out = s.engine.collect(None)
    assert len(out["entries"]) == 1 and out["entries"][0]["resent"] is True and out["entries"][0]["open"] is True
    s.points, s.hand = 0, []                   # decided on screen
    s.player()
    s.advance(0.3)
    s.player()
    s.t += 20.0
    out = s.engine.collect(None)
    assert out["entries"] == [] and out["skipped"]["superseded"] == 1 and out["last_seq"] == 1
    assert s.engine.next_due_in() is None


def test_last_seq_never_acks_unseen_entry():
    """last_seq does not jump over an entry returned earlier (result possibly lost) that awaits its resend."""
    s = Sim()
    _puncture(s)
    s.engine.collect(0)                        # returned (and lost)
    s.event("CARD", points=1, hand=hand(*H1))
    out = s.engine.collect(0)
    assert [e["type"] for e in out["entries"]] == ["card_offer"]
    assert out["last_seq"] == 0
    s.t += 20.0
    out = s.engine.collect(out["last_seq"])
    assert [(e["type"], e["resent"]) for e in out["entries"]] == [("car_damaged", True), ("card_offer", True)]
    assert out["last_seq"] == 2


def test_queue_eviction_note():
    s = Sim(queue_size=10)
    s.engine.collect(0)
    for i in range(15):
        s.event("CARD", points=1 + i, hand=hand("H%d" % i))
    assert len(s.engine.triggers) == 10
    out = s.engine.collect(0)
    assert any("5 triggers evicted" in n for n in out["notes"])
    assert out["skipped"]["superseded"] == 9 and len(out["entries"]) == 1
    assert out["last_seq"] == 15
    assert not s.engine.collect(out["last_seq"])["notes"]


def test_remind_s_from_env(monkeypatch):
    monkeypatch.setenv("BC_REMIND_S", "0.5")
    assert TriggerEngine(TriggerConfig()).remind_s == 0.5
    assert TriggerEngine(TriggerConfig.from_env({"BC_REMIND_S": "2"})).remind_s == 2.0
    monkeypatch.delenv("BC_REMIND_S")
    assert TriggerEngine(TriggerConfig()).remind_s == 20.0


def test_waiter_wakes_on_trigger():
    s = Sim()

    async def main():
        fut = s.engine.waiter()
        asyncio.get_running_loop().call_later(0.05, lambda: s.event("CARD", points=1, hand=hand("Gearbox")))
        await asyncio.wait_for(fut, 1.0)
        return s.engine.collect(None)

    out = asyncio.run(main())
    assert out["entries"][0]["type"] == "card_offer"


def test_config_validation_and_env():
    cfg = TriggerConfig.from_env({"BC_TRIGGERS": "card_offer, tire_screen", "BC_TIRE_MARGIN": "0.1"})
    assert cfg.enabled == {"card_offer", "tire_screen"} and cfg.tire_margin == 0.1
    assert TriggerConfig.from_env({"BC_TRIGGERS": "none"}).enabled == frozenset()
    with pytest.raises(ValueError):
        TriggerConfig(enabled=("warp_drive",))
    with pytest.raises(ValueError):
        TriggerConfig.from_env({"BC_TRIGGER_PERF_DROP": "abc"})
    with pytest.raises(ValueError):
        TriggerConfig.from_env({"BC_REMIND_S": "0"})


def test_tire_health_noise_one_stint_one_trigger():
    """RISK-noise: +-0.02 noise around linear wear, no pit stop: one tire set, one tires_wont_last."""
    import random
    rnd = random.Random(7)
    s = new_tire_sim()
    s.total_laps = 60
    base = 1.0
    lap_s, step = 30.0, 0.1
    for _ in range(int(12 * lap_s / step)):
        s.advance(step)
        base = max(0.0, base - 0.1 * step / lap_s)
        s.health = max(0.0, base + rnd.uniform(-0.02, 0.02))
        if s.st >= (s.lap + 1) * lap_s:
            s.lap += 1
        s.player()
        s.lap_data()
    assert s.engine._stint_id == 1
    fired = s.fired("tires_wont_last")
    assert len(fired) == 1 and fired[0]["laps_completed"] in (7, 8)
    assert s.engine.wear_estimate()[0] == pytest.approx(0.1, abs=0.02)


def test_new_tires_need_sustained_large_rise():
    s = new_tire_sim()
    s.health = 0.5
    for _ in range(5):
        s.advance(0.1)
        s.player()
    stint = s.engine._stint_id
    s.health = 0.9                             # a single spike: not a new set
    s.advance(0.1)
    s.player()
    s.health = 0.5
    for _ in range(3):
        s.advance(0.1)
        s.player()
    assert s.engine._stint_id == stint
    s.health = 1.0                             # sustained for >= 0.5 s: new set (no pit seen, e.g. Player off)
    for _ in range(7):
        s.advance(0.1)
        s.player()
    assert s.engine._stint_id == stint + 1 and s.engine._stint["why"] == "tire health rose"
    assert s.engine.health == pytest.approx(1.0)
