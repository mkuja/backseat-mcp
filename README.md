# backseat-champions-mcp

An **unofficial** [Model Context Protocol](https://modelcontextprotocol.io) server for the official *Game API* of the
Steam game **Backseat Champions**. It is not made or endorsed by the game's developers.

In Backseat Champions you do not steer: a machine-learned AI drives, and you manage it during races by picking
upgrade cards, calling pit stops, choosing tire compounds and holding boost buttons. This server lets an LLM such as
Claude act as **race engineer** for player one's car through the game's UDP API.

**Trigger-driven.** The model is woken only when a decision is needed, through one long-polling tool,
`wait_for_trigger`:

| Trigger | When | Typical answer |
|---|---|---|
| `card_offer` | a new hand of upgrade cards is offered | `ban_card` (optional), `pick_card` |
| `car_damaged` | puncture flag 0 -> 1 (definite), or a sudden car-specific performance drop (heuristic) | `set_pit(true)` |
| `tires_wont_last` | tire health is forecast not to last until the end of the next lap (heuristic) | `set_pit(true)`, then `pick_tire` |
| `tire_screen` *(off by default)* | the tire selection screen opened | `pick_tire` |

Everything else (standings, telemetry, session, abilities, events, ...) is available as on-demand query tools. **The
server never sends a command on its own.**

Documentation in this folder:

- [docs/TOOLS.md](docs/TOOLS.md): every tool with parameters (generated from the server).
- [docs/API_REFERENCE.md](docs/API_REFERENCE.md): the Game API itself (packets, events, commands, enums, quirks).

## Enabling the Game API in the game

The API is **off by default**. In the game open **Settings > Telemetry** and turn it on. The defaults match this
server: telemetry to `127.0.0.1:29770`, commands on `127.0.0.1:29771`. Telemetry is only sent **during a session**
(practice, qualifying, race). Then tick the switches the tools you want need:

| Switch | Needed by |
|---|---|
| Driver Data | `get_standings`, `get_participants`, `get_car_telemetry`, `get_car_motion`, `get_car_status`, `get_racing_line`, `get_car_abilities`; lap counting and the performance-drop heuristic of the triggers |
| Player Data | `get_player_state`, `get_pending_decisions`, `get_car_abilities`; tire health, puncture, laps remaining and pit flags for the triggers; the card-offer fallback |
| Decision Events | `get_events`, `wait_for_event`; `card_offer` from CARD events; `tire_screen`; the open tire screen in `get_pending_decisions`; `session_ended` |
| AI Observations (off by default, ~1.1 MB/s) | `get_ai_observations` (only on the machine that simulates the cars) |
| Player Input | `pick_card`, `ban_card`, `set_pit`, `pick_tire`, `boost`, `stop_boost`, `set_camera` |
| Driver Input | `drive`, `release_drive` (only registered with `BC_ALLOW_DRIVE=1`) |

Recommended for the race-engineer workflow: Driver Data, Player Data, Decision Events and Player Input.
`get_api_status` reports which switches are off and which tools that breaks.

## Install and run

Requires Python 3.10 or newer. The only dependency is the official MCP Python SDK, `mcp` 2.x (where the former
`FastMCP` class is called `MCPServer`).

With [uv](https://docs.astral.sh/uv/):

```sh
uv tool install /home/mikko/src/backseat_champions_ai-server          # installs the backseat-champions-mcp command
# or run without installing:
uvx --from /home/mikko/src/backseat_champions_ai-server backseat-champions-mcp --help
```

With pip:

```sh
python -m venv .venv && . .venv/bin/activate
pip install /home/mikko/src/backseat_champions_ai-server
backseat-champions-mcp --help
```

The server speaks MCP over **stdio** by default; `--transport streamable-http --port 8000` serves
`http://127.0.0.1:8000/mcp` instead. Logs go to stderr (`--log-level`, `BC_LOG_LEVEL`); stdout carries only MCP.

### Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "backseat-champions": {
      "command": "uvx",
      "args": ["--from", "/home/mikko/src/backseat_champions_ai-server", "backseat-champions-mcp"],
      "env": { "BC_TRIGGERS": "card_offer,car_damaged,tires_wont_last" }
    }
  }
}
```

### Claude Code

```sh
claude mcp add --env BC_WAIT_MAX_S=240 --transport stdio backseat-champions \
  -- uvx --from /home/mikko/src/backseat_champions_ai-server backseat-champions-mcp
