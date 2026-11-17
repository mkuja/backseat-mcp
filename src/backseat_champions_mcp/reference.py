"""Static reference data: what each error reason, event code, packet, command and settings switch means.

Everything here restates the game's ``GameAPI/README.md``. Where the README gives only a name (for instance the
error ``replay_playing``), the text says so instead of guessing. The MCP server serves these tables through the
``get_reference`` tool and the ``bc://reference/{topic}`` resources, and uses ``ERROR_INFO`` to explain refused
commands.
"""
from . import protocol as P

SETTINGS_PATH = "Settings > Telemetry"

# Broadcast switches: Hello.broadcast_flags bit, the key used in tool output, and the packets each one gates.
BROADCAST_SWITCHES = {
    "driver_data": {
        "label": "Driver Data", "hello_bit": 0,
        "packets": ["motion", "car_telemetry", "lap_data", "car_status", "participants", "racing_line",
                    "car_abilities (also sent with Player Data)"],
        "tools": ["get_standings", "get_participants", "get_car_telemetry", "get_car_motion", "get_car_status",
                  "get_car_abilities", "get_racing_line", "get_race_snapshot (position, gaps)",
                  "triggers: car_damaged performance_drop heuristic (Car Status); lap counting for tires_wont_last "
                  "(Lap Data); puncture / tire health when Player Data is off (Car Telemetry)"],
    },
    "player_data": {
        "label": "Player Data", "hello_bit": 1,
        "packets": ["player", "car_abilities (also sent with Driver Data)"],
        "tools": ["get_player_state", "get_pending_decisions (card and pit state)", "get_car_abilities",
                  "triggers: card_offer fallback when Decision Events is off; tire health, puncture, laps remaining "
                  "and pit flags for tires_wont_last and car_damaged"],
    },
    "decision_events": {
        "label": "Decision Events", "hello_bit": 2,
        "packets": ["event"],
        "tools": ["get_events", "wait_for_event", "get_pending_decisions (the tire screen is only announced by the "
                  "TIRE event)", "triggers: card_offer (CARD event), tire_screen (TIRE event), session_ended in "
                  "wait_for_trigger (SEND / FINI)"],
    },
    "ai_observations": {
        "label": "AI Observations", "hello_bit": 3,
        "packets": ["ai_observations"],
        "tools": ["get_ai_observations"],
        "note": "Off by default: 16 x 1,432 bytes per tick, about 1.1 MB/s at 50 Hz. Only sent by the machine that "
                "simulates the cars (single player, splitscreen, or the host online).",
    },
}

# Input switches: Hello.api_flags / Session.api_flags bit and the commands each one gates.
INPUT_SWITCHES = {
    "player_input": {
        "label": "Player Input", "flag_bit": 0,
        "commands": ["pick_card", "ban_card", "set_pit", "pick_tire", "boost", "set_camera"],
        "tools": ["pick_card", "ban_card", "set_pit", "pick_tire", "boost", "stop_boost", "set_camera"],
    },
    "driver_input": {
        "label": "Driver Input", "flag_bit": 1,
        "commands": ["drive", "release_drive"],
        "tools": ["drive", "release_drive"],
    },
}

# Which broadcast switches (any one of them) a telemetry packet needs.
PACKET_SWITCHES = {
    P.PKT_MOTION: ("driver_data",),
    P.PKT_CAR_TELEMETRY: ("driver_data",),
    P.PKT_LAP_DATA: ("driver_data",),
    P.PKT_CAR_STATUS: ("driver_data",),
    P.PKT_PLAYER: ("player_data",),
    P.PKT_SESSION: ("driver_data", "player_data", "decision_events", "ai_observations"),
    P.PKT_PARTICIPANTS: ("driver_data",),
    P.PKT_EVENT: ("decision_events",),
    P.PKT_AI_OBSERVATIONS: ("ai_observations",),
    P.PKT_CAR_ABILITIES: ("driver_data", "player_data"),
    P.PKT_RACING_LINE: ("driver_data",),
}

