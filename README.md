# backseat-champions-mcp

An **unofficial** [Model Context Protocol](https://modelcontextprotocol.io) server that lets an LLM such as Claude act
as **race engineer** for player one's car in the Steam game **Backseat Champions**, through the game's official UDP
*Game API*. It is not made or endorsed by the game's developers.

## Install & connect

Follow the steps in order; each ends with something you can check. Shell commands are run **in the folder of your
copy of this repository** (the folder with `pyproject.toml`).

### 1. Prerequisites

- Python **3.10 or newer** (`python3 --version`).
- [uv](https://docs.astral.sh/uv/) (recommended; check with `uv --version`). On macOS and Linux astral's installer
  is `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`, or `pipx install uv`); see
  [uv's installation page](https://docs.astral.sh/uv/getting-started/installation/) for Windows and other options.
  Without uv you need pip and the `venv` module: on Debian / Ubuntu install the `python3-venv` package
  (`sudo apt install python3-venv`) first.
- The only dependency, the official MCP Python SDK (`mcp` 2.x), is installed automatically.
- **Claude desktop app on Linux:** Anthropic's Linux app is in **beta** (Debian 12+ / Ubuntu 22.04+, per
  [Claude Desktop on Linux](https://code.claude.com/docs/en/desktop-linux)). On other distributions use Claude
  Code (the CLI).

### 2. Set up the game

In Backseat Champions open **Settings > Telemetry**:

1. Turn the **Game API on** (it is off by default).
2. Tick the **required** switches: **Driver Data**, **Player Data**, **Decision Events** and **Player Input**.
   Optional: **AI Observations** (only for `get_ai_observations`, about 1.1 MB/s). **Driver Input** only if you start
   the server with `BC_ALLOW_DRIVE=1` (it registers `drive`; see the safety notes).
3. Keep the telemetry target **`127.0.0.1:29770`** and the command listen address **`127.0.0.1:29771`** (both are the
   game's defaults).

**One listener per port.** Only one program can receive telemetry on a UDP port. Every MCP client starts its own
copy of this server, so if both Claude Code and the Claude desktop app have it configured and running, the two
copies fight over 29770 (the second reports `telemetry_listener_down`). The same happens with two Claude
Code sessions open at once: each starts its own copy. Use one client at a time, or add a second
telemetry target in the game with its own port (for example `127.0.0.1:29772`) and start the second copy with
`BC_TELEMETRY_PORT=29772`. The same applies to the game's own `gameapi_client.py listen` or any other telemetry tool.

### 3. Install and register the server

**Install it as a uv tool** (recommended). In the repository folder run:

```sh
uv tool install .
command -v backseat-champions-mcp    # prints the absolute path of the installed command
backseat-champions-mcp --version     # prints the version; also confirms the install works
```

This builds the server once and puts the `backseat-champions-mcp` command in uv's tool bin folder (`uv tool dir
--bin` shows it; if `command -v` prints nothing, that folder is not on your `PATH`: run `uv tool update-shell` and
open a new terminal, or use `$(uv tool dir --bin)/backseat-champions-mcp`). Use the **absolute path** in the client
configuration below, so the client does not depend on your shell's `PATH`.

**Claude Code:**

```sh
claude mcp add --transport stdio backseat-champions -- "$(command -v backseat-champions-mcp)"
```

(Your shell replaces `$(command -v ...)` with the absolute path. Add `--scope user` before the name to make it
available in every project.) Check with `claude mcp list`.

**Claude desktop app.** Open its configuration file through the app: *Settings > Developer > Edit Config* (the file
is `claude_desktop_config.json`; let the app show you where it is rather than guessing a path, especially on the
Linux beta). Add:

```json
{
  "mcpServers": {
    "backseat-champions": {
      "command": "/absolute/path/to/backseat-champions-mcp"
    }
  }
}
```

- For `"command"`, use the output of `command -v backseat-champions-mcp` (the app does not read your shell's
  `PATH`).
- Configuration files do **not** expand `~` or `$HOME`: write the paths out in full.
- Environment variables (see [Configuration](#configuration)) go in an optional `"env": { "BC_TELEMETRY_PORT":
  "29772" }` next to `"command"`.

Then fully quit the app (not just close its window) and reopen it.

**Alternative: `uvx` without installing.** `uvx --from /path/to/backseat_champions_ai-server backseat-champions-mcp`
builds and runs the checkout from uv's cache (Claude Code: `claude mcp add --transport stdio backseat-champions --
uvx --from "$PWD" backseat-champions-mcp`; desktop app: `"command"` = the output of `command -v uvx`, `"args":
["--from", "/path/to/backseat_champions_ai-server", "backseat-champions-mcp"]`). Warm the build once in a terminal
with `uvx --from "$PWD" backseat-champions-mcp --version` before connecting, so the client's first start does not
wait for the download and build. **Do not mix the two ways:** once the server is installed as a uv tool,
`uvx --from <checkout> backseat-champions-mcp` runs the *installed* tool, not the checkout (seen with uv 0.11.32).

**Without uv:** `python3 -m venv .venv && . .venv/bin/activate && pip install .`, then use the absolute path of
`.venv/bin/backseat-champions-mcp` as the command (no arguments); after a source change run `pip install .` again.
`--transport streamable-http --port 8000` serves `http://127.0.0.1:8000/mcp` instead of stdio.

**Updating.** Neither the installed tool nor `uvx` notices a change to the Python source by itself. Tested with uv
0.11.32:

- uv tool install: after pulling or changing the source, run `uv tool install --reinstall .` in the repository
  folder (for a git checkout: `git pull && uv tool install --reinstall .`), then fully quit and
  reopen the desktop app (or start a new Claude Code session). The command path stays the same, so the
  configuration does not change. (`uv tool upgrade` is not reliable for a local folder: it can report "Nothing to
  upgrade" while the source changed.)
- uvx: after `git pull`, run `uv cache clean backseat-champions-mcp && uv cache prune` (or `uv cache clean`, which
  empties the whole cache), then restart the client. `uv cache clean backseat-champions-mcp` alone,
  `uvx --refresh`, `--refresh-package` and `--reinstall` still ran the old build in this test. A change to
  `pyproject.toml` (a version bump, say) does make `uvx` rebuild by itself.
- **How to confirm:** `backseat-champions-mcp --version` in a terminal, or ask the model for `get_api_status`:
  its `server_version` must be the new version. That only distinguishes builds with different versions, so
  prefer pulling tagged releases (each bumps the version) to running arbitrary commits.

### 4. Verify

Ask the model to call `get_api_status`. Expect:

- `server_version`: the version in `pyproject.toml`;
- `game.reachable: true` and `game.required_switches_off: []`;
- `telemetry.bound: true` (false: another program holds the port, see above).

`telemetry.receiving` only becomes true once a session runs.

### 5. Start a race

Start the session in the game yourself: **the API cannot start races**. Then `telemetry.receiving` is true. Ask
Claude to "be my race engineer", or invoke the `race_engineer` prompt.

### Troubleshooting first steps

| Symptom | Cause / fix |
|---|---|
| `telemetry_listener_down`, "address in use", `telemetry.bound: false` | Another program holds the telemetry port: usually a second copy of this server started by another MCP client, or `gameapi_client.py listen`. Close it (the server retries every 2 s), or use a second telemetry target port and `BC_TELEMETRY_PORT`. Commands still work. |
| `game.reachable: false` | Game not running, Game API off, or a different `BC_COMMAND_HOST` / `BC_COMMAND_PORT`. |
| `receiving: false`, `not_in_session` | The game sends no telemetry outside a session (practice, qualifying, race). Start one in the game. |
| `paused: true`, data "frozen" | The game stops time while a card hand waits: only Session, Car Status and Participants keep coming. Answer the hand (`pick_card`). |
| `pick_card` refused with `cards_unavailable` | Seen in 0.88.2 for a hand that arrived while the car was in the pit box: the game stays paused until a card is picked; retry `pick_card` after a few seconds - do not wait for the pit exit (a paused game never leaves the pit box). |
| `pit_call_lost: true` | The game dropped an acknowledged `set_pit(true)` at the next lap boundary (seen in 0.88.2 for a call ~1.5 s before the line). Call `set_pit(true)` again if the stop is still wanted; the server never re-sends it. |
| A tool call fails after about 80 s with a client error | Some clients cut tool calls at about 80 s whatever the progress notifications (seen with the Claude desktop app's remote-device bridge). `wait_for_trigger` waits at most 55 s by default and never more than 60 s, which avoids this. If a result is lost anyway, pass `since_seq`: an open decision comes back about 20 s after it was last returned (at most 3 times), another trigger once. |
| Old behaviour after an update | Installed tool: `uv tool install --reinstall .`; uvx: `uv cache clean backseat-champions-mcp && uv cache prune`. Then fully restart the client and check `server_version` (see **Updating**). |
| `boost` / `drive` returns `no_reply` | Usually the game is not answering. Under an extreme flood of replies, though, their liveness ping can time out while the hold has started: check `get_car_abilities` (`api_boosting`) / `get_api_status` (holds), or call `stop_boost` / `release_drive`. |

## Does the AI need instructions?

No setup is needed. The server ships its own operating guide in three places:

- the MCP `initialize` result's `instructions`: the race loop in under 20 lines. Most MCP clients pass these to
  the model; if yours doesn't, invoke the `race_engineer` prompt;
- every tool's description;
- the `race_engineer` prompt (optional argument `goal`): the same loop in more detail.

In short: call `get_api_status` first; then loop on `wait_for_trigger(timeout_s=55, since_seq=last)`; answer a
`card_offer` first and at once with `pick_card(index, seq)` (the game pauses while a hand waits); answer
`tire_screen` with `pick_tire`; on `tires_wont_last` / `car_damaged` decide `set_pit` (an Ack is not a confirmation:
watch for `pit_call_lost`); set `last` to the result's `last_seq` after acting. Use `car="me"`, `"ahead"` or `"behind"` instead of car indices, which change with positions.

### Other MCP clients

The server should work with any MCP client that launches stdio servers (command = the installed
`backseat-champions-mcp`), or over `--transport streamable-http`. The steps above are written for Claude Code and
the Claude desktop app; for other clients note:

- Delivery of the `instructions` to the model varies; use the `race_engineer` prompt (or paste its text) if the
  model does not seem to know the loop.
- Tool-call timeouts vary. `wait_for_trigger` blocks up to 55 s; if your client cuts tool calls sooner, set
  `BC_WAIT_MAX_S` a few seconds below its limit.
- The loop needs a model that follows multi-step tool instructions; weaker models may loop poorly (polling,
  retrying failed calls, missing `since_seq`).

## The trigger model

`wait_for_trigger` is the one wake source: the model is woken only when a decision is needed.

| Trigger | When | Typical answer |
|---|---|---|
| `card_offer` | a new hand of upgrade cards is offered (the game pauses until it is answered) | `ban_card` (optional), `pick_card(index, seq)` |
| `tire_screen` | the tire selection screen opened (grid, qualifying box, pit stop) | `pick_tire` |
| `tires_wont_last` | tire health is forecast not to last to the end of the next lap (heuristic) | `set_pit(true)`, then the `tire_screen` |
| `car_damaged` | puncture (definite) or a sudden car-specific performance drop (heuristic) | puncture: `set_pit(true)` if `can_pit` and the race is not nearly over; performance drop: check `get_car_status` first, pit only on a clear failure |
| `session_ended` | `SEND` or `FINI` for seat 0, once per session | summarise |

Enabled by default: `card_offer`, `car_damaged`, `tires_wont_last`, `tire_screen` (`BC_TRIGGERS`); `session_ended`
is always on.

### Delivery and acknowledgement

- Every trigger gets an increasing `seq` and stays in a bounded queue, across sessions.
- `wait_for_trigger(timeout_s, since_seq)` acknowledges everything up to `since_seq` and returns **at once** if
  anything is *due*; otherwise it blocks until something becomes due or the timeout passes. The wait is
  `min(timeout_s, BC_WAIT_MAX_S, 60)` seconds, 55 by default.
- **Due** means: not acknowledged, and either never returned, or returned at least 20 s ago and still within its
  resend budget: 3 resends for an open decision (`card_offer`, `tire_screen`), 1 for other types when `since_seq`
  is passed, none without `since_seq`. Resent entries carry `resent: true`. So a result the client lost comes back
  on a later wait once 20 s have passed since it was last returned (not instantly), and an unanswered hand is
  returned again without becoming a busy loop.
- **The 55 s cap is the primary protection** against clients that cut a tool call at about 80 s (the Claude desktop
  app's device bridge): a wait that ends before the cutoff never loses its result. Resends are the fallback; in
  that rare case the game can stay paused on a card hand for up to about 20 s before the hand comes back.
- Superseded entries (a newer hand or tire screen; new tires since a tire warning or damage), decisions known to be
  closed, and entries of an old session (except that session's `session_ended`) are not returned; `skipped` counts
  them.
- Every result, timeouts included, carries `last_seq` (pass it as `since_seq` next time) and `open_decisions` (the
  live open card hand and tire screen). `open_decisions` never makes a call return early.
- `status` is `triggered`, `session_ended` (the last entry ended the session) or `timeout`.
- A new server process ignores the first `since_seq` it gets, and a `since_seq` above the newest seq resets the
  acknowledgement (the server was restarted).
- The server sends no resource-updated or log notifications; `bc://triggers/latest` can be read on demand
  (`wait_for_trigger` is the way to be woken).

### What fires each trigger

All thresholds are **this server's tunable heuristics, not values from the game**.

- **card_offer** (not heuristic). A `CARD` event for seat 0 with points > 0 and a hand. Without Decision Events, or
  when no `CARD` event announced a hand within 2 s, the Player packet: points > 0 and a hand whose cards differ from
  the last one, stable for 0.25 s. One hand is one trigger whichever source sees it first; a `CARD` event repeating
  the same cards and points (after a ban, say) is ignored. A hand counts as answered on a successful `pick_card`
  (or `no_points`), not on `cards_unavailable` (seen in 0.88.2 for a hand that arrived in the pit box: the game stays
  paused until a card is picked, so retry `pick_card` after a few seconds - do not wait for the pit exit).
  `pick_card(index, seq)` is refused with `hand_changed` if the hand on offer is no longer that trigger's hand.
  `index` and `seq` also accept numeric strings (`"1"`): a client with a stale cached tool schema sent them in a real
  race.
- **tire_screen** (not heuristic). Every `TIRE` event for seat 0 (`pre_race`, `current`, `allowed`, `picks_left`).
- **car_damaged**, `signal: "puncture"` (definite): your car's puncture flag (Player, or Car Telemetry without Player
  Data) set in two consecutive samples. Re-arms after the flag has been clear for 3 s of session time, or on pit
  exit. Payload includes `can_pit`, `pit_planned`, `pit_planned_by`, `pit_call_lost`, `in_pits`, `laps_remaining`.
  Observed in 0.88.2: worn-out tires puncture (tire health reaching 0.0 set the flag).
- **car_damaged**, `signal: "performance_drop"` (`heuristic: true`). The README says Car Status performance is
  "moved by weather, slipstream, abilities and failures" but has no failure flag. One trigger per Car Status packet
  lists every area of your car that dropped by at least `BC_TRIGGER_PERF_DROP` (15 %) within
  `BC_TRIGGER_PERF_WINDOW_S` (2 s), confirmed only if the drop persists for 1 s of session time while the field
  median of that area did not drop by half the threshold (weather). It is skipped while an ability of your car
  (boost, DRS, overspeed, recharging, slipstream) is active, so a failure during a boost can be missed. Not in the
  pit lane. Expiring temporary upgrades could look the same.
- **tires_wont_last** (heuristic forecast). Wear per lap is the median of the last full laps on the current tire set,
  measured from tire health at lap boundaries; the current lap is also extrapolated from its wear so far over the
  elapsed share of a lap (at least 25 %), in session time, and the higher figure is used (wear is bursty and can
  accelerate). `predicted = health - wear_per_lap * (rest of this lap + 1 lap)`. It fires when
  `predicted <= BC_TIRE_MARGIN` (0.05). `BC_TIRE_FLOOR` (0.10) applies **only when there is no forecast yet**
  (`reason: "floor_no_forecast"`): with a forecast, a low but sufficient health does not fire. Silent after the
  flag, on the final lap (`laps_remaining` 1 in a race with a lap count), in the pit lane, or with a stop already
  called. Re-arms after new tires, when the forecast recovers, or when an acknowledged `set_pit(true)` turns out
  lost. Worn-out tires (health 0.0) puncture (observed in 0.88.2).
- **Laps** (observed in 0.88.2, 8-lap race; the game README says Lap Data `lap` is "laps completed", which is wrong
  there). Lap Data `lap`, the `LAPC` event's `lap` (1 at the start, 9 at the flag of an 8-lap race) and Player
  `total_laps - laps_remaining` are all the **lap being driven**; Player `laps_remaining` counts the laps after the
  current one (0 on the final lap). The server reports `current_lap`, `laps_completed` (= current lap - 1, from the
  highest of the three sources, clamped to 0..`total_laps`; FINI completes the race) and `laps_remaining` = laps
  still to finish, the current one included (1 on the final lap, which also sets `final_lap`; 0 after the flag).
  `get_player_state` shows the packet's own field as `laps_after_current`. `get_trigger_config` keeps the last
  `lap_observations`. (0.2.0 read Lap Data as laps completed: one lap too many completed, the last-lap silence
  started a lap early, and the end of a race showed 9/8.)
- **Pit call verification** (observed in 0.88.2: a `set_pit(true)` acknowledged ~1.5 s before a lap boundary was
  dropped by the game at the boundary; a second call after it stuck). An Ack only means the game received the
  command: `set_pit(true)` waits up to 0.5 s for a sample showing `pit_planned` and says `confirmed` true or
  false. The server keeps watching: if `pit_planned` is false 1 s (session time) after the next lap boundary (lap
  count, `LAPC` or `PITW`), the car has not entered the pit lane and you did not cancel, the call is **lost**:
  `pit_call_lost: true` in `get_pending_decisions`, `get_race_snapshot`, the trigger contexts and payloads, a
  note in the next `wait_for_trigger` result, and `tires_wont_last` is re-armed so you are woken again. The server
  never re-sends the call. `pit_planned_by` is `"api"` when `pit_planned` rose while your call was pending and
  `"game"` otherwise: the game's own strategy can plan a stop by itself (seen with `can_pit` false).
- **Energy** is 0..1 at the base capacity and can exceed 1 with capacity upgrades (BoostCapacity cards; observed up to
  ~3.7). It is not clamped.

### Pauses and car indices

**Pauses** (observed in game 0.88.2). The game stops time while a card hand waits: only Session, Car Status (twice a
second) and Participants keep arriving and `session_time` stops. Time-based heuristics run on `session_time`, so a
pause does not distort them; `get_api_status`, `get_race_snapshot` and wait results report `paused` instead of
"telemetry stopped".

**Car indices** (observed in game 0.88.2). The game README says a car's index stays the same for the whole session,
but in 0.88.2 the participant list and `seat_car` were re-ordered by race position mid-race. The server takes "my
car" from each packet's own `seat_car[0]`, names other cars only from a Participants packet of the same frame, and
returns the stable `player_id`. Tools accept `car="me"`, `"ahead"`, `"behind"` (resolved by position in the newest
Lap Data) or `"P<n>"`; an integer index is valid for that instant only.

## Configuration

Every setting is an environment variable; most also have a CLI flag that overrides it (`backseat-champions-mcp
--help` lists them).

| Variable | Default | Meaning |
|---|---|---|
| `BC_TELEMETRY_HOST` | `127.0.0.1` | address telemetry is received on. Off loopback, telemetry is only accepted from `BC_GAME_HOST` |
| `BC_TELEMETRY_PORT` | `29770` | telemetry UDP port (a target set under Settings > Telemetry) |
| `BC_TELEMETRY_REUSE` | `auto` | `auto` / `on` / `off`: SO_REUSEADDR (+ SO_REUSEPORT) on the telemetry socket; `auto` = not on Windows |
| `BC_GAME_HOST` | = `BC_COMMAND_HOST` | the only telemetry sender accepted when listening off loopback |
| `BC_COMMAND_HOST` | `127.0.0.1` | the game's command address |
| `BC_COMMAND_PORT` | `29771` | the game's command UDP port |
| `BC_COMMAND_TOKEN` | empty | Command Token, only for a game on another machine; 12 to 47 UTF-8 bytes |
| `BC_COMMAND_TIMEOUT` | `1.0` | seconds to wait for Ack / Error |
| `BC_ALLOW_DRIVE` | off | `1` registers `drive` / `release_drive` (see safety notes) |
| `BC_TRIGGERS` | `card_offer,car_damaged,tires_wont_last,tire_screen` | enabled triggers; `none` disables all |
| `BC_WAIT_MAX_S` | `55` | longest `wait_for_trigger` wait; it can only lower the hard cap of 60 s |
| `BC_PROGRESS_INTERVAL_S` | `10` | seconds between MCP progress notifications during waits; a number > 0 and <= 60, otherwise the server exits (code 2) with a message |
| `BC_REMIND_S` | `20` | advanced, meant for tests: seconds before an unacknowledged trigger is resent (must be > 0); the "20 s" in this README assumes the default |
| `BC_TIRE_MARGIN` | `0.05` | `tires_wont_last`: fire when predicted health at the end of the next lap is at or below this |
| `BC_TIRE_FLOOR` | `0.10` | `tires_wont_last`: fire at or below this health only while there is no forecast |
| `BC_TRIGGER_PERF_DROP` | `0.15` | `car_damaged` heuristic: relative drop of a performance area (`0` disables it) |
| `BC_TRIGGER_PERF_WINDOW_S` | `2.0` | ... within this many seconds of session time |
| `BC_RESULTS_PORT` | unset | enable the race results receiver on this TCP port (e.g. `29780`) |
| `BC_RESULTS_HOST` | `127.0.0.1` | results receiver address; off loopback a token is required |
| `BC_RESULTS_TOKEN` | empty | bearer token the game must send |
| `BC_RESULTS_DIR` | `~/.backseat-champions-mcp/race-results` | where uploads are stored (folder 0700, files 0600). A leading `~` is expanded to your home folder and a relative path is resolved against the server's working directory at start, which a client chooses: use an absolute path |
| `BC_RESULTS_KEEP` | `100` | uploads kept; the oldest are deleted when the receiver starts and after each new upload (`0` keeps all) |
| `BC_LOG_LEVEL` | `WARNING` | stderr log level: DEBUG, INFO, WARNING or ERROR, any case |

## Tools

[docs/TOOLS.md](docs/TOOLS.md) has every tool with its parameters (generated from the server).
[docs/API_REFERENCE.md](docs/API_REFERENCE.md) has notes on the game's Game API (packets, events, commands, enums).

| Tool | Summary |
|---|---|
| `wait_for_trigger` | block until a decision is needed (the main loop) |
| `get_api_status` | hello + ping: game version, switches (required / optional), listener health, pause, config |
| `get_race_snapshot` | compact overview of the race for your car |
| `get_pending_decisions` | open card hand, pit call state (`pit_planned_by`, `pit_call_lost`), open tire screen |
| `pick_card` / `ban_card` | pick or ban a card from the hand (optional `seq` guard) |
| `set_pit` | call or cancel a pit stop (`confirmed` says whether `pit_planned` showed; lost calls are reported) |
| `pick_tire` | pick a compound while the tire screen is open |
| `boost` / `stop_boost` | hold boost buttons for `duration_s` 0.05 to 30 s (re-sent at 50 Hz) / release them |
| `set_camera` | change your camera view |
| `get_session`, `get_participants`, `get_standings` | session, cars (with `player_id`), running order |
| `get_car_telemetry`, `get_car_motion`, `get_car_status`, `get_car_abilities` | per-car data (`car="me"`, `"ahead"`, `"behind"`, `"P<n>"`, an index or `"all"`) |
| `get_player_state` | points, hand, laps, pit flags, tires, compound picks left, energy, strategy scalars |
| `get_racing_line`, `get_ai_observations` | next checkpoints; the AI driver's net |
| `get_events`, `wait_for_event` | buffered game events (not needed for the race loop) |
| `get_triggers`, `get_trigger_config` | recent triggers; enabled triggers, thresholds, tire forecast, lap observations |
| `get_reference` | static tables: enums, error reasons, events, packets, commands, switches, rules, triggers, observed behaviour |
| `ping` | liveness check with latency |
| `drive` / `release_drive` | only with `BC_ALLOW_DRIVE=1`; `duration_s` 0.05 to 10 s |
| `list_race_results` / `get_race_result` | only with `BC_RESULTS_PORT` |

Resources: `bc://reference/{topic}`, `bc://triggers/latest`. Prompt: `race_engineer`.

Every tool result is `{ok: true, ...}` or `{ok: false, error_reason, explanation, hint}`. **Exception:** arguments
that do not match a tool's schema (a wrong type, an out-of-range number, `true` where an integer is expected) are
rejected by the MCP layer before the tool runs and arrive as plain MCP error text (`isError: true`), not in that
shape. A reply of the wrong kind from the game is `protocol_error`; `no_reply` on `pick_card` / `ban_card` /
`set_pit` / `pick_tire` carries `outcome_unknown: true` (check the state before retrying). `boost` and `drive` are
only answered when the game refuses them, so their success is *presumed* when the game answers a ping and sends no
error within 0.2 s.

## Safety notes

- **No autonomous actions.** The server never sends a gameplay command on its own: only the read-only `hello`
  handshake is sent unasked (at start, on a new session at most once a second, and from read tools when data is
  missing, to say why). Nothing changes in the race unless the model calls a command tool.
- **Seat rule.** Only player one's car (seat 0) can be commanded; the server always sends seat 0.
- **Public lobbies.** The game refuses every command except `ping`, `hello` and `set_camera` (`public_lobby`).
  Telemetry still flows.
- **drive** is not registered unless `BC_ALLOW_DRIVE=1`. Driving through the API **withholds achievements, Steam stats
  and leaderboard submissions for the whole race weekend**, and only works where this machine simulates the car.
  (The README says `boost` does not count as driver input; that boosting does not withhold them is inferred.)
- **Command Token.** Commands from another machine must carry the token set in the game; it travels in plain text, so
  use it only on trusted networks. Prefer the environment variable to the CLI flag (visible in process lists).
  `get_api_status` never shows the token.
- **Remote telemetry.** Off loopback the server accepts telemetry only from the game host.
- **One listener per port.** The server checks that the port is free before binding, so a second copy never silently
  shares the port and loses half the datagrams.

## Race results receiver (optional)

When you **host**, the game can upload each finished race weekend to your endpoints (Settings > Telemetry > Race
Results). With `BC_RESULTS_PORT=29780` this server listens on `http://127.0.0.1:29780/`; add that URL (and
`BC_RESULTS_TOKEN` as its token, if set) in the game. `list_race_results` and `get_race_result` appear only then.

- **Lifetime.** The receiver runs only while this server process runs; the game does not retry, so a weekend that
  ends while the server is not running is lost.
- **Guards** (as the game's `gameapi_client.py results-server`): an `Origin` header or a missing `X-BC-Format` is
  refused (403), a wrong token 401, bodies over 16 MB 413, ZIPs unpacking to over 256 MB or anything that is not
  readable JSON 422. Timeouts: the request line and headers must arrive within 10 s of the connection being served
  (else it is closed without an answer); the body within 30 s, or `size / 128 KiB/s` for bodies over 3.75 MB, at
  most 120 s (else 408); a connection that sends nothing for 30 s is closed. At most 16 connections are served at
  once; a new one waits up to 11 s for a free slot, then is closed.
- **Schema problems.** Unlike the reference (which answers 422), a readable JSON document that fails this server's
  checks (missing fields, `format` other than 2, ...) is saved and answered **200**, because the game would not send
  it again; the problems are recorded and shown by `list_race_results`. `get_race_result` refuses a `format` other
  than 2 (`unsupported_format`) and reports a damaged stored file as `stored_file_unreadable`.
- **Storage.** In `BC_RESULTS_DIR`, the body **as received** (`<id>.results.json` or `.results.zip`) plus
  `<id>.headers.json` (the `X-BC-*` headers, receive time, problems). The id is the UTC receive time. Files are 0600,
  the folder 0700. Each file is written to a temporary name and renamed into place (headers first, results last),
  so a failed save (disk full, say) answers 500 and leaves nothing behind.
- **Retention.** The newest `BC_RESULTS_KEEP` (100) uploads are kept: older ones are deleted, oldest first, when the
  receiver starts and after each saved upload. An upload counts only when both of its files exist. Pruning also
  deletes leftovers older than 1 h (temp files, headers files without a results file); a results file without
  headers is not listed and never deleted.
- **Output.** `get_race_result` returns versions, each session's decision-log header, the classification as
  uploaded and decision counts; numbers are not rounded. Steam ids (`steam_id`, `host_steam_id`) are included only
  with `include_steam_ids=true`.

## Testing

```sh
pip install -e '.[test]'       # or: uv pip install -e '.[test]'
python3 -m pytest -q
python3 scripts/gen_tools_doc.py  # regenerates docs/TOOLS.md from the registered tools
```

The tests decode the 14 packets captured from the game (`tests/data`), unit-test the trigger engine, and run the
server end to end over stdio against a fake game (`tests/fake_game.py`; by hand: `python3 tests/fake_game.py
--scenario --events` sends telemetry to 127.0.0.1:29770 and answers commands on 29771). To also cross-check the
decoder and command encoders against the game's reference codec, set `BC_UPSTREAM_GAMEAPI` to the game's `GameAPI`
folder (the one with `bc_api.py`); without it those tests are skipped.

## Known limitations and live-only questions

Tested against captured packets and a fake game; two races were run against game 0.88.2, which is where the pause,
index re-ordering, `cards_unavailable` in the pit box, the ~80 s client limit, the lap numbering, the lost pit call,
energy above 1 and worn-out tires puncturing were seen. Still open:

1. Whether the game pauses on every pending hand (assumed; the Player packet decides otherwise, nothing is lost).
2. Whether events or `frame` advance during a pause (pause detection uses `session_time`).
3. Whether a pit call is dropped only close to the lap boundary (seen once, ~1.5 s before it); `pit_call_lost` is
   judged 1 s after the boundary and can be wrong if the game re-plans the stop later.
4. Whether Participants is re-sent when the order changes (names of other cars are attached only when exact).
5. Whether index = position - 1 always holds within one packet (assumed for `"ahead"` / `"behind"`).
6. Whether `cards_unavailable` only happens in the pit box (the hand stays open either way; retry after a few
   seconds).
7. `packet_version` values other than the README's are dropped and counted in `get_api_status`.
8. Whether `CARD` repeats after a ban (deduplicated by cards and points).
9. How the pre-race tire screen closes (resends are bounded either way).
10. Puncture flags shorter than the sample interval can be missed; a failure during a boost can be missed.
11. If a client drops a `wait_for_trigger` result despite the 55 s cap, the lost card hand is resent only after 20 s,
    so the game can stay paused on it for up to about 20 s.
12. A telemetry packet with a new, never-seen `session_uid` starts a new session in this server. Source filtering
    applies only off loopback, so on loopback any local process could spoof that.
13. Last-lap silence and `laps_remaining` need Lap Data (Driver Data) or Player Data. With neither there is no lap
    count, and `tires_wont_last` can fire on the last lap.
14. Under an extreme flood of replies on the command port, the liveness ping of `boost` / `drive` can time out
    and return `no_reply` even though the hold may have started: check `get_car_abilities` (`api_boosting`) or
    `get_api_status` (holds), or call `stop_boost` / `release_drive`.
