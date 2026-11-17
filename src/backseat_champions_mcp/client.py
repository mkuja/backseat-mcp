"""asyncio UDP client for the Backseat Champions Game API.

One :class:`GameClient` owns:

* the telemetry listener: a UDP socket bound to the address the game sends telemetry to. It keeps the latest
  datagram of every packet type (per seat for Player and Racing Line, per car for AI Observations) with its
  receive time, and decodes a packet only when somebody asks for it;
* the event queue: every Event packet, decoded, with a sequence number, plus the decisions still open;
* the command socket: connected to the game's listen address; every command gets its own request id and its
  Ack / Error / Hello is matched on that id;
* the hold loops that re-send ``boost`` and ``drive`` at 50 Hz, because the game lets go after 0.25 s of silence;
* the :class:`~.triggers.TriggerEngine`, fed with the decoded packets it needs (at most ``TRIGGER_SAMPLE_HZ`` per
  high-rate packet type; events, Car Status and Session always).

Nothing here knows about MCP.
"""
import asyncio
import collections
import contextlib
import ipaddress
import logging
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from . import protocol as P
from . import reference as R
from .triggers import TriggerConfig, TriggerEngine

log = logging.getLogger("backseat_champions_mcp.client")

HOLD_RATE_HZ = 50.0
HOLD_CONFIRM_WINDOW_S = 0.2
MAX_BOOST_S = 30.0
MAX_DRIVE_S = 10.0
HELLO_CACHE_S = 2.0
TRIGGER_SAMPLE_HZ = 10.0
# packet types the trigger engine needs; the high-rate ones are sampled at TRIGGER_SAMPLE_HZ
_TRIGGER_SAMPLED = (P.PKT_PLAYER, P.PKT_CAR_TELEMETRY, P.PKT_LAP_DATA, P.PKT_CAR_ABILITIES)
_TRIGGER_ALWAYS = (P.PKT_CAR_STATUS, P.PKT_SESSION, P.PKT_EVENT)

# After this long without a fresh packet of a type its data is flagged stale (seconds).
STALE_AFTER_S = {P.PKT_CAR_STATUS: 3.0, P.PKT_SESSION: 3.0, P.PKT_PARTICIPANTS: 12.0}
DEFAULT_STALE_AFTER_S = 2.0