PACKET_INFO = {
    P.PKT_MOTION: ("at the configured rate", "Driver Data",
                   "Per car: world position (m), velocity and angular velocity in the car's own frame, pitch/roll/yaw."),
    P.PKT_CAR_TELEMETRY: ("at the configured rate", "Driver Data",
                          "Per car: speed, applied pedal and steer, four wheels (track limits, ground contact, slip), "
                          "tire compound, puncture, tire health, energy (boost or fuel)."),
    P.PKT_LAP_DATA: ("at the configured rate", "Driver Data",
                     "Per car: position, class position, laps completed, pit/finished flags, pit stops, gaps."),
    P.PKT_CAR_STATUS: ("twice a second", "Driver Data",
                       "Per car: current performance ratio in each of the 8 performance areas."),
    P.PKT_PLAYER: ("at the configured rate, one per local player", "Player Data",
                   "One local player's points, card hand, laps, pit flags, tires, compound picks left, energy and "
                   "the strategy model's view of the race."),
    P.PKT_SESSION: ("twice a second, and when a session starts", "any broadcast option",
                    "Session type, fuel or boost, laps, input flags, weather, track scene and measured track "
                    "properties."),
    P.PKT_PARTICIPANTS: ("every 5 seconds, and when a session starts", "Driver Data",
                         "Per car: player id, car type, kind (AI / local player / remote player) and display name."),
    P.PKT_EVENT: ("when it happens", "Decision Events", "A four-letter event code, the seat it is about, details."),
    P.PKT_AI_OBSERVATIONS: ("at the configured rate, one per car, from the host", "AI Observations",
                            "One car's driving net: policy id and name, last action (pedal, steer) and its input "
                            "vector."),
    P.PKT_ACK: ("in reply to a command", "", "The command was accepted."),
    P.PKT_ERROR: ("in reply to a command", "", "The command was refused; carries an error reason."),
    P.PKT_HELLO: ("in reply to hello", "", "Game version, input flags, broadcast switches, rate, feature schema."),
    P.PKT_CAR_ABILITIES: ("at the configured rate", "Driver Data or Player Data",
                          "Per car: active boosts, DRS, overspeed, recharging, unlocks, ban tokens, slipstream."),
    P.PKT_RACING_LINE: ("at the configured rate, one per local player", "Driver Data",
                        "The next 20 checkpoints ahead of a local player's car: racing line point and both track "
                        "edges."),
}

COMMAND_INFO = {
    P.CMD_PING: ("", "", "Ack", "Liveness check. Allowed in a public lobby."),
    P.CMD_HELLO: ("", "", "Hello", "Reports what is switched on. Allowed in a public lobby."),
    P.CMD_PICK_CARD: ("uint8 seat, uint8 index (into the hand)", "Player Input", "Ack",
                      "Picks a card from the hand on offer."),
    P.CMD_SET_PIT: ("uint8 seat, bool pit", "Player Input", "Ack", "Calls (1) or cancels (0) a pit stop."),
    P.CMD_PICK_TIRE: ("uint8 seat, uint8 compound", "Player Input, tire screen open", "Ack",
                      "Picks a tire compound while the tire screen is open."),
    P.CMD_DRIVE: ("uint8 seat, float pedal (-1 brake to 1 throttle), float steer (-1 to 1)",
                  "Driver Input, this machine simulates the car", "only errors",
                  "Holds the car while commands keep arriving (0.25 s timeout); send at 50 Hz. Withholds "
                  "achievements, Steam stats and leaderboard submissions for the race weekend."),
    P.CMD_RELEASE_DRIVE: ("uint8 seat", "Driver Input", "Ack", "Hands the car back to the built-in driver."),
    P.CMD_BAN_CARD: ("uint8 seat, uint8 index (into the hand)", "Player Input", "Ack",
                     "Bans a card: spends a ban token and keeps your turn."),
    P.CMD_BOOST: ("uint8 seat, uint8 buttons (bit 0 speed boost, bit 1 cornering boost, bit 2 party mode)",
                  "Player Input", "only errors",
                  "Holds the boost buttons while commands keep arriving (0.25 s timeout); no buttons lets go."),
    P.CMD_SET_CAMERA: ("uint8 seat, uint8 camera (camera type; 253 previous, 254 next, 255 automatic)",
                       "Player Input", "Ack", "Changes your own view. Allowed in a public lobby."),
}

