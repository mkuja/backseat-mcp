# Backseat Champions Game API: reference for the MCP server

Source: the `GameAPI` folder shipped with the game (`README.md`, `bc_api.py`, `gameapi_client.py`, `example.py`,
`rust/src/lib.rs`, `rust/examples/listen.rs`, `schema/race-results/*.schema.json`, `testdata/*.bin`).
Captured packets are from game version 0.86.2.

Conventions in this document:

- **[README]** = stated in the spec. **[code]** = only visible in `bc_api.py` / `gameapi_client.py` / `lib.rs`.
  **[capture]** = observed in `testdata/`. **[inferred]** = my reading, not stated anywhere; treat as unverified.
- "Unclear" means the sources do not say. **[observed 0.88.2]** = seen in one race against the live game (0.88.2);
  an observation, not a documented guarantee. Everything else was not tested against a running game.

Known discrepancies between the sources are collected in [section 10](#10-discrepancies-and-open-questions). The most
important one: **AI Observations is 1,432 bytes, not the 1,428 the README table says** (confirmed live in 0.88.2).

---

## 1. Transport overview

### 1.1 Channels

| Channel | Protocol | Default address | Direction | Content |
|---|---|---|---|---|
| Telemetry | UDP | `127.0.0.1:29770` and `[::1]:29770` | game -> configured targets | packets 0-8, 12, 13 (telemetry and events) |
| Commands | UDP | `127.0.0.1:29771` | client -> game | commands 0-9 |
| Replies | UDP | the sender's source address and port | game -> the command's sender | packets 9 (Ack), 10 (Error), 11 (Hello) |
| Race results | HTTP(S) POST | user-configured endpoints | game (host only) -> your server | one JSON (or ZIP) document per race weekend |

- The API is **off by default**. It is switched on in **Settings > Telemetry**. Settings are stored in `game-api.json`
  in the game's persistent data folder. The file's layout is not documented.
- Replies do **not** arrive on 29770. They go to whatever socket sent the command. A client therefore needs two
  sockets: one bound to the telemetry port, one (ephemeral) for commands and their replies.
- Telemetry targets may be a unicast address, a LAN broadcast address (`255.255.255.255`, `192.168.1.255`), an
  external IP or a hostname. Hostnames are resolved when settings change and again every 5 minutes; a name with both
  A and AAAA records gets the IPv4 one. The Telemetry tab's status line reports an unresolvable name.
- The command listen IP can be changed from loopback (`0.0.0.0`, `::`), which opens the command port to other
  machines, subject to the Command Token rule below.
- **No telemetry is sent outside a session** (menus, lobby). Whether commands are answered outside a session is
  only partly specified: seat commands return `no_player` ("no session, or no player in that seat"); behaviour of
  `ping`/`hello` outside a session is unclear (replies "carry the normal header", whose session fields would
  presumably be zero).

### 1.2 Wire format

- Little-endian, packed, no padding.
- Every packet type has a fixed size; a parser can and should check the datagram length before reading.
- Largest packet is at most 1,452 bytes, so no IP fragmentation.

| Type | Bytes | Notes |
|---|---|---|
| `uint8`, `int8` | 1 | |
| `uint16` | 2 | |
| `uint32` | 4 | |
| `float` | 4 | IEEE 754 single |
| `char[N]` | N | UTF-8, zero-terminated, zero-padded to N |
| `bool` | 1 | 0 or 1 |

Flags are bits of a `uint8`; bit 0 is the least significant.

### 1.3 Settings switches

Broadcast switches (reported in `Hello.broadcast_flags`):

| Switch | Hello bit | Enables |
|---|---|---|
| Driver Data | 0 | Motion (0), Car Telemetry (1), Lap Data (2), Car Status (3), Participants (6), Racing Line (13); Car Abilities (12) together with or instead of Player Data |
| Player Data | 1 | Player (4); Car Abilities (12) together with or instead of Driver Data |
| Decision Events | 2 | Event (7) |
| AI Observations | 3 | AI Observations (8). Off by default, about 1.1 MB/s |
| (any of the above) | | Session (5) is sent when "any broadcast option" is on |

Input switches (reported in `Hello.api_flags` and `Session.api_flags`):

| Switch | Flag bit | Enables |
|---|---|---|
| Player Input | 0 "player input active" | `pick_card`, `set_pit`, `pick_tire`, `ban_card`, `boost`, `set_camera` |
| Driver Input | 1 "driver input active" | `drive`, `release_drive` |
| (not a switch) | 2 "public lobby" | set by the game when the lobby counts as public |

The flag is described as "active", not "enabled". Whether bits 0/1 are cleared while in a public lobby is unclear.

Other settings named in the README: telemetry targets, send rate, listen IP/port, **Command Token** (under Input),
Race Results endpoints with optional token and **Send as ZIP**.

### 1.4 Rates

| Rate | Packets |
|---|---|
| Configured rate, 1 to 50 Hz (50 Hz is the physics rate) | Motion, Car Telemetry, Lap Data, Car Abilities; Player and Racing Line (one per local player); AI Observations (one per car) |
| Twice a second | Car Status, Session (Session also when a session starts) |
| Every 5 seconds | Participants (also when a session starts) |
| When it happens | Event |
| On request | Ack, Error, Hello |

Packets from one telemetry tick share the header's `frame`.

Bandwidth [README]: at 50 Hz, 16 cars, one local player, Driver Data + Player Data is about 205 KB/s (1.6 Mbit/s)
in 6 datagrams per tick. AI Observations add 16 datagrams per tick, about 1.1 MB/s.

### 1.5 Command Token rules

- Every command has a 48-byte `token` field (`char[48]`, so at most 47 bytes of UTF-8 plus the terminator).
- From **loopback**: no token needed; the field may be all zero.
- From **another machine**: every command must carry the Command Token set in the Telemetry tab. The token must be
  at least 12 and at most 47 UTF-8 bytes.
- From another machine, the game **drops silently, with no reply**: a command without the token, with the wrong
  token, anything that cannot be a command, and everything while no token is set. (Reason given: a forged sender
  address must never get a reply.) So "no reply" from a remote game can mean wrong token, not only "game not running".
- `invalid_packet` errors are only sent to another machine when the datagram carries the token.
- The token is sent in plain text.
- Both client libraries refuse to build a command with a token over 47 bytes (`ValueError` / `TokenTooLong`).
  Neither enforces the 12-byte minimum.

### 1.6 Seat rule

"Input only ever controls your own car." `seat` must be **0** (player one, the player running the API). Any other
seat is refused with `not_your_car`. Splitscreen guests, other online players and AI cars cannot be controlled.

Telemetry is not restricted this way: Player and Racing Line are sent for every local seat (up to four in
splitscreen), and events carry the seat they concern.

### 1.7 Public-lobby rule

A lobby is public when it is listed in the lobby browser, was found through matchmaking, or has any member who is
not your Steam friend (for example someone who joined by invite code).

In a public lobby, on the host and every client:

- Every command except `ping`, `hello` and `set_camera` is refused with `public_lobby`.
- A car currently being driven or boosted through the API is handed back at once.
- Telemetry and events still go out.
- `Session.api_flags` bit 2 and `Hello.api_flags` bit 2 report the state.

### 1.8 Timeouts and hold semantics

| Command | Hold rule |
|---|---|
| `drive` | Holds the car only while commands keep arriving. After **0.25 s** without one the built-in driver takes over. Send at the physics rate (50 Hz). `release_drive` hands back immediately. |
| `boost` | Holds the buttons only while commands keep arriving. After **0.25 s** without one, or on a `boost` with no buttons, they are released. Send at the physics rate. |

Everything else is a one-shot command.

### 1.9 Versioning rules

- `packet_format` (1) changes only if the header changes. Drop packets with an unknown format.
- `packet_version` / `command_version` rises when one packet's layout changes; its size changes with it.
- New packet types get new ids, new events new codes. Parsers must skip unknown ids and codes.
- Enum values never change meaning; new ones are appended.
- The meaning of `obs` (AI Observations) follows `policy`; the meaning of `opponents`/`near` (Player) follows
  `feature_schema_hash`. Neither layout is documented in this folder.

---

## 2. Telemetry packets

### 2.0 Common header (24 bytes, every packet the game sends)

Python struct: `<HBB3BBIIf4B`

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | `packet_format` | uint16 | 1 |
| 2 | `packet_id` | uint8 | 0-13, see below |
| 3 | `packet_version` | uint8 | layout version of this packet type: **2 for AI Observations, 1 for all others** |
| 4 | `game_version` | uint8[3] | e.g. 0, 86, 2 (bc_api renders `"0.86.2"`) |
| 7 | `num_cars` | uint8 | car slots in use this session, at most 16 |
| 8 | `session_uid` | uint32 | new for every session; qualifying and the race after it are two sessions |
| 12 | `frame` | uint32 | telemetry tick within the session; packets of one tick share it |
| 16 | `session_time` | float | seconds since the session's cars were placed on the grid |
| 20 | `seat_car` | uint8[4] | car index of each local player; seat 0 is player one; 255 = no such player |

A car's **index** is its position in the session's participant list, from 0. [README] says it stays the same for
the whole session. **[observed 0.88.2]** It does not: mid-race the participant list and `seat_car` were re-ordered
by race position (the player's index went 3, 4, 6, 8, 9, 11, always position - 1), while `player_id` stayed the same.
Within one packet the indices agree with that packet's own `seat_car`; do not carry an index from one packet to a
later one. Every per-car array has 16 slots; slots from `num_cars` on are zero. Both decoders return only the first
`num_cars` entries.

To find "my car" in any per-car packet: `cars[header.seat_car[0]]`, after checking it is not 255 and is below the
array length.

### 2.1 Packet index

| id | Name (bc_api) | Total bytes | Body | Rate | Needs |
|---|---|---|---|---|---|
| 0 | `motion` | 792 | 16 x 48 | configured | Driver Data |
| 1 | `car_telemetry` | 952 | 16 x 58 | configured | Driver Data |
| 2 | `lap_data` | 312 | 16 x 18 | configured | Driver Data |
| 3 | `car_status` | 536 | 16 x 32 | 2 Hz | Driver Data |
| 4 | `player` | 1,039 | 1,015 | configured, one per local player | Player Data |
| 5 | `session` | 123 | 99 | 2 Hz and at session start | any broadcast option |
| 6 | `participants` | 584 | 16 x 35 | every 5 s and at session start | Driver Data |
| 7 | `event` | 47 | 23 | on occurrence | Decision Events |
| 8 | `ai_observations` | **1,432** | 1,408 | configured, one per car, host only | AI Observations |
| 9 | `ack` | 29 | 5 | reply | |
| 10 | `error` | 30 | 6 | reply | |
| 11 | `hello` | 53 | 29 | reply to `hello` | |
| 12 | `car_abilities` | 200 | 16 x 11 | configured | Driver Data or Player Data |
| 13 | `racing_line` | 747 | 723 | configured, one per local player | Driver Data |

### 2.2 Packet 0: Motion

Per car, 48 bytes (`<12f`).

| Field | Type | Unit / range | Meaning |
|---|---|---|---|
| `position[3]` | float x3 | metres, world space | Car position. Axis convention of the world frame is not stated; captures show index 1 behaving as height (consistent with the car frame's "y up"). |
| `velocity[3]` | float x3 | m/s, car's own frame: x right, y up, z forward | `velocity[2]` is forward speed |
| `angular_velocity[3]` | float x3 | rad/s, car's own frame | |
| `pitch` | float | degrees, -180 to 180 | |
| `roll` | float | degrees (range not stated) | |
| `yaw` | float | degrees (range and zero direction not stated) | |

### 2.3 Packet 1: Car Telemetry

Per car, 58 bytes (`<3f` + 4 x `Bff` + `BBff`).

| Field | Type | Unit / range | Meaning |
|---|---|---|---|
| `speed_kmh` | float | km/h | |
| `pedal` | float | -1 full brake to 1 full throttle | as applied to the car |
| `steer` | float | -1 left to 1 right | as applied |
| `wheels[4]` | Wheel x4 | | two front wheels, then two rear. Left/right order within a pair is not stated. |
| `tire_compound` | uint8 | Compounds enum | |
| `tire_flags` | uint8 | bit 0: puncture | |
| `tire_health` | float | 1 new to 0 worn out | |
| `energy` | float | 0 to 1 | boost or fuel left; `Session.fuel` says which |

Wheel (9 bytes):

| Field | Type | Meaning |
|---|---|---|
| `flags` | uint8 | bit 0: within track limits (`on_track`); bit 1: touching the ground (`grounded`) |
| `slip_forward` | float | longitudinal slip; 0 while off the ground. Unit and scale not stated. |
| `slip_sideways` | float | lateral slip. Unit and scale not stated. |

### 2.4 Packet 2: Lap Data

Per car, 18 bytes (`<BBHBBfff`).

| Field | Type | Unit / range | Meaning |
|---|---|---|---|
| `position` | uint8 | from 1 | overall race position |
| `class_position` | uint8 | from 1 | position within the car's class |
| `lap` | uint16 | | laps completed |
| `flags` | uint8 | bits | bit 0 `in_pits`: in the pit lane; bit 1 `finished`; bit 2 `pit_planned`: pit stop called, **this machine's cars only** (a rival's call is never shown); bit 3 `api_driving`: an API client is driving it |
| `pit_stops` | uint8 | | stops made |
| `gap_to_leader` | float | seconds | |
| `interval_ahead` | float | seconds | gap to the car ahead |
| `interval_behind` | float | seconds | gap to the car behind. **[capture] this value is negative in the captured packet** (e.g. -0.7093 where Player reports `class_gap_to_behind` = +0.7093). The sign is not documented. |

### 2.5 Packet 3: Car Status

Per car, 32 bytes (`<8f`): `float performance[8]`, the car's current performance ratio per area in Performance
areas order (Suspension, EngineTorque, Downforce, Acceleration, Drag, AngularDrag, TractionControl, Weight).

Each ratio is the car type's base, raised by upgrades and moved by weather, slipstream, abilities and failures.
Scale and direction (is higher always better, including for Drag and Weight?) are not stated. Captured values range
from about 0.44 to 1.17.

### 2.6 Packet 4: Player

One per local player; 1,015 bytes after the header
(`<BBB17sHHHBBBBf7BfBBHHBBBB4f194f42f`).

| Field | Type | Unit / range | Meaning |
|---|---|---|---|
| `seat` | uint8 | 0-3 | 0 is player one |
| `car` | uint8 | car index | this seat's car |
| `points` | uint8 | | upgrade points available to spend |
| `hand` | Hand (17 bytes) | | the cards on offer |
| `upgrades_applied` | uint16 | | count of upgrades applied so far |
| `total_laps` | uint16 | | |
| `laps_remaining` | uint16 | | |
| `pit_flags` | uint8 | bits | bit 0 `pit_planned`: stop called; bit 1 `can_pit`: a stop can be called now; bit 2 `in_pits` |
| `pit_stops` | uint8 | | stops made |
| `tire_compound` | uint8 | Compounds enum | |
| `tire_flags` | uint8 | bit 0: puncture | |
| `tire_health` | float | 1 new to 0 worn out | |
| `compound_picks_left[7]` | uint8 x7 | per compound, 255 = no limit | how many more times each compound can be picked. bc_api turns 255 into `None`. |
| `energy` | float | 0 to 1 | boost or fuel |
| `has_strategy` | bool | | 0 when this seat has no strategy model; all fields below are then zero. bc_api returns `strategy: None`. |
| `start_grid_position` | uint8 | | |
| `laps_since_pit` | uint16 | | |
| `tire_age_laps` | uint16 | | |
| `cars_within_5s_ahead` | uint8 | | |
| `cars_within_5s_behind` | uint8 | | |
| `class_position` | uint8 | | |
| `class_size` | uint8 | | cars in this class |
| `class_gap_to_leader` | float | seconds [inferred from name] | |
| `class_gap_to_ahead` | float | seconds [inferred] | |
| `class_gap_to_behind` | float | seconds [inferred] | positive in the capture |
| `reference_lap_s` | float | seconds | reference lap time; exact definition not stated |
| `opponents[194]` | float x194 | | the Extreme AI's opponent block, in FeatureSchema order. Layout not documented here. |
| `near[42]` | float x42 | | its near-cars block. Layout not documented here. |

Hand (17 bytes): `uint8 count` (cards in use, at most 4), then `Card cards[4]`. The card's position 0-3 is the
`index` that `pick_card` and `ban_card` take.

Card (4 bytes):

| Field | Type | Meaning |
|---|---|---|
| `upgrade` | uint8 | Upgrades enum |
| `facility` | uint8 | Facilities enum |
| `tier` | uint8 | card tier; scale not stated (0 in all captures) |
| `flags` | uint8 | bit 0: banned |

`opponents` and `near` are "the race state the Extreme AI decides from". Their layout follows the feature schema
named by `feature_schema_version` / `feature_schema_hash` in Session and Hello; when the hash changes, re-check
what the blocks mean. The schema itself is not in this folder, so the MCP server can expose these only as opaque
vectors.

### 2.7 Packet 5: Session

99 bytes after the header (`<BBHBfHI32sBfHfffffffffBff`).

| Field | Type | Unit / range | Meaning |
|---|---|---|---|
| `session_type` | uint8 | Session types enum | |
| `fuel` | bool | | 1: `energy` fields are fuel; 0: boost. bc_api renders `energy: "fuel" | "boost"`. |
| `total_laps` | uint16 | | 0 when the session has no lap count |
| `api_flags` | uint8 | bits | bit 0 player input active, bit 1 driver input active, bit 2 public lobby |
| `weather` | float | -1 clear sky to 1 storm | rain starts above 0 |
| `feature_schema_version` | uint16 | | |
| `feature_schema_hash` | uint32 | | bc_api renders as `0x%08X` |
| `scene` | char[32] | | the track's scene name |
| `has_track` | bool | | 0 when the track has no measured properties; fields below are then zero. bc_api returns `track: None`. |
| `length_km` | float | km | |
| `corner_count` | uint16 | | |
| `avg_corner_speed` | float | unit not stated | |
| `straight_ratio` | float | unit not stated | |
| `elevation_change` | float | unit not stated (metres plausible) | |
| `base_tire_wear_rate` | float | unit not stated | |
| `overtake_difficulty` | float | unit not stated | |
| `pit_lane_time_loss_s` | float | seconds | |
| `width_mean` | float | unit not stated (metres plausible) | |
| `width_min` | float | unit not stated | |
| `barrier_ratio` | float | unit not stated | |
| `drs_zone_count` | uint8 | | |
| `drs_coverage` | float | unit not stated (share of lap plausible) | |
| `lap_time_est_s` | float | seconds | |

### 2.8 Packet 6: Participants

Per car, 35 bytes (`<BBB32s`). Carries every player's display name, other online players' included, which is why
it needs Driver Data.

| Field | Type | Meaning |
|---|---|---|
| `player_id` | uint8 | meaning not stated; in the capture it is a permutation of 0-15 with the local human as 0 |
| `car_type` | uint8 | Car types enum |
| `kind` | uint8 | 0 AI, 1 a player on this machine, 2 a player on another machine (bc_api: `ai`, `local`, `remote`) |
| `name` | char[32] | display name |

### 2.9 Packet 7: Event

23 bytes after the header (`<4sB18s`).

| Field | Type | Meaning |
|---|---|---|
| `code` | char[4] | four-letter code, not zero-terminated |
| `seat` | uint8 | seat the event is about; 255 for the whole session (bc_api: `None`) |
| `details` | uint8[18] | code-specific, zero past what the code uses |

See [section 3](#3-events) for each code.

### 2.10 Packet 8: AI Observations

One per car, and only from the machine that simulates the cars (single player, splitscreen, or the host online).
1,408 bytes after the header (`<BB13s23sIffH339f`). Packet version 2.

| Field | Type | Meaning |
|---|---|---|
| `car` | uint8 | car index |
| `flags` | uint8 | bit 0 `api_driving`: an API client is driving it |
| `policy` | char[13] | the driving net: first 12 hex digits of its source ONNX's SHA-256 |
| `policy_name` | char[23] | e.g. `AllCars.onnx` |
| `obs_frame` | uint32 | advances each time the net decides, roughly every third physics tick |
| `action[2]` | float x2 | pedal and steer the net last chose |
| `obs_count` | uint16 | observations in use; the rest of the array is zero |
| `obs[339]` | float x339 | the net's input vector, in `DriverAIAgent.FillObservations` order. Layout not documented here. |

The observation layout belongs to the net: when `policy` changes, re-check what `obs` means. bc_api returns only
the first `obs_count` values.

### 2.11 Packets 9, 10, 11: Ack, Error, Hello

See [section 4.1](#41-replies).

### 2.12 Packet 12: Car Abilities

Per car, 11 bytes (`<BBBff`).

| Field | Type | Unit / range | Meaning |
|---|---|---|---|
| `active` | uint8 | bits | bit 0 speed boost, bit 1 cornering boost, bit 2 super boost, bit 3 party mode, bit 4 DRS open, bit 5 boost overspeed, bit 6 boost ran empty and is recharging |
| `unlocked` | uint8 | bits | **this machine's cars only**: bit 0 super boost unlocked, bit 1 party mode unlocked, bit 2 DRS unlocked, bit 3 an API client is holding boost buttons (`api_boosting`) |
| `ban_tokens` | uint8 | | **this machine's cars only**: card bans left |
| `slipstream` | float | 0 to 1 | how far into another car's slipstream |
| `recharge_left` | float | 0 to 1 | while recharging, share of the wait still to go: 1 just emptied, 0 ready |

DRS, slipstream and boost overspeed cannot be switched by a player or the API; the game applies them. The packet
only reports them. [capture] a super boost shows bits 0, 1 and 2 set together.

### 2.13 Packet 13: Racing Line

One per local player; 723 bytes after the header: `uint8 seat`, `uint8 car`, `uint8 count`, then 20 x Point.

Point (36 bytes, `<9f`):

| Field | Type | Meaning |
|---|---|---|
| `racing_line[3]` | float x3 | racing line point at that checkpoint; world position in metres, same frame as Motion |
| `left[3]` | float x3 | the track's left edge at that checkpoint |
| `right[3]` | float x3 | the track's right edge |

The next 20 checkpoints the car is heading for, in driving order starting from the one it is heading for. `count`
is the number in use; the rest are zero. During a pit stop the points follow the pit lane.

---

## 3. Events

All events arrive as packet 7 and need **Decision Events**. Detail offsets are within the 18-byte `details` field.

The README does not prescribe a response to any event. The "answer with" column is taken from what `autopilot` in
`gameapi_client.py` does, plus the command descriptions.

| Code | Fired when | `seat` | Details | Answer with |
|---|---|---|---|---|
| `SSTA` | a session's cars are on the grid | 255 [inferred: "whole session"] | none | nothing. Good moment to reset cached state and send `hello`. |
| `SEND` | the session ends | 255 [inferred] | none | nothing |
| `CARD` | a local player has points and a new hand | the player's seat | `[0] uint8 points`, `[1..17] Hand hand` (count + 4 x Card) | `pick_card(index)` to spend a point, optionally `ban_card(index)` first (a ban keeps the turn) |
| `TIRE` | the tire screen opens: starting grid, qualifying box or pit stop | the player's seat | `[0] bool pre_race` (a race that has not started), `[1] uint8 current` compound, `[2] uint8 allowed` (bit per compound index), `[3..9] uint8 picks_left[7]` (255 = no limit) | `pick_tire(compound)` with a compound whose bit is set in `allowed` |
| `LAPC` | each new lap | the player's seat | `[0..1] uint16 lap`, `[2] uint8 position` | nothing (informational) |
| `PITW` | each new lap | the player's seat | `[0..1] uint16 lap`, `[2] bool pit_planned`, `[3] bool can_pit` | `set_pit(true/false)` if the plan should change and `can_pit` is set |
| `PITI` | entering the pit lane | the player's seat | none | nothing; a `TIRE` event is expected to follow when the tire screen opens |
| `PITO` | leaving the pit lane | the player's seat | none | nothing |
| `FINI` | the player takes the flag | the player's seat | `[0] uint8 position` | nothing |

Notes:

- Events are per local seat, but only seat 0 can be commanded. An event for seat 1-3 (splitscreen guest) cannot be
  answered through the API.
- `CARD` gives the same hand as the Player packet's `hand`, so a client without Decision Events can poll
  `player.points > 0` instead. Likewise `PITW` duplicates `player.pit_flags`. There is no polling equivalent for
  `TIRE`'s `allowed` mask or `pre_race`: the tire screen being open is only signalled by the event.
- How long the tire screen stays open, and what the game picks if nobody answers, is not stated.
- Whether `CARD` is re-sent after each pick while points remain is not stated explicitly ("has points and a new
  hand" suggests one event per new hand).
- Unknown codes must be skipped. The Rust crate maps them to `Event::Other { code, seat }`.

---

## 4. Commands

### 4.0 Command header (56 bytes)

Python struct: `<HBBI48s`

| Offset | Field | Type | Meaning |
|---|---|---|---|
| 0 | `packet_format` | uint16 | 1 |
| 2 | `command_id` | uint8 | 0-9 |
| 3 | `command_version` | uint8 | 1 |
| 4 | `request_id` | uint32 | any number; the reply echoes it |
| 8 | `token` | char[48] | Command Token, or all zero from loopback |

The payload follows directly. The datagram must be exactly the size listed for its command.

### 4.1 Replies

Every command is answered with **Ack** or **Error**, except:

- `hello` is answered with **Hello** (or an Error),
- `drive` only answers errors (no Ack on success),
- `boost` only answers errors, "like `drive`".

Replies carry the normal 24-byte header (so they include `session_uid`, `frame`, `seat_car` at the moment of reply).

Ack (packet 9, 5 bytes after header, `<BI`):

| Field | Type | Meaning |
|---|---|---|
| `command_id` | uint8 | the command acknowledged |
| `request_id` | uint32 | echoed |

Error (packet 10, 6 bytes after header, `<BIB`):

| Field | Type | Meaning |
|---|---|---|
| `command_id` | uint8 | 255 when the command was too short to have one |
| `request_id` | uint32 | echoed |
| `reason` | uint8 | Error reasons enum |

Hello (packet 11, 29 bytes after header, `<I16sBBBHI`):

| Field | Type | Meaning |
|---|---|---|
| `request_id` | uint32 | echoed |
| `game_version` | char[16] | e.g. `"0.86.2"` |
| `api_flags` | uint8 | bit 0 player input active, bit 1 driver input active, bit 2 public lobby |
| `broadcast_flags` | uint8 | bit 0 Driver Data, bit 1 Player Data, bit 2 Decision Events, bit 3 AI Observations |
| `rate_hz` | uint8 | configured send rate |
| `feature_schema_version` | uint16 | |
| `feature_schema_hash` | uint32 | |

"Send `hello` first: the reply says what is switched on."

### 4.2 Errors common to all commands

The README gives one global error table, not a per-command matrix. The per-command lists below are therefore
**[inferred]** from each reason's description, except where marked [README].

Errors any command can plausibly return:

| Reason | When |
|---|---|
| `invalid_packet` (1) | too short, or the wrong size for its command |
| `unsupported_version` (2) | unknown `packet_format` or `command_version` |
| `unknown_command` (3) | `command_id` above 9 |
| `internal_error` (20) | unspecified game-side failure |

Errors any seat-taking command (ids 2-9) can plausibly return:

| Reason | When |
|---|---|
| `public_lobby` (4) | [README] every command except `ping`, `hello`, `set_camera` |
| `input_disabled` (5) | the input switch the command needs is off |
| `not_your_car` (6) | [README] `seat` is not 0 |
| `no_player` (7) | no session, or no player in that seat |
| `replay_playing` (8) | a replay is playing (which commands check this is not stated) |

The order in which the game checks these is not documented.

### 4.3 Command table

| id | Name | Payload | Total bytes | Needs | Success reply |
|---|---|---|---|---|---|
| 0 | `ping` | none | 56 | | Ack |
| 1 | `hello` | none | 56 | | Hello |
| 2 | `pick_card` | `uint8 seat`, `uint8 index` | 58 | Player Input | Ack |
| 3 | `set_pit` | `uint8 seat`, `bool pit` | 58 | Player Input | Ack |
| 4 | `pick_tire` | `uint8 seat`, `uint8 compound` | 58 | Player Input, tire screen open | Ack |
| 5 | `drive` | `uint8 seat`, `float pedal`, `float steer` | 65 | Driver Input, this machine simulates the car | none |
| 6 | `release_drive` | `uint8 seat` | 57 | Driver Input | Ack |
| 7 | `ban_card` | `uint8 seat`, `uint8 index` | 58 | Player Input | Ack |
| 8 | `boost` | `uint8 seat`, `uint8 buttons` | 58 | Player Input | none |
| 9 | `set_camera` | `uint8 seat`, `uint8 camera` | 58 | Player Input | Ack |

"Commands go through the same calls as a press on the player's own screen, so every pick rule applies: a refused
compound, a banned card or a pit call the car can't make comes back as an error."

### 4.4 Per-command detail

[inferred] This section is my reading of the README per command. Error lists, prerequisites and side effects are
inferred unless marked [README], [capture] or [observed 0.88.2]; the README has no per-command error matrix.

#### 0 `ping`

- Payload: none. 56 bytes.
- Prerequisites: none. Allowed in a public lobby.
- Reply: Ack.
- Errors: common set only.
- Use: liveness check, token check from a remote machine.

#### 1 `hello`

- Payload: none. 56 bytes.
- Prerequisites: none. Allowed in a public lobby.
- Reply: Hello.
- Errors: common set only.
- Use: discover game version, which broadcast and input switches are on, the send rate and the feature schema.

#### 2 `pick_card`

- Payload: `seat` (must be 0), `index` 0-3 into the current hand (as shown by the latest `CARD` event or Player
  packet).
- Prerequisites: Player Input; a session with a player in seat 0; upgrade points available; a hand on offer.
- Reply: Ack.
- Specific errors [inferred from names unless marked]:
  - `no_points` (9): no upgrade points to spend.
  - `invalid_index` (10): index outside the hand. [capture: `pick_card` with a bad index returned this; `validate`
    sends index 99 and expects an Error.]
  - `cards_unavailable` (11): no hand on offer right now [inferred]. **[observed 0.88.2]** also returned for a hand
    offered while the car was in the pit lane; the same pick succeeded seconds later.
  - `card_banned` (12): [README] "a banned card ... comes back as an error".
  - `driver_decided` (13): reserved, never sent.
- Side effects: spends one upgrade point and applies the card's upgrade. A pick ends the turn for that hand
  (contrast with `ban_card`). The card with upgrade `Reroll` (26) presumably redraws the hand; not stated.

#### 3 `set_pit`

- Payload: `seat` (0), `pit` (1 call a stop, 0 cancel the call).
- Prerequisites: Player Input; `can_pit` (Player `pit_flags` bit 1 / `PITW.can_pit`) should be set.
- Reply: Ack.
- Specific errors: `refused` (14): [README] "the game would not make the pit call".
- Side effects: sets or clears the pit call; visible afterwards as `pit_planned` in Player, Lap Data (bit 2) and
  the next `PITW`. The car then enters the pits (`PITI`), the tire screen opens (`TIRE`), and it leaves (`PITO`).
  Exactly when a called stop is taken (this lap or next) is not stated.

#### 4 `pick_tire`

- Payload: `seat` (0), `compound` (Compounds index 0-6).
- Prerequisites: Player Input; the tire screen must be open (after a `TIRE` event: starting grid, qualifying box
  or pit stop).
- Reply: Ack.
- Specific errors:
  - `invalid_compound` (15): not a compound index.
  - `tire_select_closed` (16): the tire screen is not open.
  - `compound_refused` (17): [README] "that compound can't be picked now" (not in `allowed`, or no picks left).
- Side effects: fits that compound; presumably decrements that compound's `picks_left`. Whether it also closes the
  tire screen is not stated.

#### 5 `drive`

- Payload: `seat` (0), `pedal` float (-1 full brake to 1 full throttle), `steer` float (-1 left to 1 right).
  65 bytes.
- Prerequisites: Driver Input; this machine must simulate the car: single player, splitscreen, or a private online
  lobby you host.
- Reply: **only on error**. Success is silent; confirm through Lap Data `flags` bit 3 (`api_driving`) or AI
  Observations `flags` bit 0.
- Specific errors:
  - `not_host` (18): [README] an online client gets this.
  - `invalid_axis` (19): [README] `pedal` or `steer` is not a finite number. What happens to finite values outside
    -1..1 (clamped or refused) is not stated.
- Semantics: holds the car only while commands keep arriving; after 0.25 s without one the built-in driver takes
  over. Send at 50 Hz.
- Side effects: **achievements, Steam stats and leaderboard submissions are withheld for any race weekend in which
  `drive` steered a car.** A race that follows qualifying on the same track is the same weekend, so driving in
  qualifying also withholds the race after it. This is not reversible by `release_drive`.
- In a public lobby a driven car is handed back at once.

#### 6 `release_drive`

- Payload: `seat` (0). 57 bytes.
- Prerequisites: Driver Input.
- Reply: Ack.
- Specific errors: none beyond the common sets (`not_host` plausible; not stated).
- Semantics: hands the car back to the built-in driver immediately rather than after the 0.25 s timeout. Whether it
  errors when the car was not being driven is not stated.

#### 7 `ban_card`

- Payload: `seat` (0), `index` 0-3 into the hand.
- Prerequisites: Player Input; a hand on offer; at least one ban token (Car Abilities `ban_tokens`).
- Reply: Ack.
- Specific errors:
  - `ban_refused` (21): [README] no ban tokens left, the last card left to pick, or a card that can't be banned.
  - `invalid_index` (10), `cards_unavailable` (11): plausible, not stated.
- Semantics: it is the card screen's own ban. It spends a ban token and cannot ban the last pickable card.
  **Unlike a pick, it keeps your turn**: after a ban the hand is still on offer and a `pick_card` is still due.
  The banned card then shows `flags` bit 0 in the hand. Whether a banned card is replaced, or is excluded from
  future hands, is not stated.

#### 8 `boost`

- Payload: `seat` (0), `buttons` bitmask: bit 0 (1) speed boost, bit 1 (2) cornering boost, bit 2 (4) party mode.
- Prerequisites: Player Input. Does **not** need Driver Input. [README] "does not count as driver input".
  [inferred] It therefore does not trigger the achievement/leaderboard withholding, which the README states only
  for `drive`.
- Reply: **only on error**.
- Specific errors: `assist_on` (22): [README] a Boost Assist (or Party Mode Assist) that is switched on boosts for
  the player and keeps those buttons.
- Semantics:
  - Holds the buttons as the player's own presses would, only while commands keep arriving. After 0.25 s without
    one, or on a `boost` with `buttons = 0`, they are released. Send at the physics rate while boosting.
  - Speed + cornering together (buttons = 3) become a **super boost** once the car has unlocked it (Car Abilities
    `unlocked` bit 0). Before the unlock, what holding both does is not stated.
  - **Party mode exists only in fuel races** (`Session.fuel` = 1), once unlocked (`unlocked` bit 1).
  - Boost draws on `energy`. When it runs empty the car enters `recharging` (`active` bit 6) and
    `recharge_left` counts 1 -> 0. Whether boost buttons are ignored or refused during recharge is not stated.
  - Confirmation: Car Abilities `unlocked` bit 3 (`api_boosting`) and the `active` bits.
- In a public lobby a boosted car is handed back at once.

#### 9 `set_camera`

- Payload: `seat` (0), `camera`: a Camera types index, or 253 previous, 254 next, 255 automatic.
- Prerequisites: Player Input (per the command table). **Allowed in a public lobby**, as it only changes your own
  view.
- Reply: Ack.
- Specific errors: `camera_unavailable` (23): [README] not a camera this race offers. Today the camera button
  cycles through all types except TrackOverview (0), so 0 is refused.
- Semantics: 253 and 254 work like the camera button. While your view follows another car, the first press brings
  it back to your own car in the same camera type, and only the next one cycles. 255 "automatic" is not described
  further.
- There is no telemetry field reporting the current camera.

---

## 5. Enum tables

Values never change meaning; new ones are appended. Both decoders pass through unknown values (bc_api returns the
raw integer, the Rust `name()` returns `"?"`).

**Compounds**

| 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| Standard | Boost | Experimental | Fast | Durable | Wet | Golden |

**Upgrades**

| # | Name | # | Name | # | Name |
|---|---|---|---|---|---|
| 0 | EnginePower | 9 | WeightReduction | 18 | FuelCapacity |
| 1 | DragReduction | 10 | BoostPerSector | 19 | FuelEfficiency |
| 2 | TireGrip | 11 | BoostRechargeSpeed | 20 | FuelPerformance |
| 3 | TemporaryUpgradeDuration | 12 | ExperienceGain | 21 | RefuelSpeed |
| 4 | TractionControl | 13 | SensorQuality | 22 | EngineBraking |
| 5 | Downforce | 14 | TurboCharger | 23 | Gearbox |
| 6 | TireSaving | 15 | Suspension | 24 | FuelWeight |
| 7 | PitSpeed | 16 | Cooling | 25 | KineticRegen |
| 8 | BoostCapacity | 17 | BoostOverspeed | 26 | Reroll |

What each upgrade does is not described in this folder.

**Facilities**: 0 Speed, 1 Cornering, 2 Performance, 3 Boost, 4 Fuel, 5 All, 6 Redline, 7 Augment

**Session types**: 0 AITraining, 1 Practice, 2 Qualifying, 3 Race

**Car types**: 0 Formula, 1 ClassicFormula, 2 ModernFormula, 3 Endurance, 4 GT2, 5 Stock, 6 FormulaOldskool,
7 FormulaLegacy

**Performance areas**: 0 Suspension, 1 EngineTorque, 2 Downforce, 3 Acceleration, 4 Drag, 5 AngularDrag,
6 TractionControl, 7 Weight

**Camera types**: 0 TrackOverview, 1 Helicopter, 2 TrackSide, 3 Drone, 4 Onboard_TCam, 5 Onboard_Rear,
6 Onboard_Front, 7 Onboard_Driver, 8 Onboard_TVAngle, 9 Chase, 10 Onboard_BattleCam.
`set_camera` specials: 253 previous, 254 next, 255 automatic.

**Participant kinds**: 0 AI (`ai`), 1 a player on this machine (`local`), 2 a player on another machine (`remote`)

**Boost buttons** (bitmask): 1 speed, 2 cornering, 4 party mode

**Error reasons**

| # | Name | Note |
|---|---|---|
| 0 | `none` | [code] placeholder in both libraries; not in the README table |
| 1 | `invalid_packet` | too short, or the wrong size for its command; from another machine only answered when it carries the token |
| 2 | `unsupported_version` | `packet_format` or `command_version` this game does not speak |
| 3 | `unknown_command` | |
| 4 | `public_lobby` | |
| 5 | `input_disabled` | the input this command needs is switched off |
| 6 | `not_your_car` | `seat` is not 0 |
| 7 | `no_player` | no session, or no player in that seat |
| 8 | `replay_playing` | |
| 9 | `no_points` | |
| 10 | `invalid_index` | |
| 11 | `cards_unavailable` | |
| 12 | `card_banned` | |
| 13 | `driver_decided` | reserved, never sent |
| 14 | `refused` | the game would not make the pit call |
| 15 | `invalid_compound` | |
| 16 | `tire_select_closed` | |
| 17 | `compound_refused` | that compound can't be picked now |
| 18 | `not_host` | |
| 19 | `invalid_axis` | `pedal` or `steer` is not a finite number |
| 20 | `internal_error` | |
| 21 | `ban_refused` | no ban tokens left, the last card left to pick, or a card that can't be banned |
| 22 | `assist_on` | an assist boosts for the player, so the buttons are its |
| 23 | `camera_unavailable` | not a camera this race offers |

**Event codes**: `SSTA`, `SEND`, `CARD`, `TIRE`, `LAPC`, `PITW`, `PITI`, `PITO`, `FINI`

---

## 6. Race results HTTP upload

### 6.1 When and where

- Only when **you host**. Every finished race weekend is uploaded to each endpoint listed under **Race Results**
  in the Telemetry tab.
- Independent of Gameplay > "Upload Race Results" (that one controls the official analytics server only).
- **Nothing is retried and nothing is kept on disk.** A missed upload is lost.
- Endpoints must be `https`. Plain `http` is accepted only for this machine (`localhost`, `127.0.0.1`, `::1`) or
  private addresses (`10.x`, `172.16-31.x`, `192.168.x`, link-local, IPv6 unique-local).
- Redirects are not followed.

### 6.2 Request

One `POST` per weekend per endpoint.

| Part | Value |
|---|---|
| Body | the results document, `Content-Type: application/json`, at most 16 MB. With **Send as ZIP**: `application/zip`, a ZIP whose only entry is `results.json` |
| `Authorization` | `Bearer <token>`, when the endpoint has a token configured |
| `X-BC-Format` | `2` (the document's `format`) |
| `X-BC-Game-Version` | game version |
| `X-BC-Unity-Version` | engine version |
| `X-BC-Schema-Version` | Extreme AI FeatureSchema version |
| `X-BC-Schema-Hash` | FeatureSchema hash as an unsigned decimal |
| `X-BC-Timepoint` | when the weekend's first session started, Unix milliseconds |
| `X-BC-Session-Count` | sessions in the document |
| `X-BC-Editor`, `X-BC-Demo` | `true` or `false` |

Response: any 2xx accepts. Anything else is logged in the game and the upload is dropped.

### 6.3 Document (`results.schema.json`, JSON Schema draft 2020-12)

All listed fields are required. New fields may be added at any time; consumers should ignore unknown fields.

| Field | Type | Meaning |
|---|---|---|
| `format` | const 2 | raised only when an older consumer would misread the document |
| `game_version` | string | |
| `unity_version` | string | |
| `schema_version` | integer | `FeatureSchema.SCHEMA_VERSION`: layout of every decision's `state` and `offered` vectors |
| `schema_hash` | integer >= 0 | `FeatureSchema.SchemaHash` as an unsigned decimal (same value as the UDP `feature_schema_hash`) |
| `timepoint` | integer | when the weekend's first session started, Unix ms |
| `editor` | boolean | |
| `demo` | boolean | |
| `public_lobby` | boolean | the lobby was public at a session start or end; every `steam_id` in the classification is then null |
| `host_steam_id` | string of digits, or null | host's SteamID64 as a decimal string; null when Steam was not running. Sent in public lobbies too. |
| `sessions` | array of session | in the order they ran: qualifying first when the weekend had one, then the race |

**session**

| Field | Type | Meaning |
|---|---|---|
| `kind` | `"qualifying"` or `"race"` | |
| `track` | string | the track's scene name (same as UDP `Session.scene`) |
| `started_at` | integer or null | Unix ms |
| `ended_at` | integer | Unix ms, when the classification was final |
| `classification` | array of car | every car, in finishing order (qualifying: by best lap) |
| `race_id` | string or null | the decision log's race id; null when no car logged a decision this session |
| `header` | header object or null | the decision log's header line; null when no car logged a decision |
| `decisions` | array of decision | card picks, pit calls and tire choices made by the AI and by assists; empty when none. **A pick a player makes by hand is not logged.** Whether picks made through the API count as "by hand" is not stated. |

**car** (classification entry)

| Field | Type | Meaning |
|---|---|---|
| `position` | integer >= 1 | |
| `class_position` | integer >= 1 | |
| `car_id` | integer | the `car_id` its decisions carry. Relation to the UDP car index is not stated. |
| `car_number` | integer | |
| `car_class` | string | the car type's name, e.g. `FormulaCar` (note: not the UDP enum spelling `Formula`) |
| `name` | string | sent in public lobbies too |
| `driver` | `"human"` or `"ai"` | |
| `steam_id` | string of digits, or null | SteamID64 as a decimal string. Null for AI, in a public lobby, or when unknown. Splitscreen players share their machine's id. |
| `local_seat` | integer >= 0, or null | seat on the player's own machine: 0 main player, 1+ splitscreen guests; null for AI |
| `grid` | integer >= 1, or null | starting position; null in qualifying and when the grid was not set by qualifying |
| `laps` | integer >= 0 | |
| `gap_to_leader_s` | number >= 0, or null | seconds behind the leader (qualifying: behind pole on best lap) |
| `best_lap_s` | number >= 0, or null | null when the car set no lap time |

### 6.4 Decision log header (`header.schema.json`)

One per session that logged a decision. All fields required.

| Field | Type | Meaning |
|---|---|---|
| `type` | const `"header"` | |
| `schema_version` | integer | `FeatureSchema.SCHEMA_VERSION` |
| `schema_hash` | integer >= 0 | unsigned decimal; "the API's hello and session_start give the same value in hex" |
| `run_id` | string | not described |
| `race_id` | string | unique per session; matches the session's `race_id` |
| `game_version` | string | |
| `unity_version` | string | |
| `timestamp_utc` | string | ISO 8601, when the log opened |
| `seed` | integer | not described |
| `car_type_setting` | integer | the race's car class setting, as its enum number |
| `ai_difficulty` | integer | the AI difficulty setting, as its enum number (enum not given) |
| `build` | string | `retail`, `editor` or `training` |
| `demo` | boolean | |
| `model_bundle` | string | the Extreme AI model bundle that was loaded; empty for the built-in one |
| `log_rev` | integer | bumped when the line format itself changes |
| `context` | array of number | race context features in FeatureSchema order |

### 6.5 Decision (`decision.schema.json`)

"One strategy decision a car made: a card pick, a pit call or a tire choice, with how its race ended." All fields
required.

| Field | Type | Meaning |
|---|---|---|
| `type` | const `"decision"` | |
| `car_id` | integer | matches the classification's `car_id` |
| `car_type` | integer | the car's class as its enum number; -1 when unknown |
| `decision_idx` | integer | not described (presumably a running index) |
| `decision_type` | string | `card`, `pit` or `tire` |
| `lap` | integer | |
| `race_time_s` | number | |
| `state` | array of number | race state features in FeatureSchema order (`header.schema_version`) |
| `offered` | array of array of number | one feature vector per option the car could choose from |
| `chosen` | integer | index into `offered` |
| `chosen_by` | string | `ml_agent` or `hard_ai` |
| `policy` | string | which policy ran the seat, for reports |
| `outcome` | object | how the car's session ended, see below |

`outcome` (all required): `finish_position` integer, `class_position` integer, `class_size` integer,
`finish_score` number (not described), `race_duration_s` number.

---

## 7. Insights from `gameapi_client.py`, `bc_api.py` and `lib.rs`

### 7.1 Sockets

- **No socket options are set anywhere.** Neither Python file nor the Rust example sets `SO_REUSEADDR`,
  `SO_REUSEPORT` or `SO_BROADCAST`. Telemetry sockets are a plain `bind(("127.0.0.1", 29770))`.
  [inferred] Two listeners therefore cannot normally share port 29770 on one machine: the MCP server and
  `gameapi_client.py listen` would conflict unless the MCP server sets reuse options itself or a second target
  port is configured in the game.
- Telemetry socket: `AF_INET` bound to `127.0.0.1` (or `AF_INET6` on `::1` with `--ipv6`), timeout 0.2 s, receive
  buffer argument 2048 bytes (enough for the 1,432-byte maximum).
- Command socket: UDP, **`connect()`ed** to `(host, command_port)` up front, timeout 0.5 s. The code comment gives
  two reasons: the OS then drops datagrams from anyone but the game, and Windows refuses a receive on a socket that
  has not been bound. Family is IPv6 if the host string contains `:`.
- `receive()` catches `ConnectionResetError` alongside timeouts. [inferred] On Windows a UDP send to a closed port
  surfaces as a connection reset on the next receive, so this is the "game not running" case.
- Remote telemetry hardening: with a non-loopback `--listen-ip`, telemetry is accepted only from the resolved
  addresses of `--game-ip` (default `--host`). IPv4-mapped (`::ffff:`) prefixes and zone ids are stripped before
  comparing. The stated reason: otherwise anyone on the network could feed `autopilot` a fake event and make it
  send a command with the token attached. An MCP server that accepts remote telemetry should do the same.

### 7.2 Request ids and reply matching

- `bc_api` command builders default `request_id` to 0.
- `send` (one-shot) uses request id 1, waits up to 0.5 s and takes the **first well-formed packet** on the
  connected command socket as the reply. It does not compare `request_id`. Exit code 0 only for `ack` or `hello`.
  For `drive` (silent on success) it prints "no reply" and exits 1.
- `validate` is the only mode that matches: it stores replies in a dict keyed by `request_id`.
- `autopilot` increments a counter for every seat event, including ones it does not answer, sends without
  waiting, and merely prints whatever replies it later drains from the command socket. It never retries.
- `drive` and `boost` send with the default request id 0 and print any reply (which can only be an error).
- Because all replies come back on the command socket, an MCP server should use a fresh `request_id` per command
  and match on `(command_id, request_id)`, with a timeout meaning either "silent success" (`drive`, `boost`) or
  "dropped / game not running".

### 7.3 `autopilot` decision logic

Deliberately trivial; it is a demo, not a strategy.

- State kept: `health[seat] = tire_health` from every Player packet. So it needs **Player Data** as well as
  Decision Events; without Player packets health defaults to 1.0 and it never pits.
- Ignores events whose seat is 255. It answers events for **any** seat with that seat number; for seats 1-3 the
  game would reply `not_your_car`.
- `CARD`: picks the **first card in the hand that is not banned** (lowest index). Never bans. Sends nothing if all
  are banned.
- `TIRE`: picks the **first allowed compound** (lowest enum index with its `allowed` bit set). Ignores `picks_left`,
  `current`, `pre_race` and weather.
- `PITW`: only when `can_pit`. `want = tire_health < --pit-below` (default **0.35**). Sends `set_pit(want)` only if
  `want != pit_planned`. Consequence: it also **cancels** a stop the player called by hand whenever health is above
  the threshold.
- `LAPC`, `PITI`, `PITO`, `FINI`: ignored.

### 7.4 `drive` and `boost` modes

- Loop: send the command, drain replies, drain telemetry, `sleep(0.02)` (50 Hz), until `--seconds` elapse; then
  send `release_drive` (or `boost` with no buttons).
- Defaults: drive 5 s, pedal 1.0, steer 0.0; boost 3 s. Both accept `--seat` but only 0 can work.
- Confirmation method: `drive` counts Lap Data frames by `api_driving` (`api` vs `builtin`); `boost` counts Car
  Abilities frames by the set of active flags. Progress is printed every 25th frame.
- The `send` subcommand has no `boost` word; `boost` exists only as its own subcommand. `send drive 1 0` sends a
  single packet, which holds the car for at most 0.25 s.

### 7.5 `validate`: probes and sanity ranges

Probes, re-sent every 2 s until all three have been answered:

| Request id | Command | Expected reply |
|---|---|---|
| 1 | `hello` | Hello (printed) |
| 2 | `ping` | Ack |
| 3 | `pick_card` index 99 | Error (any reason) |

Checks applied to every decoded packet (these are the ranges the game's authors consider "sensible"):

| Scope | Check |
|---|---|
| all | `packet_version == PACKET_VERSIONS[packet_id]` |
| all | `num_cars <= 16` |
| all | every `seat_car[i]` is 255 or `< num_cars` |
| `lap_data` | the sorted `position` values are exactly 1..num_cars (unique, no gaps) |
| `participants` | every car has a non-empty name |
| `car_telemetry` | `-1.01 <= pedal <= 1.01`; `0 <= tire_health <= 1.0001` |
| `event` | code is one of the nine known codes |
| `ai_observations` | `len(obs)` is 0 or 339 |
| `car_abilities` | `0 <= slipstream <= 1`; `0 <= recharge_left <= 1` |
| `racing_line` | at least one point; `seat < 4`; `seat_car[seat] == car` |

Not checked: `steer`, `energy`, `speed_kmh`, `weather`, gaps, Motion, Car Status, Player, Session. Exit code 1 on
any problem or if no packets arrived.

### 7.6 `listen`

- Prints, once a second, total KB/s and per-type `count/s x size`.
- `--save DIR` writes the first packet of each type as `DIR/<type>.bin`; that is how `testdata/` was produced.
- Without `--print`, only events are printed.

### 7.7 `results-server`

Useful as a model for an MCP-side results receiver.

- Default `http://127.0.0.1:29780/`, output folder `race-results/`. Refuses to bind a non-loopback address without
  `--token` (exit code 2).
- Request checks in order: `Origin` header present or `X-BC-Format` missing -> 403 (comment: a browser sends
  `Origin` on any cross-site POST and cannot add `X-BC-Format` without a preflight the server never answers);
  wrong or missing bearer token -> 401, compared with `hmac.compare_digest`; missing `Content-Length` -> 411;
  length over 16 MB -> 413.
- Saves the raw body **before** validating, as `<stamp>.results.json` or `<stamp>.zip`, plus `<stamp>.headers.json`
  holding the `X-BC-*` headers. Stamp is local time `%Y%m%d-%H%M%S-%f`.
- ZIP uploads must contain exactly one entry named `results.json`; inflation is counted as it goes and capped at
  256 MB (the entry's declared size is not trusted).
- Replies 200 when the document passes the schema, **422** when it does not. Since the game drops an upload on any
  non-2xx and never retries, a validating receiver that wants to keep imperfect data should still answer 2xx.
- Validation uses a small built-in JSON Schema subset (`$ref`, `allOf`, `const`, `type`, `minimum`, `maximum`,
  `pattern`, `minItems`, `maxItems`, `items`, `properties`, `required`, `additionalProperties`). It is strict by
  default (unknown properties are reported, to reveal drift between game and schema); `--lenient` allows them.
  At most 200 problems are kept.
- Code comment: "The game never sends a results upload above 16 MB; a weekend is a few MB of JSON."
- `ThreadingHTTPServer`, POST only.

### 7.8 Decoder behaviour worth copying or knowing

- `bc_api.decode()` raises `PacketError` for: shorter than 24 bytes, `packet_format != 1`, `packet_id >= 14`, or a
  length that differs from the fixed size. `example.py` comments that a listening socket "can receive anything",
  so always catch it.
- `bc_api` output conventions: `game_version` as a string; adds `header["packet"]` (the name); enum fields as
  names; flags expanded to booleans; `seat` 255 -> `None`; `compound_picks_left` / `picks_left` as a
  `{compound_name: int | None}` dict; `strategy` and `track` -> `None` when their `has_*` flag is 0;
  `feature_schema_hash` as a hex string; `TIRE.allowed` as a list of compound names; each hand card gets an
  explicit `index`.
- `bc_api.pick_tire()` and `set_camera()` accept either a name or an index. A wrong name raises `ValueError`
  from `list.index`.
- `bc_api.command()` asserts the built datagram has the documented size.
- Rust crate (`bc-game-api`, no dependencies): same sizes and tables; typed structs keep enum fields as raw `u8`;
  no participant-kind name table; `SessionStarted`/`SessionEnded` carry no seat; `obs_count` is clamped to 339 and
  the line-point count to 20; the hand reader always consumes four cards and then keeps `count`.
- Rust doc comment on `PickCard`: `index` is "into the hand the latest `CardsOffered` or `Player` packet showed".
- Rust doc comment on `TokenTooLong`: a longer token means "the game would drop every command sent with it".
- Rust doc comment on `recharging`: "Boost ran empty and is locked until it recharges."
- The Rust test requires at least 13 of the 14 captures to decode and to have `packet_id` matching the file name.
- This copy of the folder contains `rust/src` and `rust/examples` but **no `Cargo.toml`**, so the crate cannot be
  built as delivered.

---

## 8. Decoded testdata

Decoded with the game's `bc_api.py` (the same decoding the tests cross-check when `BC_UPSTREAM_GAMEAPI` is set).
Floats rounded to 4 places; long arrays truncated. Output is in bc_api's dict form, not raw wire values.

Three different sessions appear in the captures: most packets are from `session_uid` 383067569 (player on car 10),
the event from 640902445 (player on car 14), and the three replies from 1663966214 (player on car 13).

Typical header (all telemetry captures):

```json
{"packet_format": 1, "packet_id": 0, "packet_version": 1, "game_version": "0.86.2", "num_cars": 16,
 "session_uid": 383067569, "frame": 3918, "session_time": 78.3584, "seat_car": [10, 255, 255, 255],
 "packet": "motion"}
```

`frame` 3918 at `session_time` 78.36 s confirms 50 ticks per second. Car Status and Session are from frame 3919,
Participants from frame 4009.

### motion.bin (792 bytes), car 10 (the player)

```json
{"position": [-287.844, -30.3018, -179.2842], "velocity": [0.9678, -0.0081, 28.0995],
 "angular_velocity": [-0.0764, -0.8016, -0.0246], "pitch": -1.877, "roll": 2.5247, "yaw": 42.6517}
```

28.1 m/s forward is 101.2 km/h, matching `speed_kmh` below.

### car_telemetry.bin (952 bytes), car 10

```json
{"speed_kmh": 101.1583, "pedal": 1.0, "steer": -0.2495,
 "wheels": [
  {"on_track": true, "grounded": true, "slip_forward": -0.0811, "slip_sideways": 0.0881},
  {"on_track": true, "grounded": true, "slip_forward": -0.0857, "slip_sideways": 0.0893},
  {"on_track": true, "grounded": true, "slip_forward": 0.4521, "slip_sideways": 0.0658},
  {"on_track": true, "grounded": true, "slip_forward": 0.4407, "slip_sideways": 0.0631}],
 "tire_compound": "Wet", "puncture": false, "tire_health": 0.8346, "energy": 1.0}
```

All 16 cars are on Wet tires (weather 0.67). Speeds across the field: 41 to 136 km/h.

### lap_data.bin (312 bytes), cars 10 and 15

```json
{"position": 11, "class_position": 11, "lap": 2, "in_pits": false, "finished": false, "pit_planned": false,
 "api_driving": false, "pit_stops": 0, "gap_to_leader": 4.8655, "interval_ahead": 0.5014,
 "interval_behind": -0.7093}
{"position": 16, "class_position": 16, "lap": 1, "in_pits": true, "finished": false, "pit_planned": false,
 "api_driving": false, "pit_stops": 0, "gap_to_leader": 14.9482, "interval_ahead": 5.1823,
 "interval_behind": -0.243}
```

`interval_behind` is negative for every car, including the last-placed one (-0.243, with nobody behind it).

### car_status.bin (536 bytes), car 10

```json
{"performance": {"Suspension": 0.6717, "EngineTorque": 0.5442, "Downforce": 0.7967, "Acceleration": 0.753,
 "Drag": 0.628, "AngularDrag": 1.1691, "TractionControl": 0.7655, "Weight": 0.7305}}
```

### player.bin (1,039 bytes)

```json
{"seat": 0, "car": 10, "points": 14,
 "hand": [
  {"index": 0, "upgrade": "SensorQuality", "facility": "Redline", "tier": 0, "banned": false},
  {"index": 1, "upgrade": "EnginePower", "facility": "Speed", "tier": 0, "banned": false},
  {"index": 2, "upgrade": "BoostRechargeSpeed", "facility": "Boost", "tier": 0, "banned": false}],
 "upgrades_applied": 0, "total_laps": 24, "laps_remaining": 22,
 "pit_planned": false, "can_pit": true, "in_pits": false, "pit_stops": 0,
 "tire_compound": "Wet", "puncture": false, "tire_health": 0.8346,
 "compound_picks_left": {"Standard": null, "Boost": 2, "Experimental": 2, "Fast": 2, "Durable": 2, "Wet": null,
  "Golden": 2},
 "energy": 1.0,
 "strategy": {"start_grid_position": 3, "laps_since_pit": 2, "tire_age_laps": 2,
  "cars_within_5s_ahead": 10, "cars_within_5s_behind": 3, "class_position": 11, "class_size": 16,
  "class_gap_to_leader": 4.8172, "class_gap_to_ahead": 0.5014, "class_gap_to_behind": 0.7093,
  "reference_lap_s": 28.4179,
  "opponents": [1.0, -4.8172, 0.0, 0.814, 1.0, 1.15, "... 188 more (173 of 194 non-zero, min -4.817, max 22.0)"],
  "near": [1.0, -0.0176, 0.8154, 0.0, 1.0, -0.0423, "... 36 more (36 of 42 non-zero, min -0.112, max 22.0)"]}}
```

Observations: a hand of three cards (count 3 of the 4 slots); 14 unspent points with 0 upgrades applied;
Standard and Wet have no pick limit (255), the others 2 each.

### session.bin (123 bytes)

```json
{"session_type": "Race", "energy": "boost", "total_laps": 24,
 "player_input": true, "driver_input": true, "public_lobby": false,
 "weather": 0.6674, "feature_schema_version": 12, "feature_schema_hash": "0x40A5F76F", "scene": "JapanShort",
 "track": {"length_km": 0.9668, "corner_count": 2, "avg_corner_speed": 11.4877, "straight_ratio": 0.9931,
  "elevation_change": 59.5562, "base_tire_wear_rate": 0.1034, "overtake_difficulty": 0.3982,
  "pit_lane_time_loss_s": 6.3093, "width_mean": 13.323, "width_min": 11.6201, "barrier_ratio": 0.0383,
  "drs_zone_count": 1, "drs_coverage": 0.3531, "lap_time_est_s": 26.3492}}
```

### participants.bin (584 bytes)

```json
{"cars": [
 {"player_id": 9, "car_type": "Stock", "kind": "ai", "name": "Thruster"},
 {"player_id": 10, "car_type": "Stock", "kind": "ai", "name": "Pole"},
 "... cars 2-9: ai",
 {"player_id": 0, "car_type": "Stock", "kind": "local", "name": "<the capturing player's display name>"},
 "... cars 11-15: ai, e.g. 'The Serpent', 'Neon', 'Eagle', 'Striker'"]}
```

15 AI cars and one local human at index 10 with `player_id` 0. `player_id` values are a permutation of 0-15 and do
not equal the car index.

### event.bin (47 bytes), different session, player on car 14

```json
{"code": "CARD", "seat": 0, "points": 1,
 "hand": [
  {"index": 0, "upgrade": "BoostCapacity", "facility": "Boost", "tier": 0, "banned": false},
  {"index": 1, "upgrade": "PitSpeed", "facility": "Performance", "tier": 0, "banned": false},
  {"index": 2, "upgrade": "Suspension", "facility": "Cornering", "tier": 0, "banned": false}]}
```

Raw body: `43415244 00 01 03 08030000 07020000 0f010000 00000000 00` = `CARD`, seat 0, points 1, count 3, three
cards (upgrade, facility, tier, flags), an empty fourth slot, one zero pad byte. Fired at `session_time` 36.7 s.

### ai_observations.bin (1,432 bytes), `packet_version` 2

```json
{"car": 0, "api_driving": false, "policy": "40c3e1d69003", "policy_name": "AllCars.onnx", "obs_frame": 758,
 "action": [1.0, 0.483],
 "obs": [0.1849, 1.0, 1.0, 0.9998, 0.0, -0.011, "... 333 more (290 of 339 non-zero, min -0.415, max 1.989)"]}
```

`obs_count` is 339 (the array is full). `obs_frame` 758 at frame 3918 is about one decision per 5 ticks here,
somewhat slower than the README's "roughly every third physics tick".

### ack.bin (29 bytes)

```json
{"command": "ping", "request_id": 11}
```

### error.bin (30 bytes)

```json
{"command": "pick_card", "request_id": 12, "reason": "invalid_index"}
```

### hello.bin (53 bytes)

```json
{"request_id": 13, "game_version": "0.86.2", "player_input": true, "driver_input": true, "public_lobby": false,
 "driver_data": true, "player_data": true, "decision_events": true, "ai_observations": true, "rate_hz": 50,
 "feature_schema_version": 12, "feature_schema_hash": "0x40A5F76F"}
```

The three replies were captured in-session (frames 3535, 3537, 3539) and carry a full header with `num_cars` 16
and `seat_car` [13, 255, 255, 255].

### car_abilities.bin (200 bytes)

```json
{"cars": [
 {"speed_boost": true, "cornering_boost": false, "super_boost": false, "party_mode": false, "drs": false,
  "boost_overspeed": false, "recharging": false, "super_boost_unlocked": false, "party_mode_unlocked": false,
  "drs_unlocked": false, "api_boosting": false, "ban_tokens": 0, "slipstream": 0.0, "recharge_left": 0.0},
 {"speed_boost": true, "...": "same as above", "slipstream": 0.8498, "recharge_left": 0.0},
 "... 14 more cars"]}
```

Active flags per car index in this frame: 0, 1, 7, 13 speed boost; 2, 3, 8 speed + cornering + super boost;
11 the same plus boost overspeed; 12 boost overspeed only; 5, 9, 15 recharging; 4, 6, 10 (the player), 14 nothing.
All `unlocked` bits and `ban_tokens` are 0 for every car, the player's included, at lap 2 with 0 upgrades applied.
All `recharge_left` values round to 0.0 even for the three recharging cars.

### racing_line.bin (747 bytes)

```json
{"seat": 0, "car": 10, "points": [
 {"racing_line": [-285.169, -30.1097, -175.7136], "left": [-286.5435, -30.187, -174.9104],
  "right": [-275.5609, -29.5696, -181.328]},
 {"racing_line": [-277.8532, -29.1982, -166.1496], "left": [-283.3122, -29.5318, -164.8591],
  "right": [-270.9819, -28.7784, -167.7738]},
 "... 18 more points"]}
```

All 20 points in use. The first point is about 4.5 m from the car's Motion position; consecutive points are about
12 m apart.

---

## 9. MCP tool surface

The tools this server registers, with their parameters, are listed in [TOOLS.md](TOOLS.md) (generated from the
server, so always current). The design notes that shaped them:

- One background UDP listener keeps the latest packet per type (per seat for Player / Racing Line, per car for AI
  Observations) plus a bounded event queue; tools read from this cache.
- One command socket with incrementing `request_id`s; replies are matched on `request_id` and command.
- `drive` and `boost` hold at 50 Hz for a bounded duration in a background task (`duration_s` 0.05 to 10 s for
  `drive`, 0.05 to 30 s for `boost`).
- Every write tool hard-codes `seat = 0`.
- "No data" is reported distinctly from "zero": no telemetry arrives outside a session or when a switch is off.

---

## 10. Discrepancies and open questions

1. **AI Observations size.** README packet table and Bandwidth section say 1,428 bytes. The README's own field list
   sums to 1,408 + 24 = **1,432**, which is what `bc_api.py`, `lib.rs` and `testdata/ai_observations.bin` use.
   Use 1,432. **Confirmed live** against game 0.88.2: 250,584 AI Observations packets of exactly 1,432 bytes in one
   race, none dropped.
2. **`interval_behind` sign.** Documented only as "seconds"; negative for every car in the capture.
3. **Gap mismatch.** In the same frame Lap Data gives the player's `gap_to_leader` as 4.8655 while Player gives
   `class_gap_to_leader` as 4.8172 (single class, so same leader). They are evidently computed differently or at
   different instants.
4. **"session_start" in `header.schema.json`.** It says "the API's hello and session_start give the same value in
   hex". The binary API has no packet of that name and sends the hash as a `uint32`; the Session packet is the one
   that carries it (the `SSTA` event has no details). Looks like wording left over from an earlier API.
5. **`car_class` vs Car types.** Results use names like `FormulaCar`; the UDP enum says `Formula`. A mapping is not
   given. `decision.car_type` and `header.car_type_setting` are "enum numbers" without saying which enum.
6. **Per-command error matrix** is not documented; section 4 infers it.
7. **`api_flags` "active".** Unclear whether these reflect only the settings switches or also become 0 in a public
   lobby or replay.
8. **`set_camera` and Player Input.** The command table says it needs Player Input, while the public-lobby rule
   lets it through. Whether it works with Player Input off is unclear.
9. **`boost` success reply.** "Boost only answers errors, like `drive`" contradicts the general sentence listing
   only `drive` and `hello` as exceptions. Treat `boost` as silent on success.
10. **Events without Decision Events.** `SSTA`, `SEND`, `LAPC`, `PITI`, `PITO`, `FINI` are not decisions but share
    the Event packet, so they presumably also need the Decision Events switch.
11. **Unlock flags all zero in the capture** for the player's own car, though AI cars show super boost active.
    Consistent with "this machine's cars only" plus nothing unlocked yet, but it means the capture does not
    demonstrate those bits.
12. **`recharge_left` reads 0.0 for cars flagged `recharging`** in the capture. Either the value is only filled for
    this machine's cars (not stated) or the wait had just finished.
13. **Unit-less fields**: wheel slip, most Session track properties, Car Status ratios, `tier`, `player_id`,
    `finish_score`, `run_id`, `seed`.
14. **Feature layouts** (`opponents`, `near`, `obs`, `state`, `offered`, `context`) are identified only by
    hash/policy; no layout is shipped in this folder.
15. **Tire screen lifetime** and the default pick when unanswered are not stated; nor is whether `CARD` repeats
    while points remain.
16. **`game-api.json`** layout is not documented.
17. **No `Cargo.toml`** in this copy of `rust/`.
18. **Index stability** [observed 0.88.2]. The README says an index stays the same for the session; live, indices
    re-ordered by race position (section 2.0).
19. **Pause during a card hand** [observed 0.88.2]. The game pauses while a hand waits: only Session, Car Status and
    Participants keep arriving and `session_time` stops. Not mentioned in the README.
20. **`LAPC.lap`**: whether it is the lap just completed or the new lap number is not stated.