def _is_true(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _default_results_dir():
    return os.path.join(os.path.expanduser("~"), ".backseat-champions-mcp", "race-results")


@dataclass
class Config:
    """Where the game is and what this server may do. ``from_env`` reads the BC_* environment variables."""

    telemetry_host: str = "127.0.0.1"
    telemetry_port: int = 29770
    command_host: str = "127.0.0.1"
    command_port: int = 29771
    command_token: str = ""
    allow_drive: bool = False
    results_port: Optional[int] = None
    results_token: str = ""
    results_dir: str = field(default_factory=_default_results_dir)
    results_host: str = "127.0.0.1"
    game_host: str = ""              # where telemetry may come from when listening off loopback; default command_host
    command_timeout: float = 1.0
    telemetry_reuse: str = "auto"    # auto | on | off: SO_REUSEADDR / SO_REUSEPORT on the telemetry socket
    event_buffer: int = 500
    wait_max_s: float = 300.0        # cap for wait_for_trigger's timeout
    triggers: TriggerConfig = field(default_factory=TriggerConfig)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env

        def integer(name, default):
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                raise ValueError("%s must be a whole number, got %r" % (name, raw))

        def number(name, default):
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                raise ValueError("%s must be a number, got %r" % (name, raw))

        return cls(
            telemetry_host=(env.get("BC_TELEMETRY_HOST") or "127.0.0.1").strip(),
            telemetry_port=integer("BC_TELEMETRY_PORT", 29770),
            command_host=(env.get("BC_COMMAND_HOST") or "127.0.0.1").strip(),
            command_port=integer("BC_COMMAND_PORT", 29771),
            command_token=env.get("BC_COMMAND_TOKEN") or "",
            allow_drive=_is_true(env.get("BC_ALLOW_DRIVE")),
            results_port=integer("BC_RESULTS_PORT", None),
            results_token=env.get("BC_RESULTS_TOKEN") or "",
            results_dir=(env.get("BC_RESULTS_DIR") or "").strip() or _default_results_dir(),
            results_host=(env.get("BC_RESULTS_HOST") or "127.0.0.1").strip(),
            game_host=(env.get("BC_GAME_HOST") or "").strip(),
            command_timeout=number("BC_COMMAND_TIMEOUT", 1.0),
            telemetry_reuse=(env.get("BC_TELEMETRY_REUSE") or "auto").strip().lower(),
            wait_max_s=number("BC_WAIT_MAX_S", 300.0),
            triggers=TriggerConfig.from_env(env),
        )

    def validate(self):
        """Raise ``ValueError`` with a message fit for the user when a setting cannot work."""
        for name in ("telemetry_port", "command_port"):
            port = getattr(self, name)
            if not (0 <= port <= 65535):
                raise ValueError("%s %r is not a UDP port (0..65535)" % (name, port))
        if self.results_port is not None and not (0 <= self.results_port <= 65535):
            raise ValueError("results_port %r is not a TCP port (0..65535)" % (self.results_port,))
        try:
            P.check_token(self.command_token)
        except ValueError as e:
            raise ValueError("BC_COMMAND_TOKEN: %s" % e)
        if not (0.05 <= self.command_timeout <= 10):
            raise ValueError("BC_COMMAND_TIMEOUT must be between 0.05 and 10 seconds")
        if self.telemetry_reuse in ("1", "true", "yes"):
            self.telemetry_reuse = "on"
        if self.telemetry_reuse in ("0", "false", "no"):
            self.telemetry_reuse = "off"
        if self.telemetry_reuse not in ("auto", "on", "off"):
            raise ValueError("BC_TELEMETRY_REUSE must be auto, on or off")
        if not (1.0 <= self.wait_max_s <= 3600.0):
            raise ValueError("BC_WAIT_MAX_S must be between 1 and 3600 seconds")
        if self.results_port is not None and not is_loopback(self.results_host) and not self.results_token:
            raise ValueError("refusing to receive race results on %s without BC_RESULTS_TOKEN: anyone who can reach "
                             "it could upload" % self.results_host)
        return self

    def public(self):
        """The settings as they may be shown to a model: no secrets."""
        return {
            "telemetry_listen": "%s:%d" % (self.telemetry_host, self.telemetry_port),
            "command_target": "%s:%d" % (self.command_host, self.command_port),
            "command_token_set": bool(self.command_token),
            "command_timeout_s": self.command_timeout,
            "drive_tools_enabled": self.allow_drive,
            "wait_for_trigger_max_s": self.wait_max_s,
            "results_receiver": (None if self.results_port is None
                                 else "http://%s:%d/" % (self.results_host, self.results_port)),
        }


class _Entry:
    __slots__ = ("data", "header", "received", "body")

    def __init__(self, data, header, received):
        self.data = data
        self.header = header
        self.received = received
        self.body = None


class _TelemetryProtocol(asyncio.DatagramProtocol):
    def __init__(self, client):
        self.client = client

    def datagram_received(self, data, addr):
        self.client._on_telemetry(data, addr)

    def error_received(self, exc):
        log.debug("telemetry socket error: %s", exc)


class _CommandProtocol(asyncio.DatagramProtocol):
    def __init__(self, client):
        self.client = client
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.client._on_reply(data)

    def error_received(self, exc):
        self.client._on_command_socket_error(self, exc)

    def connection_lost(self, exc):
        self.client._on_command_socket_lost(self)


class _Hold:
    """One running boost or drive hold."""

    def __init__(self, kind, describe, make_packet, release_packet, duration):
        self.kind = kind
        self.describe = describe
        self.make_packet = make_packet
        self.release_packet = release_packet
        self.duration = duration
        self.started = time.time()
        self.started_mono = time.monotonic()
        self.packets_sent = 0
        self.task = None
        self.error = None
        self.ended = None            # None while running; else expired | stopped | replaced | error | shutdown
        self.ended_mono = None
        self.reported = False

    def finish(self, reason):
        if self.ended is None:
            self.ended = reason
            self.ended_mono = time.monotonic()

    def summary(self):
        now = time.monotonic()
        out = {"active": self.ended is None}
        out.update(self.describe)
        out["duration_s"] = round(self.duration, 2)
        out["packets_sent"] = self.packets_sent
        if self.ended is None:
            out["remaining_s"] = round(max(0.0, self.duration - (now - self.started_mono)), 2)
        else:
            out["ended"] = self.ended
            out["ended_ago_s"] = round(now - self.ended_mono, 2)
            out["held_for_s"] = round(self.ended_mono - self.started_mono, 2)
        if self.error:
            out["error"] = self.error
        return out


def failure(reason, command=None, detail=None, **extra):
    """A structured failure: ``{ok: False, error_reason, explanation, hint}``."""
    explanation, hint = R.explain_error(reason)
    out = {"ok": False}
    if command:
        out["command"] = command
    out["error_reason"] = reason
    out["explanation"] = explanation if not detail else "%s %s" % (explanation, detail)
    out["hint"] = hint
    out.update(extra)
    return out


class GameClient:
    def __init__(self, config):
        self.config = config
        self._token = P.check_token(config.command_token)
        self._loop = None
        self.started = False

        # telemetry listener
        self._tele_transport = None
        self.listener_error = None       # why the telemetry socket could not be bound, as text
        self.listener_errno = None
        self.telemetry_port = config.telemetry_port
        self._allowed_sources = None     # None: accept from anyone (loopback listener)
        self._cache = {}                 # (packet_id, key) -> _Entry
        self.session_uid = None
        self.seat_car = [None, None, None, None]
        self.num_cars = 0
        self.game_version = None
        self.last_packet = None          # monotonic time of the last telemetry packet
        self.counts = collections.Counter()
        self.dropped = collections.Counter()
        self.sessions_seen = 0

        # events and decisions
        self.events = collections.deque(maxlen=max(10, config.event_buffer))
        self.event_seq = 0
        self._event_waiters = set()
        self.card_event = None           # the latest CARD event for seat 0 that has not been answered
        self.tire_event = None           # the latest TIRE event for seat 0 that has not been answered
        self.pit_event = None            # the latest PITW event for seat 0

        # commands
        self._cmd_protocol = None
        self._cmd_stale = False
        self._pending = {}
        self._request_id = (int(time.time() * 1000) ^ (os.getpid() << 16)) & 0x7FFFFFFF
        self._hello = None               # (monotonic time, result dict)
        self.commands_sent = 0
        self.last_reply = None

        # holds
        self.holds = {"boost": None, "drive": None}

        self._background = set()

        # triggers
        self.triggers = TriggerEngine(config.triggers)
        self._trigger_fed = {}           # packet id -> monotonic time it was last fed to the engine

    # ------------------------------------------------------------------------------------------ lifecycle

    async def start(self):
        """Bind the telemetry socket. Never raises: a failed bind is kept in ``listener_error`` for the tools."""
        if self.started:
            return
        self.started = True
        self._loop = asyncio.get_running_loop()
        try:
            sock = self._telemetry_socket()
            self._tele_transport, _ = await self._loop.create_datagram_endpoint(
                lambda: _TelemetryProtocol(self), sock=sock)
            self.listener_error = None
            self.telemetry_port = sock.getsockname()[1]
            log.info("listening for telemetry on %s:%d", self.config.telemetry_host, self.telemetry_port)
        except (OSError, ValueError) as e:
            self._tele_transport = None
            self.listener_error = str(e)
            self.listener_errno = getattr(e, "errno", None)
            log.warning("cannot listen for telemetry on %s:%d: %s", self.config.telemetry_host,
                        self.config.telemetry_port, e)
        self._refresh_hello()

    def _refresh_hello(self):
        """Ask the game what is switched on, in the background (tells the trigger engine about Decision Events)."""
        if self._loop is None or not self.started:
            return

        async def run():
            with contextlib.suppress(Exception):
                await self.hello(max_age=0)
        self._background.add(self._loop.create_task(run()))
        self._background = {t for t in self._background if not t.done()}

    async def stop(self):
        """Let go of every hold, close both sockets."""
        if not self.started:
            return
        self.started = False
        for kind in list(self.holds):
            self._end_hold(kind, "shutdown", release=True)
        for task in list(self._background):
            task.cancel()
        for waiter in list(self._event_waiters) + list(self.triggers._waiters):
            if not waiter.done():
                waiter.cancel()
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        if self._tele_transport is not None:
            self._tele_transport.close()
            self._tele_transport = None
        if self._cmd_protocol is not None and self._cmd_protocol.transport is not None:
            self._cmd_protocol.transport.close()
        self._cmd_protocol = None
        await asyncio.sleep(0)

    def _telemetry_socket(self):
        host, port = self.config.telemetry_host, self.config.telemetry_port
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        if not is_loopback(host):
            source = self.config.game_host or self.config.command_host
            allowed = set()
            for info in socket.getaddrinfo(source, None):
                allowed.add(info[4][0].split("%", 1)[0])
            self._allowed_sources = allowed
        reuse = self.config.telemetry_reuse
        use_reuse = reuse == "on" or (reuse == "auto" and sys.platform != "win32")
        if use_reuse and port != 0:
            # With SO_REUSEADDR / SO_REUSEPORT two sockets can share a UDP port, but unicast datagrams then go to
            # only one of them (Linux spreads them, BSD/macOS picks one), so a second listener would silently
            # see a part of the stream. A plain bind first proves nobody else holds the port.
            probe = socket.socket(family, socket.SOCK_DGRAM)
            try:
                probe.bind((host, port))
            finally:
                probe.close()
        sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            # On Windows SO_REUSEADDR lets a second socket take over a port that is in use, which would hide the
            # "port already in use" case from the user, so "auto" leaves it off there.
            if use_reuse:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                    except OSError:
                        pass
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            except OSError:
                pass
            sock.setblocking(False)
            sock.bind((host, port))
        except Exception:
            sock.close()
            raise
        return sock

    # ------------------------------------------------------------------------------------------ telemetry in

    def _on_telemetry(self, data, addr):
        if self._allowed_sources is not None:
            source = addr[0].split("%", 1)[0]
            if source.startswith("::ffff:"):
                source = source[7:]
            if source not in self._allowed_sources:
                self.dropped["not from the game host"] += 1
                return
        try:
            header = P.peek_header(data)
        except P.PacketError:
            self.dropped["not a Game API packet"] += 1
            return
        pid = header["packet_id"]
        if pid in P.REPLY_PACKETS:
            self.dropped["reply on the telemetry port"] += 1
            return
        now = time.monotonic()
        if header["session_uid"] != self.session_uid:
            self._new_session(header["session_uid"])
        self.seat_car = header["seat_car"]
        self.num_cars = min(header["num_cars"], P.MAX_CARS)
        self.game_version = header["game_version"]
        self.last_packet = now
        self.counts[pid] += 1
        if pid == P.PKT_EVENT:
            body = self._on_event(data, header, now)
            self._feed_triggers(pid, header, body, now)
            return
        key = P.peek_keys(data) if pid in (P.PKT_PLAYER, P.PKT_RACING_LINE, P.PKT_AI_OBSERVATIONS) else 0
        entry = _Entry(data, header, now)
        self._cache[(pid, key)] = entry
        if pid in _TRIGGER_ALWAYS or (pid in _TRIGGER_SAMPLED and key == 0
                                      and now - self._trigger_fed.get(pid, -1e9) >= 1.0 / TRIGGER_SAMPLE_HZ):
            self._trigger_fed[pid] = now
            try:
                entry.body = P.decode_body(data, header)
            except Exception:  # a body that does not decode must not stop the listener
                self.dropped["undecodable body"] += 1
                return
            self._feed_triggers(pid, header, entry.body, now)

    def _feed_triggers(self, pid, header, body, now):
        try:
            self.triggers.feed(pid, header, body, now)
        except Exception:
            log.exception("trigger engine failed on a %s packet", P.PACKET_NAMES[pid])

    def _new_session(self, uid):
        if self.session_uid is not None:
            log.info("session %s -> %s: telemetry cache reset", self.session_uid, uid)
        self.session_uid = uid
        self.sessions_seen += 1
        self._cache.clear()
        self._trigger_fed.clear()
        self._refresh_hello()
        self.card_event = None
        self.tire_event = None
        self.pit_event = None

    def latest(self, packet_id, key=0):
        """``(header, body, age_s)`` of the newest packet of that type, or ``None`` when none has arrived."""
        entry = self._cache.get((packet_id, key))
        if entry is None:
            return None
        if entry.body is None:
            entry.body = P.decode_body(entry.data, entry.header)
        return entry.header, entry.body, time.monotonic() - entry.received

    def keys(self, packet_id):
        """The seats (Player, Racing Line) or cars (AI Observations) a packet type has been received for."""
        return sorted(key for (pid, key) in self._cache if pid == packet_id)

    def my_car(self):
        """Car index of seat 0 (player one), or ``None`` when the session has no such player."""
        return self.seat_car[0]

    def telemetry_age(self):
        return None if self.last_packet is None else time.monotonic() - self.last_packet

    def in_session(self):
        age = self.telemetry_age()
        return age is not None and age < 3.0

    @staticmethod
    def stale_after(packet_id):
        return STALE_AFTER_S.get(packet_id, DEFAULT_STALE_AFTER_S)

    # ------------------------------------------------------------------------------------------ events

    def _on_event(self, data, header, now):
        body = P.decode_body(data, header)
        self.event_seq += 1
        event = {"seq": self.event_seq, "code": body["code"], "seat": body["seat"]}
        for name, value in body.items():
            if name not in ("code", "seat"):
                event[name] = value
        event["session_time_s"] = round(header["session_time"], 2)
        event["session_uid"] = header["session_uid"]
        event["_received"] = now
        self.events.append(event)

        code, seat = body["code"], body["seat"]
        if seat == 0:
            if code == "CARD":
                self.card_event = event
            elif code == "TIRE":
                self.tire_event = event
            elif code == "PITW":
                self.pit_event = event
            elif code == "PITO":
                self.tire_event = None       # back on track: the pit stop's tire screen is over
            elif code == "LAPC":
                if self.tire_event is not None and self.tire_event.get("pre_race"):
                    self.tire_event = None   # laps are being counted: the pre-race tire screen is over
            elif code == "FINI":
                self.tire_event = None
        if code == "SEND":
            self.card_event = None
            self.tire_event = None
        for waiter in list(self._event_waiters):
            if not waiter.done():
                waiter.set_result(None)
        self._event_waiters.clear()
        return body

    @staticmethod
    def public_event(event):
        """The event as shown to a model: without internal fields, with its age."""
        out = {k: v for k, v in event.items() if not k.startswith("_") and k != "session_uid"}
        out["age_s"] = round(time.monotonic() - event["_received"], 2)
        return out

    def events_after(self, since_seq=0, codes=None, limit=None):
        """Events with ``seq > since_seq`` (oldest first), optionally only some codes, at most ``limit`` newest."""
        wanted = None if not codes else {c.upper() for c in codes}
        found = [e for e in self.events if e["seq"] > since_seq and (wanted is None or e["code"] in wanted)]
        if limit is not None and len(found) > limit:
            found = found[-limit:]
        return found

    def oldest_seq(self):
        return self.events[0]["seq"] if self.events else self.event_seq + 1

    async def wait_for_event(self, codes=None, timeout=30.0, since_seq=None, on_tick=None, tick=5.0):
        """Wait until an event with ``seq > since_seq`` (default: later than now) matches; ``[]`` on timeout."""
        if since_seq is None:
            since_seq = self.event_seq
        deadline = time.monotonic() + timeout
        while True:
            found = self.events_after(since_seq, codes)
            if found:
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            waiter = self._loop.create_future()
            self._event_waiters.add(waiter)
            try:
                await asyncio.wait_for(waiter, min(remaining, tick))
            except asyncio.TimeoutError:
                if on_tick is not None:
                    await on_tick(timeout - max(0.0, deadline - time.monotonic()), timeout)
            finally:
                self._event_waiters.discard(waiter)

    # ------------------------------------------------------------------------------------------ commands out

    def _next_request_id(self):
        self._request_id = (self._request_id + 1) & 0xFFFFFFFF
        return self._request_id

    async def _command_transport(self):
        protocol = self._cmd_protocol
        if protocol is not None and not self._cmd_stale and protocol.transport is not None \
                and not protocol.transport.is_closing():
            return protocol.transport
        if protocol is not None and protocol.transport is not None:
            protocol.transport.close()
        self._cmd_stale = False
        host = self.config.command_host
        family = socket.AF_INET6 if ":" in host else socket.AF_UNSPEC
        # Connected, so the system only hands us datagrams that come from the game's command address.
        _, protocol = await self._loop.create_datagram_endpoint(
            lambda: _CommandProtocol(self), remote_addr=(host, self.config.command_port), family=family)
        self._cmd_protocol = protocol
        return protocol.transport

    def _on_command_socket_error(self, protocol, exc):
        if protocol is not self._cmd_protocol:
            return
        # Typically ICMP "port unreachable": nothing listens on the command port. Some event loops stop reading
        # after such an error, so the socket is replaced before the next command.
        self._cmd_stale = True
        refused = isinstance(exc, (ConnectionRefusedError, ConnectionResetError))
        reason = "game_unreachable" if refused else "send_failed"
        detail = "(%s:%d: %s)" % (self.config.command_host, self.config.command_port, exc)
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result(("local", {"reason": reason, "detail": detail}))
        for kind, hold in self.holds.items():
            if hold is not None and hold.ended is None:
                hold.error = failure(reason, kind, detail)
                self._end_hold(kind, "error", release=False)

    def _on_command_socket_lost(self, protocol):
        if protocol is self._cmd_protocol:
            self._cmd_stale = True

    def _on_reply(self, data):
        try:
            header, body = P.decode(data)
        except P.PacketError:
            self.dropped["bad reply"] += 1
            return
        pid = header["packet_id"]
        if pid not in P.REPLY_PACKETS:
            return
        self.last_reply = time.monotonic()
        self.game_version = header["game_version"]
        kind = P.PACKET_NAMES[pid]
        future = self._pending.get(body["request_id"])
        if future is not None:
            if not future.done():
                future.set_result((kind, body))
            return
        # An Error nobody waits for: the hold loops' own packets are only ever answered with errors.
        if pid == P.PKT_ERROR and body["command"] in ("boost", "drive"):
            hold = self.holds.get(body["command"])
            if hold is not None and hold.ended is None:
                hold.error = failure(body["reason"], body["command"])
                self._end_hold(body["command"], "error", release=False)

    async def request(self, command_id, build=None, timeout=None):
        """Send one command and wait for the reply with its request id.

        ``build(request_id, token)`` returns the datagram (one of the ``protocol.encode_*`` functions); without it
        the command has no payload. Returns ``{ok: True, command, request_id, latency_ms}`` (plus ``hello`` for the
        hello command) or a :func:`failure` dict. Never raises for network trouble.
        """
        name = P.COMMAND_NAMES[command_id]
        timeout = self.config.command_timeout if timeout is None else timeout
        request_id = self._next_request_id()
        data = (build(request_id, self._token) if build is not None
                else P.encode_command(command_id, b"", request_id, self._token))
        future = self._loop.create_future()
        self._pending[request_id] = future
        started = time.monotonic()
        try:
            transport = await self._command_transport()
            transport.sendto(data)
            self.commands_sent += 1
            kind, body = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return failure("no_reply", name, "(no reply from %s:%d within %.2f s)"
                           % (self.config.command_host, self.config.command_port, timeout), request_id=request_id)
        except OSError as e:
            self._cmd_stale = True
            return failure("send_failed", name, "(%s:%d: %s)" % (self.config.command_host,
                                                                  self.config.command_port, e))
        finally:
            self._pending.pop(request_id, None)
        latency = round((time.monotonic() - started) * 1000, 1)
        if kind == "local":
            return failure(body["reason"], name, body["detail"])
        if kind == "error":
            return failure(body["reason"], name, request_id=request_id, error_code=body["reason_code"],
                           latency_ms=latency)
        out = {"ok": True, "command": name, "request_id": request_id, "latency_ms": latency}
        if kind == "hello":
            out["hello"] = {k: v for k, v in body.items() if k != "request_id"}
        return out

    async def silent_command(self, command_id, build, window=HOLD_CONFIRM_WINDOW_S):
        """Send a command the game only answers when it refuses it (boost, drive).

        Silence alone would look the same as "no game", so a ping goes out with it: the result is ok when the ping
        is acknowledged and no Error for the command arrives within ``window`` seconds.
        """
        name = P.COMMAND_NAMES[command_id]
        request_id = self._next_request_id()
        future = self._loop.create_future()
        self._pending[request_id] = future
        started = time.monotonic()
        try:
            transport = await self._command_transport()
            transport.sendto(build(request_id, self._token))
            self.commands_sent += 1
            ping = await self.request(P.CMD_PING, timeout=min(self.config.command_timeout, 0.5))
            if not ping["ok"] and not future.done():
                ping["command"] = name
                return ping
            try:
                kind, body = await asyncio.wait_for(future, max(0.0, window - (time.monotonic() - started)))
            except asyncio.TimeoutError:
                return {"ok": True, "command": name, "request_id": request_id,
                        "note": "The game answers this command only when it refuses it; it stayed silent."}
        except OSError as e:
            self._cmd_stale = True
            return failure("send_failed", name, "(%s)" % e)
        finally:
            self._pending.pop(request_id, None)
        if kind == "local":
            return failure(body["reason"], name, body["detail"])
        if kind == "error":
            return failure(body["reason"], name, request_id=request_id, error_code=body["reason_code"])
        return {"ok": True, "command": name, "request_id": request_id}

    async def hello(self, max_age=HELLO_CACHE_S, timeout=None):
        """The game's Hello (what is switched on), cached briefly. ``{ok: True, hello: {...}}`` or a failure."""
        now = time.monotonic()
        if self._hello is not None and now - self._hello[0] <= max_age:
            return self._hello[1]
        result = await self.request(P.CMD_HELLO, timeout=timeout if timeout is not None
                                    else min(self.config.command_timeout, 0.6))
        self._hello = (time.monotonic(), result)
        if result.get("ok"):
            self.triggers.set_decision_events(result["hello"]["decision_events"])
        return result

    def forget_hello(self):
        self._hello = None

    # ------------------------------------------------------------------------------------------ the commands
    # Seat is always 0: the game refuses any other seat with not_your_car.

    async def ping(self):
        return await self.request(P.CMD_PING)

    async def pick_card(self, index):
        result = await self.request(P.CMD_PICK_CARD, lambda rid, tok: P.encode_pick_card(index, 0, rid, tok))
        self.note_answered("pick_card", result, index)
        return result

    async def ban_card(self, index):
        result = await self.request(P.CMD_BAN_CARD, lambda rid, tok: P.encode_ban_card(index, 0, rid, tok))
        self.note_answered("ban_card", result, index)
        return result

    async def set_pit(self, pit):
        return await self.request(P.CMD_SET_PIT, lambda rid, tok: P.encode_set_pit(pit, 0, rid, tok))

    async def pick_tire(self, compound):
        result = await self.request(P.CMD_PICK_TIRE, lambda rid, tok: P.encode_pick_tire(compound, 0, rid, tok))
        self.note_answered("pick_tire", result)
        return result

    async def set_camera(self, camera):
        return await self.request(P.CMD_SET_CAMERA, lambda rid, tok: P.encode_set_camera(camera, 0, rid, tok))

    async def boost(self, buttons, duration):
        """Hold boost buttons (bit mask) for ``duration`` seconds; see :meth:`start_hold`."""
        token = self._token
        describe = {"speed": bool(buttons & P.BOOST_SPEED), "cornering": bool(buttons & P.BOOST_CORNERING),
                    "party_mode": bool(buttons & P.BOOST_PARTY_MODE)}
        return await self.start_hold(
            "boost", describe,
            lambda rid: P.encode_boost(buttons, 0, rid, token),
            lambda rid: P.encode_boost(0, 0, rid, token),
            min(max(duration, 0.0), MAX_BOOST_S))

    async def stop_boost(self):
        """Stop the boost hold and tell the game to let go of every boost button now."""
        was_holding = self.stop_hold("boost")
        result = await self.silent_command(P.CMD_BOOST, lambda rid, tok: P.encode_boost(0, 0, rid, tok))
        result["was_holding"] = was_holding
        return result

    async def drive(self, pedal, steer, duration):
        """Hold pedal and steering for ``duration`` seconds; see :meth:`start_hold`."""
        token = self._token
        return await self.start_hold(
            "drive", {"pedal": round(pedal, 3), "steer": round(steer, 3)},
            lambda rid: P.encode_drive(pedal, steer, 0, rid, token),
            lambda rid: P.encode_release_drive(0, rid, token),
            min(max(duration, 0.0), MAX_DRIVE_S))

    async def release_drive(self):
        """Stop the drive hold and hand the car back to the built-in driver now."""
        was_holding = self.stop_hold("drive")
        result = await self.request(P.CMD_RELEASE_DRIVE, lambda rid, tok: P.encode_release_drive(0, rid, tok))
        result["was_holding"] = was_holding
        return result

    # ------------------------------------------------------------------------------------------ decisions

    def note_answered(self, command, result, index=None):
        """Keep the open decisions in step with a command's outcome."""
        reason = result.get("error_reason")
        if command == "pick_card":
            if result["ok"] or reason in ("no_points", "cards_unavailable"):
                self.card_event = None       # a new hand arrives as a new CARD event
        elif command == "ban_card":
            if result["ok"] and self.card_event is not None and index is not None:
                for card in self.card_event.get("hand", []):
                    if card["index"] == index:
                        card["banned"] = True
        elif command == "pick_tire":
            if result["ok"] or reason == "tire_select_closed":
                self.tire_event = None

    # ------------------------------------------------------------------------------------------ holds

    def _send_now(self, data):
        protocol = self._cmd_protocol
        if protocol is None or protocol.transport is None or protocol.transport.is_closing():
            return False
        try:
            protocol.transport.sendto(data)
            self.commands_sent += 1
            return True
        except OSError:
            return False

    def _end_hold(self, kind, reason, release):
        hold = self.holds.get(kind)
        if hold is None or hold.ended is not None:
            return
        hold.finish(reason)
        if hold.task is not None and hold.task is not asyncio.current_task():
            hold.task.cancel()
        if release and hold.release_packet is not None:
            self._send_now(hold.release_packet(self._next_request_id()))

    async def _hold_loop(self, hold):
        period = 1.0 / HOLD_RATE_HZ
        loop = self._loop
        end = loop.time() + hold.duration
        next_tick = loop.time()
        try:
            while hold.ended is None and loop.time() < end:
                transport = await self._command_transport()
                transport.sendto(hold.make_packet(self._next_request_id()))
                self.commands_sent += 1
                hold.packets_sent += 1
                next_tick += period
                delay = next_tick - loop.time()
                if delay < -5 * period:          # fell far behind (a stalled loop): do not burst to catch up
                    next_tick = loop.time()
                    delay = 0.0
                await asyncio.sleep(max(delay, 0.0))
            if hold.ended is None:
                hold.finish("expired")
                if hold.release_packet is not None:
                    self._send_now(hold.release_packet(self._next_request_id()))
        except asyncio.CancelledError:
            hold.finish("stopped")
            raise
        except OSError as e:
            hold.error = failure("send_failed", hold.kind, "(%s)" % e)
            hold.finish("error")
        except Exception as e:  # the loop must never die silently
            log.exception("%s hold failed", hold.kind)
            hold.error = failure("send_failed", hold.kind, "(%s)" % e)
            hold.finish("error")

    async def start_hold(self, kind, describe, make_packet, release_packet, duration):
        """Start re-sending a boost or drive command at 50 Hz for ``duration`` seconds, replacing a running one.

        Returns once the game had a moment to refuse it: a failure dict if it did (or if the game does not answer
        a ping), else ``{ok: True, hold: {...}}``. The hold keeps running in the background after this returns.
        """
        self._end_hold(kind, "replaced", release=False)
        hold = _Hold(kind, describe, make_packet, release_packet, duration)
        self.holds[kind] = hold
        started = time.monotonic()
        try:
            await self._command_transport()
        except OSError as e:
            hold.error = failure("send_failed", kind, "(%s)" % e)
            hold.finish("error")
            hold.reported = True
            return hold.error
        hold.task = asyncio.ensure_future(self._hold_loop(hold))
        ping = await self.request(P.CMD_PING, timeout=min(self.config.command_timeout, 0.5))
        if hold.error is None and not ping["ok"]:
            ping["command"] = kind
            hold.error = ping
            self._end_hold(kind, "error", release=False)
        remaining = HOLD_CONFIRM_WINDOW_S - (time.monotonic() - started)
        if hold.error is None and remaining > 0:
            await asyncio.sleep(remaining)
        if hold.error is not None:
            hold.reported = True
            return dict(hold.error)
        return {"ok": True, "command": kind, "hold": hold.summary()}

    def stop_hold(self, kind):
        """Stop a running hold and send its release. Returns whether one was running."""
        hold = self.holds.get(kind)
        running = hold is not None and hold.ended is None
        if running:
            self._end_hold(kind, "stopped", release=False)
        return running

    def hold_status(self, mark_reported=True):
        """State of both holds for status tools; an error that ended a hold is listed until it has been shown."""
        out = {}
        for kind, hold in self.holds.items():
            if hold is None:
                continue
            if hold.ended is not None and hold.error is None and time.monotonic() - hold.ended_mono > 60:
                continue
            summary = hold.summary()
            if hold.error is not None:
                summary["error_unseen"] = not hold.reported
                if mark_reported:
                    hold.reported = True
            out[kind] = summary
        return out

    # ------------------------------------------------------------------------------------------ status

    def listener_status(self):
        out = {"bound": self._tele_transport is not None,
               "address": "%s:%d" % (self.config.telemetry_host, self.telemetry_port)}
        if self.listener_error:
            out["error"] = self.listener_error
        age = self.telemetry_age()
        out["receiving"] = age is not None and age < 3.0
        out["last_packet_age_s"] = None if age is None else round(age, 2)
        out["packets_received"] = sum(self.counts.values())
        out["packets_by_type"] = {P.PACKET_NAMES[pid]: n for pid, n in sorted(self.counts.items())}
        if self.dropped:
            out["dropped"] = dict(self.dropped)
        out["session_uid"] = self.session_uid
        out["sessions_seen"] = self.sessions_seen
        out["events_buffered"] = len(self.events)
        out["last_event_seq"] = self.event_seq
        return out

    def listener_failure(self):
        """The failure dict every read tool returns while the telemetry socket is not bound."""
        address = "%s:%d" % (self.config.telemetry_host, self.config.telemetry_port)
        text = self.listener_error or "the listener is not running"
        in_use = "in use" in text.lower() or self.listener_errno in (98, 48, 10048, 10013)
        if in_use:
            hint = ("Another program is already receiving the game's telemetry on %s: a second copy of this server "
                    "(for instance one started by another MCP client), gameapi_client.py, or another telemetry "
                    "tool. Close it and restart this server, or add a second telemetry target with its own port "
                    "in the game (%s) and set BC_TELEMETRY_PORT to it. Commands (pick_card, set_pit, ...) and "
                    "get_api_status still work." % (address, R.SETTINGS_PATH))
        else:
            hint = ("Check BC_TELEMETRY_HOST / BC_TELEMETRY_PORT: the address must belong to this machine and the "
                    "port must be free. Restart this server after fixing it. Commands and get_api_status still "
                    "work.")
        return {"ok": False, "error_reason": "telemetry_listener_down",
                "explanation": "This server could not open its telemetry socket on %s (%s), so it receives no race "
                               "data." % (address, text),
                "hint": hint}

    async def no_data_failure(self, packet_id, what=None):
        """Why there is no packet of that type, as precisely as ``hello`` and the listener state allow."""
        if self._tele_transport is None:
            return self.listener_failure()
        name = P.PACKET_NAMES[packet_id]
        what = what or name
        address = "%s:%d" % (self.config.telemetry_host, self.config.telemetry_port)
        switches = R.PACKET_SWITCHES.get(packet_id, ())
        labels = [R.BROADCAST_SWITCHES[s]["label"] for s in switches]
        need = " or ".join(labels) if len(labels) < 4 else "at least one broadcast option"
        hello = await self.hello()
        if not hello["ok"]:
            return {"ok": False, "error_reason": "no_data",
                    "explanation": "No %s data has been received on %s, and the game did not answer hello on "
                                   "%s:%d (%s)." % (what, address, self.config.command_host,
                                                    self.config.command_port, hello["error_reason"]),
                    "hint": "Start Backseat Champions and switch on the Game API under %s (it is off by default), "
                            "with %s ticked. If the game runs with other ports or on another machine, set "
                            "BC_COMMAND_HOST / BC_COMMAND_PORT / BC_TELEMETRY_PORT (and BC_COMMAND_TOKEN for "
                            "another machine)." % (R.SETTINGS_PATH, need),
                    "game_reachable": False}
        flags = hello["hello"]
        out = {"ok": False, "error_reason": "no_data", "game_reachable": True,
               "game_version": flags["game_version"]}
        if switches and not any(flags.get(s) for s in switches):
            out["error_reason"] = "switch_off"
            out["explanation"] = ("The game is running, but %s is switched off, so it does not send %s data."
                                  % (need, what))
            out["hint"] = "Tick %s under %s in the game, then call this tool again." % (need, R.SETTINGS_PATH)
            out["switch"] = list(switches)
            return out
        if not self.in_session():
            out["error_reason"] = "not_in_session"
            out["explanation"] = ("The game is running and %s is on, but no telemetry is arriving on %s. The game "
                                  "sends no telemetry outside a session." % (need, address))
            out["hint"] = ("Start or join a race, then retry (wait_for_event with codes=['SSTA'] returns when a "
                           "session's cars are on the grid, if Decision Events is on). If a race is already "
                           "running: check that the telemetry targets under %s include %s, and that no other "
                           "program is receiving on that port." % (R.SETTINGS_PATH, address))
            return out
        out["explanation"] = "Telemetry is arriving, but no %s packet has been received in this session yet." % what
        if packet_id == P.PKT_AI_OBSERVATIONS:
            out["hint"] = ("AI Observations are only sent by the machine that simulates the cars: single player, "
                           "splitscreen, or the host of an online lobby. As an online client there are none.")
        elif packet_id == P.PKT_PARTICIPANTS:
            out["hint"] = "Participants are sent every 5 seconds and when a session starts; retry in a few seconds."
        elif packet_id == P.PKT_EVENT:
            out["hint"] = "No event has happened since this server started listening; use wait_for_event."
        else:
            out["hint"] = "Retry in a moment; the switch may have just been turned on."
        return out