EVENT_INFO = {
    "SSTA": {"when": "a session's cars are on the grid", "details": [], "respond": None},
    "SEND": {"when": "the session ends", "details": [], "respond": None},
    "CARD": {"when": "a local player has points and a new hand",
             "details": ["points", "hand (up to 4 cards: index, upgrade, facility, tier, banned)"],
             "respond": "pick_card(index) spends a point on a card; ban_card(index) first spends a ban token and "
                        "keeps your turn."},
    "TIRE": {"when": "the tire screen opens (starting grid, qualifying box or pit stop)",
             "details": ["pre_race (a race that has not started)", "current compound", "allowed compounds",
                         "picks_left per compound (null = no limit)"],
             "respond": "pick_tire(compound) with one of the allowed compounds."},
    "LAPC": {"when": "each new lap", "details": ["lap", "position"], "respond": None},
    "PITW": {"when": "each new lap", "details": ["lap", "pit_planned", "can_pit"],
             "respond": "set_pit(true) to call a stop or set_pit(false) to cancel one, when can_pit is true."},
    "PITI": {"when": "entering the pit lane", "details": [], "respond": None},
    "PITO": {"when": "leaving the pit lane", "details": [], "respond": None},
    "FINI": {"when": "the player takes the flag", "details": ["position"], "respond": None},
}

_PLAYER_INPUT_HINT = ("Switch on Player Input in the game (%s, Input). get_api_status shows the current switches."
                      % SETTINGS_PATH)