```

or in `.mcp.json`:

```json
{
  "mcpServers": {
    "backseat-champions": {
      "command": "uvx",
      "args": ["--from", "/home/mikko/src/backseat_champions_ai-server", "backseat-champions-mcp"],
      "env": { "BC_WAIT_MAX_S": "240" },
      "timeout": 300000
    }
  }
}
```

`wait_for_trigger` blocks for up to `BC_WAIT_MAX_S` seconds (default 300) and sends MCP progress notifications every
10 s while it waits. Your MCP client's own tool timeout must be longer than that: in Claude Code the per-server
`timeout` (milliseconds) or `MCP_TOOL_TIMEOUT` is a hard limit per call that progress notifications do not extend.
If in doubt, lower `BC_WAIT_MAX_S`; on timeout the tool just returns `status: "timeout"` and the model calls it again.

Then ask Claude to use the `race_engineer` prompt, or: "Be my race engineer: call get_api_status, then loop on
wait_for_trigger and handle each trigger."

## Configuration

Every setting is an environment variable; most also have a CLI flag that overrides it (`--telemetry-port` for
`BC_TELEMETRY_PORT`, ...). `backseat-champions-mcp --help` lists the flags.

| Variable | Default | Meaning |
|---|---|---|
| `BC_TELEMETRY_HOST` | `127.0.0.1` | address telemetry is received on. Off loopback, telemetry is only accepted from `BC_GAME_HOST` |
| `BC_TELEMETRY_PORT` | `29770` | telemetry UDP port (a target set under Settings > Telemetry) |
| `BC_GAME_HOST` | = `BC_COMMAND_HOST` | the only sender accepted when listening off loopback |
| `BC_COMMAND_HOST` | `127.0.0.1` | the game's command address |
| `BC_COMMAND_PORT` | `29771` | the game's command UDP port |
| `BC_COMMAND_TOKEN` | empty | Command Token, needed only when the game runs on another machine; 12 to 47 UTF-8 bytes |
| `BC_COMMAND_TIMEOUT` | `1.0` | seconds to wait for Ack / Error |
| `BC_ALLOW_DRIVE` | off | `1` registers `drive` / `release_drive` (see safety notes) |
| `BC_TELEMETRY_REUSE` | `auto` | `auto` / `on` / `off`: set SO_REUSEADDR (+ SO_REUSEPORT) on the telemetry socket; `auto` = not on Windows |
| `BC_TRIGGERS` | `card_offer,car_damaged,tires_wont_last` | enabled triggers; add `tire_screen`; `none` disables all |
| `BC_TIRE_MARGIN` | `0.05` | `tires_wont_last`: fire when predicted health at the end of the next lap is at or below this |
| `BC_TIRE_FLOOR` | `0.10` | `tires_wont_last`: fire when health is at or below this, whatever the forecast |
| `BC_TRIGGER_PERF_DROP` | `0.15` | `car_damaged` heuristic: relative drop of a performance area (`0` disables it) |
| `BC_TRIGGER_PERF_WINDOW_S` | `2.0` | ... within this many seconds |
| `BC_WAIT_MAX_S` | `300` | longest `wait_for_trigger` wait (1..3600) |
| `BC_RESULTS_PORT` | unset | enable the race results receiver on this TCP port (e.g. `29780`) |
| `BC_RESULTS_HOST` | `127.0.0.1` | results receiver address; off loopback a token is required |
| `BC_RESULTS_TOKEN` | empty | bearer token the game must send |
| `BC_RESULTS_DIR` | `~/.backseat-champions-mcp/race-results` | where uploads are stored |
| `BC_LOG_LEVEL` | `WARNING` | stderr log level |

### Race results (optional)

When you **host**, the game can upload each finished race weekend to your own endpoints (Settings > Telemetry >
Race Results). With `BC_RESULTS_PORT=29780` this server listens on `http://127.0.0.1:29780/`; add that URL (and
`BC_RESULTS_TOKEN` as its token, if you set one) in the game. The game never retries an upload. The tools
`list_race_results` and `get_race_result` appear only when the receiver is enabled. Like the game's own
`gameapi_client.py results-server`, the receiver refuses requests with an `Origin` header or without
`X-BC-Format`, bodies over 16 MB, and ZIPs that unpack to over 256 MB.

