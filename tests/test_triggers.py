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
    """Builds decoded packets for one session with my car at index 3 of 6."""

    def __init__(self, enabled=("card_offer", "car_damaged", "tires_wont_last"), **cfg):
        self.t = 100.0
        self.st = 0.0
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

    def header(self):
        return {"session_uid": self.uid, "seat_car": [self.me, None, None, None], "session_time": self.st}

    def advance(self, dt):
        self.t += dt
        self.st += dt

    def player(self, **kw):
        body = {"seat": 0, "car": self.me, "points": self.points, "hand": self.hand, "upgrades_applied": 0,
                "total_laps": self.total_laps, "laps_remaining": max(0, self.total_laps - self.lap),
                "pit_planned": self.pit_planned, "can_pit": True, "in_pits": self.in_pits, "pit_stops": 0,
                "tire_compound": self.compound, "puncture": self.puncture, "tire_health": self.health,
                "compound_picks_left": {c: None for c in P.COMPOUNDS}, "energy": 1.0, "strategy": None}
        body.update(kw)
        self.engine.feed(P.PKT_PLAYER, self.header(), body)

    def lap_data(self):
        cars = [{"position": i + 1, "class_position": i + 1, "lap": self.lap if i == self.me else 0,
                 "in_pits": self.in_pits if i == self.me else False, "finished": False,
                 "pit_planned": self.pit_planned if i == self.me else False, "api_driving": False, "pit_stops": 0,
                 "gap_to_leader": 0.0, "interval_ahead": 0.0, "interval_behind": 0.0} for i in range(self.n)]
        self.engine.feed(P.PKT_LAP_DATA, self.header(), {"cars": cars})

    def telemetry(self, puncture=None, health=None):
        cars = [{"speed_kmh": 100.0, "pedal": 1.0, "steer": 0.0, "wheels": [], "tire_compound": self.compound,
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
    assert [t["signal"] for t in s.fired("car_damaged")] == ["puncture"]
    assert s.fired()[0]["heuristic"] is False
    for _ in range(5):
        s.advance(0.1)
        s.player()
    assert len(s.fired("car_damaged")) == 1
    s.puncture = False
    s.player()
    s.puncture = True
    s.player()
    assert len(s.fired("car_damaged")) == 2
    # still punctured after a pit stop: reported again
    s.in_pits = True
    s.player()
    s.in_pits = False
    s.player()
    assert len(s.fired("car_damaged")) == 3


def test_puncture_from_car_telemetry_when_player_data_off():
    s = Sim()
    s.telemetry(puncture=False)
    s.telemetry(puncture=True)
    assert len(s.fired("car_damaged")) == 1 and s.fired()[0]["source"] == "car_telemetry"
    # with a fresh Player packet, Car Telemetry is ignored (no flapping between sources)
    s.puncture = True
    s.player()
    s.telemetry(puncture=False)
    s.player()
    assert len(s.fired("car_damaged")) == 1


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
    fired = s.fired("car_damaged")
    assert len(fired) == 1 and fired[0]["signal"] == "performance_drop" and fired[0]["heuristic"] is True
    assert fired[0]["area"] == "EngineTorque" and fired[0]["drop_fraction"] == pytest.approx(0.3)
    _steady(s)
    assert len(s.fired("car_damaged")) == 1   # alarmed until it recovers
    s.perf[s.me][1] = 1.0
    _steady(s)
    s.perf[s.me][1] = 0.6
    s.advance(0.5)
    s.status()
    assert len(s.fired("car_damaged")) == 2


def test_performance_drop_silent_when_field_drops():
    s = Sim()
    _steady(s)
    for p in s.perf:
        p[2] = 0.6                            # Downforce falls for everyone (weather)
    s.advance(0.5)
    s.status()
    assert not s.fired()


def test_performance_drop_silent_while_ability_active_or_disabled():
    s = Sim()
    _steady(s)
    s.abilities(speed_boost=True)
    s.perf[s.me][1] = 0.7
    s.advance(0.5)
    s.status()
    assert not s.fired()
    s2 = Sim(perf_drop=0)
    _steady(s2)
    s2.perf[s2.me][1] = 0.5
    s2.advance(0.5)
    s2.status()
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
    assert t["lap"] == 3
    assert t["reason"] == "prediction"
    assert t["wear_per_lap"] == pytest.approx(0.2, abs=0.01)
    assert t["predicted_health_end_of_next_lap"] <= 0.05
    assert t["laps_remaining"] == 7 and t["can_pit"] is True and t["compound"] == "Standard"
    run_laps(s, 1)
    assert len(s.fired("tires_wont_last")) == 1    # not re-fired while nothing changed


def test_tire_floor_without_estimate():
    s = new_tire_sim()
    s.health = 0.09
    s.player()
    fired = s.fired("tires_wont_last")
    assert len(fired) == 1 and fired[0]["reason"] == "floor" and fired[0]["wear_per_lap"] is None


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
    s.advance(0.5)
    s.telemetry(health=0.05)                         # below the floor, but on the last lap
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
    s2.advance(0.5)
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
    s.player(laps_remaining=0)
    assert len(s.fired("tires_wont_last")) == 1


# ------------------------------------------------------------------------------------------------ others

def test_tire_screen_only_when_enabled():
    s = Sim()
    s.event("TIRE", pre_race=False, current="Wet", allowed=["Standard", "Fast"], picks_left={})
    assert not s.fired()
    s2 = Sim(enabled=("tire_screen",))
    s2.event("TIRE", pre_race=True, current="Wet", allowed=["Standard", "Fast"], picks_left={"Fast": 2})
    s2.event("TIRE", seat=1, pre_race=True, current="Wet", allowed=[], picks_left={})
    fired = s2.fired("tire_screen")
    assert len(fired) == 1 and fired[0]["allowed"] == ["Standard", "Fast"] and fired[0]["pre_race"] is True


def test_session_reset_clears_queue_and_state():
    s = Sim()
    s.event("CARD", points=1, hand=hand("EnginePower"))
    assert len(s.engine.triggers) == 1
    seq = s.engine.seq
    s.uid = 2
    s.player()
    assert len(s.engine.triggers) == 0 and s.engine.session_uid == 2
    assert s.engine.pending() == ([], False)
    s.event("CARD", points=1, hand=hand("EnginePower"))
    assert s.engine.triggers[-1]["seq"] == seq + 1     # sequence numbers keep increasing


def test_delivery_and_session_end():
    s = Sim()
    s.event("CARD", points=1, hand=hand("EnginePower"))
    trig, end = s.engine.pending()
    assert len(trig) == 1 and not end
    s.engine.mark_delivered(trig, end)
    assert s.engine.pending() == ([], False)
    assert len(s.engine.pending(since_seq=0)[0]) == 1
    s.event("SEND", seat=None)
    trig, end = s.engine.pending()
    assert trig == [] and end and s.engine.end_info["code"] == "SEND"
    s.engine.mark_delivered(trig, end)
    s.event("FINI", seat=1, position=3)                  # another seat's flag is not my session end
    assert s.engine.pending() == ([], False)


def test_waiter_wakes_on_trigger():
    s = Sim()

    async def main():
        fut = s.engine.waiter()
        asyncio.get_running_loop().call_later(0.05, lambda: s.event("CARD", points=1, hand=hand("Gearbox")))
        await asyncio.wait_for(fut, 1.0)
        return s.engine.pending()

    trig, _ = asyncio.run(main())
    assert trig[0]["type"] == "card_offer"


def test_config_validation_and_env():
    cfg = TriggerConfig.from_env({"BC_TRIGGERS": "card_offer, tire_screen", "BC_TIRE_MARGIN": "0.1"})
    assert cfg.enabled == {"card_offer", "tire_screen"} and cfg.tire_margin == 0.1
    assert TriggerConfig.from_env({"BC_TRIGGERS": "none"}).enabled == frozenset()
    assert TriggerConfig.from_env({}).enabled == {"card_offer", "car_damaged", "tires_wont_last"}
    with pytest.raises(ValueError):
        TriggerConfig(enabled=("warp_drive",))
    with pytest.raises(ValueError):
        TriggerConfig.from_env({"BC_TRIGGER_PERF_DROP": "abc"})