# reason -> (explanation, hint). Explanations restate the README; hints say what to do next with these tools.
ERROR_INFO = {
    "invalid_packet": (
        "The game found the command too short, or the wrong size for its command id.",
        "This points at a version mismatch between the game and this server. Compare get_api_status "
        "(game_version) with the version this server was written for."),
    "unsupported_version": (
        "The game does not speak this packet_format or command_version.",
        "The game's API version differs from the one this server implements (packet format 1, command version 1). "
        "Update the server or the game."),
    "unknown_command": (
        "The game does not know this command id.",
        "The game is probably older than the API this server implements. Check get_api_status (game_version)."),
    "public_lobby": (
        "All input is off in a public lobby: every command except ping, hello and set_camera is refused, and a car "
        "being driven or boosted is handed back at once. A lobby is public when it is listed in the lobby browser, "
        "was found through matchmaking, or has a member who is not your Steam friend.",
        "Do not retry in this lobby. Telemetry and events still arrive, so read tools and set_camera keep working."),
    "input_disabled": (
        "The input this command needs is switched off in the game.",
        "pick_card, ban_card, set_pit, pick_tire, boost and set_camera need Player Input; drive and release_drive "
        "need Driver Input. Switch it on under %s (Input), then retry. get_api_status shows both flags."
        % SETTINGS_PATH),
    "not_your_car": (
        "The command named a seat other than 0. Only player one's own car can be controlled.",
        "This server always sends seat 0, so this is unexpected; check get_api_status."),
    "no_player": (
        "There is no session, or no player in seat 0.",
        "Wait until a session is running (an SSTA event, or get_session returning data) and retry."),
    "replay_playing": (
        "The game refused the command with reason replay_playing.",
        "The API documentation gives only the name of this reason. Retry once the replay is over."),
    "no_points": (
        "There are no upgrade points to spend.",
        "Wait for the next CARD event (wait_for_event) or check points in get_player_state."),
    "invalid_index": (
        "The index is not a card in the current hand.",
        "Read the current hand with get_pending_decisions or get_player_state; valid indices are 0 to "
        "(number of cards - 1)."),
    "cards_unavailable": (
        "The game refused the command with reason cards_unavailable.",
        "The API documentation gives only the name of this reason. Check get_pending_decisions for a hand on "
        "offer, or wait for the next CARD event."),
    "card_banned": (
        "That card is banned and cannot be picked.",
        "Pick a card whose banned flag is false (get_pending_decisions shows the hand)."),
    "driver_decided": (
        "Reserved error reason; the API documentation says it is never sent.",
        "Unexpected. Check get_api_status."),
    "refused": (
        "The game would not make the pit call.",
        "Check can_call_now in get_player_state (or can_pit in the latest PITW event) before calling set_pit."),
    "invalid_compound": (
        "That is not a tire compound the game knows.",
        "Use one of the names in get_reference(topic='compounds')."),
    "tire_select_closed": (
        "The tire screen is not open, so no compound can be picked.",
        "pick_tire only works while the tire screen is open (starting grid, qualifying box or pit stop). Wait for "
        "a TIRE event (wait_for_event with codes=['TIRE'])."),
    "compound_refused": (
        "That compound can't be picked now.",
        "Choose one of the allowed compounds of the latest TIRE event (get_pending_decisions) with picks left."),
    "not_host": (
        "Only the machine that simulates the car can drive it: single player, splitscreen, or a private online "
        "lobby you host. An online client gets this error.",
        "drive cannot be used in this session."),
    "invalid_axis": (
        "pedal or steer is not a finite number.",
        "Send finite values between -1 and 1."),
    "internal_error": (
        "The game reported an internal error while handling the command.",
        "Retry once; if it persists, check the game's log."),
    "ban_refused": (
        "The ban was refused: no ban tokens left, the card is the last one left to pick, or it can't be banned.",
        "Check ban_tokens in get_pending_decisions or get_car_abilities, and pick a card instead."),
    "assist_on": (
        "A Boost Assist (or Party Mode Assist) is switched on in the game; it boosts for the player and keeps "
        "those buttons, so the API cannot hold them.",
        "Switch the assist off in the game to boost through the API, or leave boosting to the assist."),
    "camera_unavailable": (
        "That is not a camera this race offers.",
        "set_camera takes the cameras the race's camera button cycles through (today all but TrackOverview), or "
        "next / previous / automatic."),
}

# Failures this server reports itself, in the same shape as the game's reasons.
LOCAL_ERROR_INFO = {
    "no_reply": (
        "The game did not answer.",
        "Possible causes: the game is not running; the Game API is switched off (%s); the game listens on a "
        "different address or port than this server sends to; or, when the game runs on another machine, the "
        "Command Token is missing or wrong (the game then drops commands without answering)." % SETTINGS_PATH),
    "game_unreachable": (
        "The operating system reports that nothing is listening on the game's command port.",
        "Start the game and switch on the Game API (%s), or correct BC_COMMAND_HOST / BC_COMMAND_PORT."
        % SETTINGS_PATH),
    "send_failed": (
        "The command could not be sent.",
        "Check BC_COMMAND_HOST / BC_COMMAND_PORT and the network."),
    "bad_argument": (
        "The argument is not valid for this command.",
        "See the tool description for the allowed values."),
    "drive_disabled": (
        "Driving is disabled in this server.",
        "The server operator must start it with BC_ALLOW_DRIVE=1 (or --allow-drive)."),
}