## The trigger model

The engine (`src/backseat_champions_mcp/triggers.py`) watches the decoded telemetry of seat 0's car. Each trigger is
**edge-triggered**: it fires once per occurrence and re-arms. Triggers get increasing sequence numbers and are kept
in a bounded queue that is cleared when a new session starts (the numbers keep increasing).
`wait_for_trigger` returns every not-yet-delivered trigger at once, with a compact race context; older ones may be
marked `superseded` (a newer hand arrived, or the car has had new tires since).

All thresholds below are **this server's tunable heuristics, not values from the game**.

- **card_offer** (not heuristic). Fires on a `CARD` event for seat 0 (points > 0 and a hand). Without Decision Events
  it uses the Player packet: points > 0 and a hand whose cards (upgrade, facility, tier) differ from the last one
  seen, stable for 0.25 s. With Decision Events on, a hand that no `CARD` event announced within 2 s (for example one
  already on offer when the server started) is still reported from the Player packet. One hand is one trigger even
  if both sources see it; a `CARD` event repeating the same cards and points (for example after a ban) is ignored.
  Limitation: without Decision Events, a new hand with exactly the same cards as the previous one is not detected.
- **car_damaged**, signal `puncture` (definite): the puncture flag of your car (Player packet, or Car Telemetry when
  Player Data is off) goes 0 -> 1. Re-arms when the flag clears; a puncture still showing after leaving the pit lane
  is reported again.
- **car_damaged**, signal `performance_drop` (`heuristic: true`): the README says Car Status performance is "moved by
  weather, slipstream, abilities and failures" but has no failure flag. The server reports a relative drop of at
  least `BC_TRIGGER_PERF_DROP` (15 %) of one area within `BC_TRIGGER_PERF_WINDOW_S` (2 s) when most other cars' same
  area did not drop by half that (weather), no ability of your car (boost, DRS, overspeed, recharging, slipstream
  > 0.05) was active in that window, and the car is not in the pit lane. It re-arms when the area recovers.
  Expiring temporary upgrades could look the same.
- **tires_wont_last** (heuristic forecast). Wear per lap is the median of the last three full laps on the current
  tire set, measured from tire health at lap boundaries (Lap Data lap counter or `LAPC` event). Before one full lap is
  measured, it is extrapolated from the wear so far in this lap divided by the elapsed share of a lap (lap time:
  observed, else the strategy model's `reference_lap_s`, else the track's `lap_time_est_s`; at least 25 % of a lap
  must have passed). The tire set restarts on pit exit, a compound change, or health rising.
  `predicted = health - wear_per_lap * (rest of this lap + 1 lap)` (capped at the race distance left). It fires when
  `predicted <= BC_TIRE_MARGIN` (0.05) or `health <= BC_TIRE_FLOOR` (0.10), but not after the flag, on the last lap
  (laps_remaining <= 1 in a session with a lap count; without Player Data, laps_remaining is
  total_laps - laps completed), in the pit lane, or when a stop is already called (it fires
  if that call is cancelled). It re-arms after a pit exit / new tires or when the forecast recovers 0.03 above the
  margin. Payload: tire_health, wear_per_lap and how it was derived, predicted health, laps_remaining, can_pit,
  pit_planned, compound, compound_picks_left, pit_lane_time_loss_s, lap time and its source.