def explain_error(reason):
    """``(explanation, hint)`` for a game error reason or one of this server's own."""
    info = ERROR_INFO.get(reason) or LOCAL_ERROR_INFO.get(reason)
    if info:
        return info
    return ("The game refused the command with a reason this server does not know (%s)." % reason,
            "The game may be newer than this server. See the game's GameAPI/README.md.")


def _numbered(table):
    return [{"id": i, "name": name} for i, name in enumerate(table)]


def _topic_error_reasons():
    out = []
    for code, name in enumerate(P.ERROR_REASONS):
        if code == 0:
            continue
        explanation, hint = explain_error(name)
        out.append({"id": code, "name": name, "explanation": explanation, "hint": hint})
    return out


def _topic_event_codes():
    return [{"code": code, "when": info["when"], "details": info["details"], "respond_with": info["respond"]}
            for code, info in EVENT_INFO.items()]


def _topic_packets():
    return [{"id": pid, "name": P.PACKET_NAMES[pid], "bytes": P.PACKET_SIZES[pid], "sent": PACKET_INFO[pid][0],
             "needs": PACKET_INFO[pid][1], "contents": PACKET_INFO[pid][2]} for pid in sorted(P.PACKETS)]


def _topic_commands():
    return [{"id": cid, "name": P.COMMAND_NAMES[cid], "bytes": P.COMMAND_SIZES[cid], "payload": COMMAND_INFO[cid][0],
             "needs": COMMAND_INFO[cid][1], "reply": COMMAND_INFO[cid][2], "notes": COMMAND_INFO[cid][3]}
            for cid in sorted(P.COMMANDS)]


def _topic_settings():
    return {
        "where": "%s in the game. The API is off by default." % SETTINGS_PATH,
        "broadcast_switches": [dict(key=key, **value) for key, value in BROADCAST_SWITCHES.items()],
        "input_switches": [dict(key=key, **value) for key, value in INPUT_SWITCHES.items()],
        "defaults": {"telemetry_targets": ["127.0.0.1:29770", "[::1]:29770"], "command_listen": "127.0.0.1:29771",
                     "rate_hz": "1 to 50 (50 is the physics rate)"},
        "command_token": "Needed only when commands come from another machine: 12 to 47 UTF-8 bytes, set under "
                         "Input in the Telemetry tab. It travels in plain text.",
    }


def _topic_triggers():
    return {
        "model": "The server never acts on its own. wait_for_trigger blocks until a trigger fires; everything else is "
                 "queried on demand. Thresholds are this server's tunable heuristics, not game values.",
        "triggers": [
            {"type": "card_offer", "fires_on": "a new hand for seat 0: CARD event; Player packet when Decision Events "
                                               "is off, or when no CARD event announced a hand within 2 s",
             "needs": "Decision Events or Player Data", "heuristic": False},
            {"type": "car_damaged", "fires_on": "puncture flag 0 -> 1 (definite); or a sudden drop of one performance "
                                                "area of your car not shared by the field and not during an ability "
                                                "(heuristic, BC_TRIGGER_PERF_DROP / BC_TRIGGER_PERF_WINDOW_S)",
             "needs": "Player Data or Driver Data (puncture); Driver Data (performance)", "heuristic": "partly"},
            {"type": "tires_wont_last", "fires_on": "predicted tire health at the end of the next lap <= "
                                                    "BC_TIRE_MARGIN, or health <= BC_TIRE_FLOOR; silent on the last "
                                                    "lap, in the pit lane, after the flag, or with a stop called",
             "needs": "Player Data or Driver Data; Driver Data (Lap Data) or Decision Events (LAPC) to count laps",
             "heuristic": True},
            {"type": "tire_screen", "fires_on": "TIRE event for seat 0 (off by default; BC_TRIGGERS)",
             "needs": "Decision Events", "heuristic": False},
        ],
        "session_ended": "SEND, or FINI for seat 0: wait_for_trigger returns status session_ended once.",
    }