- **tire_screen** (off by default): every `TIRE` event for seat 0 (pre_race, current, allowed, picks_left).
- **session end** is not a trigger: on `SEND` (or `FINI` for seat 0) `wait_for_trigger` returns
  `status: "session_ended"` once.

When a trigger fires, the server also sends, best effort, a `notifications/resources/updated` for
`bc://triggers/latest` and a log notification to the most recent MCP session.

## Tools

See [docs/TOOLS.md](docs/TOOLS.md) for full descriptions and parameters.

| Tool | Summary |
|---|---|
| `get_api_status` | hello + ping: game version, switches (and what is off), listener health, holds, triggers, config |
| `ping` | liveness check with latency |
| `get_session` | session type, laps, fuel/boost, weather, input flags, track properties |
| `get_participants` | every car: name, car type, kind (ai / local / remote) |
| `get_standings` | running order with laps, gaps, pit state |
| `get_car_telemetry` | speed, pedal, steer, tire compound/health, puncture, energy (car "me" / index / "all") |
| `get_car_motion` | world position, velocity, orientation |
| `get_car_status` | performance ratio per area, with the field median |
| `get_car_abilities` | boosts, DRS, slipstream, recharge; your unlocks and ban tokens |
| `get_player_state` | points, hand, laps, pit flags, tires, compound picks left, energy, strategy scalars |
| `get_pending_decisions` | open card hand, pit call state, open tire screen |
| `get_racing_line` | next checkpoints: racing line and track edges |
| `get_ai_observations` | the AI driver's net: policy, last action, optional input vector |
| `get_events` | buffered events with sequence numbers |
| `wait_for_event` | block until a matching event (max 60 s) |
| `get_race_snapshot` | compact overview of the race for your car |
| `get_reference` | static tables: enums, error reasons, events, packets, commands, switches, rules, triggers |
| `wait_for_trigger` | block until a decision is needed (the main loop) |
| `get_triggers` | recent triggers without waiting |
| `get_trigger_config` | enabled triggers, thresholds and live tire-wear forecast |
| `pick_card` | pick a card from the hand |
| `ban_card` | ban a card (spends a ban token, keeps your turn) |
| `set_pit` | call or cancel a pit stop |
| `pick_tire` | pick a compound while the tire screen is open |
| `boost` | hold boost buttons for a duration (re-sent at 50 Hz) |
| `stop_boost` | release boost buttons now |
| `set_camera` | change your camera view |
| `drive` / `release_drive` | only with `BC_ALLOW_DRIVE=1`: take over pedal and steering / hand the car back |
| `list_race_results` / `get_race_result` | only with `BC_RESULTS_PORT`: uploaded race weekends |

Resources: `bc://reference/{topic}`, `bc://triggers/latest`. Prompt: `race_engineer`.

Every command result is `{ok: true, ...}` or `{ok: false, error_reason, explanation, hint}`; every error reason of
the game is mapped to an explanation and a hint. `boost` and `drive` are only answered when the game refuses them,
so their success is *presumed* when the game answers a ping and sends no error within 0.2 s; the tools say so.

## Safety notes

- **Seat rule.** Only player one's car (seat 0) can be commanded; the server always sends seat 0.
- **Public lobbies.** In a public lobby the game refuses every command except `ping`, `hello` and `set_camera`
  (`public_lobby`), and hands back a car being driven or boosted. Telemetry still flows.
- **drive** is not registered unless `BC_ALLOW_DRIVE=1`. Driving through the API **withholds achievements, Steam stats
  and leaderboard submissions for the whole race weekend** (a race after qualifying on the same track counts), and
  only works where this machine simulates the car.
- **Command Token.** Commands from another machine must carry the token set in the game; it travels in plain text, so
  use it only on trusted networks. Prefer the environment variable to the CLI flag (visible in process lists).
  `get_api_status` never shows the token.
- **Remote telemetry.** When listening on a non-loopback address the server accepts telemetry only from the game host,
  so nobody else on the network can feed it fake events.
- **One listener per port.** Two programs cannot both receive the stream on one UDP port. The server first checks that
  the port is free (so a second copy never silently shares and loses half the datagrams), then binds with
  SO_REUSEADDR/SO_REUSEPORT as requested.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `game.reachable: false`, `game_unreachable` / `no_reply` | Game not running, API off (Settings > Telemetry), wrong `BC_COMMAND_HOST`/`PORT`, or a missing / wrong Command Token for a game on another machine (the game drops those silently). |
| `telemetry_listener_down` | Another program holds the telemetry port: a second copy of this server (e.g. started by another MCP client), `gameapi_client.py listen`, or another tool. Close it, or add a second telemetry target with another port in the game and set `BC_TELEMETRY_PORT`. Commands still work. |
| `switch_off` | The game is running but the needed switch is off; the error names it. |
| `not_in_session` / no telemetry | The game sends telemetry only during a session. |
| `wait_for_trigger` fails with a client timeout | Lower `BC_WAIT_MAX_S` or raise the client's tool timeout. |
| No `card_offer` | Card offers need Decision Events (or Player Data for the fallback) and points > 0. `get_trigger_config` shows the source in use. |
| `tires_wont_last` seems late or early | It is a forecast; `get_trigger_config` shows the samples, lap time source and prediction. Tune `BC_TIRE_MARGIN` / `BC_TIRE_FLOOR`. |
| `input_disabled` | Switch on Player Input (or Driver Input for drive). |

## Verifying against the live game

This build was tested only against the 14 packets captured from the game (in `tests/data`, byte-identical to the
game's `testdata`), the game's reference codec `bc_api.py`, and a fake game (`tests/fake_game.py`). It has **not**
been run against Backseat Champions itself. To check it:

1. Close anything else listening on 29770 (e.g. `gameapi_client.py listen`). Enable the API with Driver Data, Player
   Data, Decision Events and Player Input. Optionally run the game's own `python gameapi_client.py validate
   --seconds 30` first (then close it).
2. Start a single-player race, connect the server, call `get_api_status`: expect `reachable: true`, no switches off,
   `receiving: true` and `my_car` set.
3. Call `get_race_snapshot`, `get_standings`, `get_player_state`, `get_session`; compare with the screen
   (position, lap, tire health, points, hand).
4. With points on offer, `wait_for_trigger` should return a `card_offer` matching the hand on screen; `pick_card`
   should pick it in game. Try `ban_card` with a ban token.
5. `boost(speed=true, duration_s=3)` and watch `get_car_abilities` (`api_boosting`, `speed_boost`).
6. Run a long race and watch `get_trigger_config` (tire samples and forecast) until `tires_wont_last` fires; call
   `set_pit(true)`, then `wait_for_event(codes=["TIRE"])` and `pick_tire`.
7. Things the documentation leaves open and that are worth watching: whether a `CARD` event repeats after a ban,
   what the game shows in Car Status when something fails, how `interval_behind`'s sign is meant, and how long the
   tire screen stays open.

The fake game can also be used by hand: `python tests/fake_game.py --scenario --events` sends telemetry to
127.0.0.1:29770 and answers commands on 29771 (laps every 20 s, tires wearing 0.2 per lap, a puncture at lap 3).

## Development

```sh
pip install mcp pytest   # or: uv pip install -e '.[test]'
python -m pytest -q
python3 -m compileall src
python scripts/gen_tools_doc.py          # regenerates docs/TOOLS.md from the registered tools
```

The tests decode every captured packet, cross-check the decoder and every command encoder against the game's
`bc_api.py` (when found at `/mnt/user-data/uploads/GameAPI` or `BC_UPSTREAM_GAMEAPI`), unit-test the trigger engine on
synthetic sequences, and run the server end to end through the MCP client over stdio against the fake game.