def _topic_rules():
    return {
        "seat": "Input only ever controls your own car: seat must be 0 (player one). Splitscreen guests, other "
                "online players and AI cars cannot be controlled.",
        "public_lobby": "In a public lobby every command except ping, hello and set_camera is refused with "
                        "public_lobby. Telemetry and events still go out.",
        "pick_rules": "Commands go through the same calls as a press on the player's own screen, so every pick rule "
                      "applies: a refused compound, a banned card or a pit call the car can't make comes back as an "
                      "error.",
        "holds": "drive and boost act only while commands keep arriving; after 0.25 s without one the game lets "
                 "go. This server re-sends them at 50 Hz for the duration you ask for.",
        "drive": "Achievements, Steam stats and leaderboard submissions are withheld for any race weekend in which "
                 "drive steered a car; a race that follows qualifying on the same track is the same weekend.",
        "not_switchable": "DRS, slipstream and boost overspeed are applied by the game on its own; the API only "
                          "reports them.",
        "versioning": "Enum values never change meaning; new ones are added at the end. A value this server does "
                      "not know is shown as unknown_<number>.",
    }


_TOPICS = {
    "compounds": ("Tire compounds, as used by pick_tire and in tire fields.", lambda: _numbered(P.COMPOUNDS)),
    "upgrades": ("Upgrade names that appear on cards.", lambda: _numbered(P.UPGRADES)),
    "facilities": ("Facility names that appear on cards.", lambda: _numbered(P.FACILITIES)),
    "session_types": ("Session types.", lambda: _numbered(P.SESSION_TYPES)),
    "car_types": ("Car types.", lambda: _numbered(P.CAR_TYPES)),
    "performance_areas": ("The 8 performance areas of get_car_status, in packet order.",
                          lambda: _numbered(P.PERFORMANCE_AREAS)),
    "camera_types": ("Camera types for set_camera, plus the special values.",
                     lambda: _numbered(P.CAMERA_TYPES) + [{"id": P.CAMERA_PREVIOUS, "name": "previous"},
                                                         {"id": P.CAMERA_NEXT, "name": "next"},
                                                         {"id": P.CAMERA_AUTOMATIC, "name": "automatic"}]),
    "participant_kinds": ("Kinds of participant.", lambda: [
        {"id": 0, "name": "ai", "meaning": "AI"}, {"id": 1, "name": "local", "meaning": "a player on this machine"},
        {"id": 2, "name": "remote", "meaning": "a player on another machine"}]),
    "error_reasons": ("Why the game refuses a command, with what to do about it.", _topic_error_reasons),
    "event_codes": ("Event codes, when they fire, their details and the command that answers them.",
                    _topic_event_codes),
    "packets": ("Every packet the game sends: id, size, rate and the switch it needs.", _topic_packets),
    "commands": ("Every command the game accepts: id, size, payload, the switch it needs and its reply.",
                 _topic_commands),
    "settings": ("The in-game switches and which tools depend on them.", _topic_settings),
    "rules": ("Rules that apply to every command.", _topic_rules),
    "triggers": ("This server's triggers: what fires them, which switches they need, which are heuristic.",
                 _topic_triggers),
}

TOPICS = tuple(_TOPICS)


def topic_descriptions():
    return {name: spec[0] for name, spec in _TOPICS.items()}


def reference(topic="all"):
    """The reference table for one topic, or every table for ``"all"``. Raises ``KeyError`` for an unknown topic."""
    key = (topic or "all").strip().lower().replace("-", "_").replace(" ", "_")
    if key in ("all", "*"):
        return {name: spec[1]() for name, spec in _TOPICS.items()}
    aliases = {"errors": "error_reasons", "events": "event_codes", "cameras": "camera_types",
               "tires": "compounds", "tyres": "compounds", "cards": "upgrades", "switches": "settings"}
    key = aliases.get(key, key)
    if key not in _TOPICS:
        raise KeyError(topic)
    return _TOPICS[key][1]()
