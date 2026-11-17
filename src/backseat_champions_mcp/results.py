"""Optional HTTP receiver for the race results the game uploads when you host.

The game sends one ``POST`` per finished race weekend to every endpoint listed under Race Results in its Telemetry
tab: a JSON document (or a ZIP holding it as ``results.json``) with ``X-BC-*`` headers and, when the endpoint has a
token, ``Authorization: Bearer <token>``. Nothing is retried, so this receiver saves every upload it can read and
answers 2xx for it.

The same guards as the game's own ``gameapi_client.py results-server``: a request with an ``Origin`` header or
without ``X-BC-Format`` is refused (a web page in a browser cannot forge that), bodies over 16 MB and ZIPs that
unpack to over 256 MB are refused, and it only listens off loopback when a token is set.

Unlike the reference it answers 200 for a document that fails these checks but is readable JSON (nothing is
retried, so it is kept), and records the problems next to it; a document with a ``format`` other than 2 is kept
too, but ``load_summary`` refuses it. Anything it cannot read (bad ZIP, bad JSON, nested deeper than 64 levels) gets
422. The body is stored as received (a ZIP stays a ZIP), files are private to the user (0600, folder 0700), at most
``BC_RESULTS_KEEP`` uploads are kept (checked when the receiver starts and after each upload), the request line
and headers must arrive within 10 s of the connection being served and the body within 30 s (more for a body over
3.75 MB: 1 s per 128 KiB, at most 120 s), a connection that sends nothing for 30 s is closed, at most 16 connections
are served at once (a new one waits up to 11 s for a free slot) and only one upload is unpacked / parsed at a time.

Every file is written to a temporary name in the same folder, flushed to disk and renamed into place (headers first,
the results file last), so a reader never sees a partial file and a failed save (disk full) leaves nothing behind.

Runs in a daemon thread next to the asyncio server; it shares nothing with it but the folder on disk.
"""
import calendar
import datetime
import errno
import hmac
import http.server
import io
import json
import logging
import math
import os
import re
import socket
import stat
import threading
import time
import zipfile

log = logging.getLogger("backseat_champions_mcp.results")

MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_DEPTH = 64                  # JSON nesting accepted (the format-2 document nests about 8 deep)
SUPPORTED_FORMAT = 2
REQUEST_TIMEOUT_S = 30.0        # a connection that sends nothing for this long (headers or body) is closed
HEADER_DEADLINE_S = 10.0        # request line + headers must arrive within this, from when the connection is served
BODY_DEADLINE_MIN_S = 30.0      # the whole body must arrive within max(MIN, length / RATE), capped at MAX
BODY_DEADLINE_MAX_S = 120.0
BODY_MIN_RATE = 128 * 1024      # bytes/s: 3.75 MB fits the 30 s floor, 15 MB reaches the 120 s ceiling
MAX_CONNECTIONS = 16            # connections served at once
SLOT_WAIT_S = HEADER_DEADLINE_S + 1.0   # a new connection waits this long for a free one before it is closed
STALE_TEMP_S = 3600.0           # temp files / orphan headers files older than this are deleted when pruning
MAX_PARSING = 1                 # uploads / stored files unpacked and parsed at the same time
LISTEN_BACKLOG = 64
DEFAULT_KEEP = 100              # BC_RESULTS_KEEP: uploads kept, oldest deleted first; 0 keeps all
RESULT_SUFFIX = ".results.json"
ZIP_SUFFIX = ".results.zip"
HEADERS_SUFFIX = ".headers.json"
# New ids are UTC with a trailing Z; ids without it (0.1.1 and older) were local time.
_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9]{6}Z?$")
_TEMP = re.compile(r"^\.[0-9]{8}-[0-9]{6}-[0-9]{6}Z?\.[0-9a-f]{16}\.tmp$")
_DIGITS = re.compile(r"^[0-9]{1,12}$")
_REQUIRED = ("format", "game_version", "sessions")
_TOP_KEYS = ("format", "game_version", "unity_version", "schema_version", "schema_hash", "timepoint", "editor",
             "demo", "public_lobby")
_DECISION_DROPPED = ("state", "offered")


class ResultsError(Exception):
    """A stored upload the tools cannot use. ``reason`` is an error reason of reference.py
    (stored_file_unreadable, unsupported_format, bad_argument); ``explanation`` says what is wrong;
    ``details`` holds extra fields for the tool result (id, format, problems)."""

    def __init__(self, reason, explanation, **details):
        super().__init__("%s: %s" % (reason, explanation))
        self.reason = reason
        self.explanation = explanation
        self.details = details


def unzip_results(body):
    """The ``results.json`` entry of a "Send as ZIP" upload, as a bytearray.

    zipfile never inflates past the entry's declared size (and checks the CRC), so a declared size over
    MAX_UNPACKED_BYTES is refused before inflating anything and the buffer is allocated once, at its final size."""
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = archive.namelist()
        if names != ["results.json"]:
            raise ValueError("expected a ZIP holding only results.json, got %r" % names[:5])
        size = archive.getinfo("results.json").file_size
        if size > MAX_UNPACKED_BYTES:
            raise ValueError("the ZIP unpacks to more than %d bytes" % MAX_UNPACKED_BYTES)
        out = bytearray(size)
        view = memoryview(out)
        got = 0
        with archive.open("results.json") as entry:
            while got < size:
                n = entry.readinto(view[got:got + (1 << 20)])
                if not n:
                    break
                got += n
            if entry.read(1):                       # defensive: never more than declared
                raise ValueError("the ZIP entry is longer than it declares")
        view.release()
        if got != size:
            raise ValueError("the ZIP entry ended after %d of %d bytes" % (got, size))
    return out


def depth_ok(value, limit=MAX_DEPTH):
    """Whether a parsed JSON value nests at most ``limit`` deep (walked without recursion)."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if depth > limit:
            return False
        stack.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return True


def parse_upload(body, zipped):
    """The document of an upload; raises ``ValueError`` for anything unreadable (never anything else)."""
    try:
        raw = unzip_results(body) if zipped else body
        document = json.loads(raw)          # bytes / bytearray: no separate decoded copy kept by us
        del raw
        if not depth_ok(document):
            raise ValueError("JSON nested deeper than %d levels" % MAX_DEPTH)
        return document
    except ValueError:
        raise
    except Exception as e:      # BadZipFile, encrypted entries, unknown compression, zlib errors, RecursionError, ...
        raise ValueError("%s: %s" % (type(e).__name__, e))


def check_document(document, header_format=None):
    """Problems that make a results document unusable for the tools (not a full schema validation)."""
    if not isinstance(document, dict):
        return ["the document is not a JSON object"]
    problems = ["missing %s" % key for key in _REQUIRED if key not in document]
    fmt = document.get("format")
    if "format" in document and not _is_supported_format(fmt):
        problems.append("unsupported format %r (this server reads format %d)" % (fmt, SUPPORTED_FORMAT))
    if header_format is not None and header_format.strip() != str(SUPPORTED_FORMAT):
        problems.append("X-BC-Format header is %r, not %d" % (header_format, SUPPORTED_FORMAT))
    sessions = document.get("sessions")
    if sessions is not None and not isinstance(sessions, list):
        problems.append("sessions is not an array")
    for i, session in enumerate(sessions if isinstance(sessions, list) else []):
        if not isinstance(session, dict) or not isinstance(session.get("classification"), list):
            problems.append("sessions[%d] has no classification array" % i)
    return problems


def _is_supported_format(fmt):
    return not isinstance(fmt, bool) and isinstance(fmt, int) and fmt == SUPPORTED_FORMAT


def id_time(result_id):
    """Unix time an upload id stands for (old local-time ids are read as local time). ``ValueError`` if invalid."""
    text = result_id[:-1] if result_id.endswith("Z") else result_id
    stamp = datetime.datetime.strptime(text, "%Y%m%d-%H%M%S-%f")
    if result_id.endswith("Z"):
        return calendar.timegm(stamp.timetuple()) + stamp.microsecond / 1e6
    return time.mktime(stamp.timetuple()) + stamp.microsecond / 1e6


def _utc_iso(unix):
    return datetime.datetime.fromtimestamp(unix, datetime.timezone.utc).isoformat(timespec="seconds")


def body_deadline(length):
    """Seconds the whole body of ``length`` bytes may take to arrive."""
    return min(BODY_DEADLINE_MAX_S, max(BODY_DEADLINE_MIN_S, length / BODY_MIN_RATE))


def _write_all(fd, data):
    view = memoryview(data)
    try:
        while view:
            n = os.write(fd, view[:1 << 20])
            if n <= 0:
                raise OSError(errno.EIO, "write returned %r" % n)
            view = view[n:]
    finally:
        view.release()


def _write_private(path, data):
    """Write ``data`` to ``path`` atomically: a new 0600 temp file in the same folder (O_EXCL, O_NOFOLLOW), flushed
    to disk, then renamed over ``path``. On any error the temp file is deleted and ``path`` is untouched."""
    folder, name = os.path.split(path)
    stamp = name.split(".", 1)[0]
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0))
    while True:
        temp = os.path.join(folder, ".%s.%s.tmp" % (stamp, os.urandom(8).hex()))
        try:
            fd = os.open(temp, flags, 0o600)
            break
        except FileExistsError:
            continue
    try:
        try:
            os.fchmod(fd, 0o600)          # in case the umask or the platform ignored the mode
        except (AttributeError, OSError):
            pass
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temp, path)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    _fsync_dir(folder)


def _fsync_dir(folder):
    """Make the rename durable (best effort; not possible on Windows)."""
    try:
        fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _remove_quietly(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        log.debug("cannot delete %s: %s", path, e)


def _int_or_none(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _DIGITS.match(value.strip()):
        return int(value.strip())
    return None


def _plain(value):
    """A JSON value with NaN/inf replaced by None (json.loads accepts the NaN literals). Never rounds."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for k, v in item.items():
                if isinstance(v, float) and not math.isfinite(v):
                    item[k] = None
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(item, list):
            for i, v in enumerate(item):
                if isinstance(v, float) and not math.isfinite(v):
                    item[i] = None
                elif isinstance(v, (dict, list)):
                    stack.append(v)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _steam_str(value):
    if value is None or isinstance(value, bool):
        return value
    return str(value)


class ResultsStore:
    """The uploads saved in one folder: ``<id>.results.json`` (or ``.results.zip``, the body as received) and
    ``<id>.headers.json``. Ids are UTC times; at most ``keep`` uploads are kept (oldest deleted; 0 keeps all).

    An upload exists once both of its files do: the headers file is renamed into place first and the results file
    last, so ids() never lists a half-saved upload. Temp files (``.<id>.<hex>.tmp``) are never listed."""

    def __init__(self, folder, keep=DEFAULT_KEEP):
        # "~/x" and relative paths: expanded and made absolute once (relative to the working directory at start)
        folder = os.fspath(folder)
        absolute = os.path.abspath(os.path.expanduser(folder))
        if absolute != os.path.normpath(folder):
            log.info("race results folder %r resolved to %s", folder, absolute)
        self.folder = absolute
        self.keep = keep
        self._lock = threading.Lock()
        # shared by the receiver and load(): parsing up to 256 MB of JSON at once is bounded process-wide
        self.parsing = threading.BoundedSemaphore(MAX_PARSING)

    def makedirs(self):
        """Create the folder 0700; an existing folder is made private too (best effort)."""
        os.makedirs(self.folder, mode=0o700, exist_ok=True)
        try:
            if os.stat(self.folder).st_mode & 0o077:
                os.chmod(self.folder, 0o700)
        except OSError as e:
            log.debug("cannot make %s private: %s", self.folder, e)

    # ------------------------------------------------------------------ writing

    def save(self, body, headers=None, problems=None, zipped=False):
        """Store an upload as received; returns its id (a UTC time). Raises ``OSError`` when it cannot be saved;
        then nothing of it is left in the folder."""
        self.makedirs()
        with self._lock:
            stamp = self._new_id()
            meta = dict(headers or {})
            meta["received_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            if problems:
                meta["problems"] = list(problems)
            headers_path = os.path.join(self.folder, stamp + HEADERS_SUFFIX)
            _write_private(headers_path, json.dumps(meta, indent=1).encode("utf-8"))
            try:
                _write_private(os.path.join(self.folder, stamp + (ZIP_SUFFIX if zipped else RESULT_SUFFIX)), body)
            except BaseException:
                _remove_quietly(headers_path)
                raise
            self.prune()
        return stamp

    def _new_id(self):
        while True:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S-%f") + "Z"
            if self._path(stamp) is None and not os.path.lexists(os.path.join(self.folder, stamp + HEADERS_SUFFIX)):
                return stamp
            time.sleep(0.000001)                    # same microsecond twice

    def prune(self):
        """Delete the oldest uploads beyond ``keep`` (nothing when keep is 0), and leftovers of saves that were cut
        off (temp files and headers files without a results file) once they are STALE_TEMP_S old."""
        self._remove_leftovers()
        if not self.keep or self.keep <= 0:
            return 0
        removed = 0
        for rid in self.ids()[self.keep:]:
            for suffix in (RESULT_SUFFIX, ZIP_SUFFIX, HEADERS_SUFFIX):     # results file first: gone from ids()
                _remove_quietly(os.path.join(self.folder, rid + suffix))
            removed += 1
        if removed:
            log.info("race results: deleted the %d oldest upload(s) (BC_RESULTS_KEEP=%d)", removed, self.keep)
        return removed

    def _remove_leftovers(self):
        try:
            names = os.listdir(self.folder)
        except OSError:
            return
        present = set(names)
        cutoff = time.time() - STALE_TEMP_S
        for n in names:
            if _TEMP.match(n):
                pass
            elif n.endswith(HEADERS_SUFFIX) and _ID.match(n[:-len(HEADERS_SUFFIX)]):
                rid = n[:-len(HEADERS_SUFFIX)]
                if rid + RESULT_SUFFIX in present or rid + ZIP_SUFFIX in present:
                    continue
            else:
                continue
            path = os.path.join(self.folder, n)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if st.st_mtime < cutoff and not stat.S_ISDIR(st.st_mode):
                _remove_quietly(path)
                log.info("race results: deleted %s left over from a save that did not finish", n)

    # ------------------------------------------------------------------ reading

    def ids(self):
        """Upload ids, newest first (by the time they stand for, so old local-time ids sort correctly). Only uploads
        with both a results file and a headers file count (a save in progress or cut off is not listed)."""
        try:
            names = os.listdir(self.folder)
        except OSError:
            return []
        present = set(names)
        found = {}
        for n in names:
            for suffix in (RESULT_SUFFIX, ZIP_SUFFIX):
                if n.endswith(suffix):
                    rid = n[:-len(suffix)]
                    if _ID.match(rid) and rid not in found and rid + HEADERS_SUFFIX in present:
                        try:
                            found[rid] = id_time(rid)
                        except (ValueError, OverflowError):
                            pass                    # id-shaped but not a time (month 13): not ours
        return sorted(found, key=lambda i: (found[i], i), reverse=True)

    def _path(self, result_id):
        for suffix in (RESULT_SUFFIX, ZIP_SUFFIX):
            path = os.path.join(self.folder, result_id + suffix)
            if os.path.lexists(path):
                return path
        return None

    def resolve(self, result_id):
        """A stored id for ``"latest"`` (or None / "") or an id; ``None`` when there is no such upload."""
        ids = self.ids()
        if not ids:
            return None
        if result_id is None or str(result_id).strip() in ("", "latest"):
            return ids[0]
        result_id = str(result_id).strip()
        # Only ids this store produced ever reach the filesystem.
        return result_id if _ID.match(result_id) and result_id in ids else None

    @staticmethod
    def _open_nofollow(path):
        return os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                       | getattr(os, "O_CLOEXEC", 0))

    def _read(self, path):
        limit = (MAX_UPLOAD_BYTES if path.endswith(ZIP_SUFFIX) else MAX_UNPACKED_BYTES) + 1
        fd = self._open_nofollow(path)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise OSError(errno.EINVAL, "not a regular file")
            body = f.read(limit)
        if len(body) >= limit:
            raise ValueError("the stored file is larger than %d bytes" % (limit - 1))
        return body

    def load(self, result_id):
        """``(document, headers)`` of a stored upload; raises ``ValueError`` when the file cannot be read
        (``FileNotFoundError`` when it is gone, e.g. deleted by retention after it was listed)."""
        path = self._path(result_id)
        if path is None:
            raise FileNotFoundError(errno.ENOENT, "no stored file for %s" % result_id)
        try:
            body = self._read(path)
        except FileNotFoundError:
            raise
        except OSError as e:
            raise ValueError("cannot read %s: %s" % (os.path.basename(path), e.strerror or e))
        with self.parsing:
            document = parse_upload(body, path.endswith(ZIP_SUFFIX))
        return document, self.load_headers(result_id)

    def load_headers(self, result_id):
        """The saved ``X-BC-*`` headers (and problems) of an upload, without reading the document. A symlink or
        anything but a regular file reads as no headers."""
        try:
            fd = self._open_nofollow(os.path.join(self.folder, result_id + HEADERS_SUFFIX))
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    return {}
                headers = json.loads(f.read(1 << 20))
        except (OSError, ValueError, RecursionError):
            return {}
        return headers if isinstance(headers, dict) else {}

    def size(self, result_id):
        """Bytes of the stored results file; ``None`` when it is missing, a symlink or not a regular file."""
        path = self._path(result_id)
        try:
            st = None if path is None else os.lstat(path)
        except OSError:
            return None
        return st.st_size if st is not None and stat.S_ISREG(st.st_mode) else None

    def stored_as(self, result_id):
        path = self._path(result_id)
        return None if path is None else ("zip" if path.endswith(ZIP_SUFFIX) else "json")

    # ------------------------------------------------------------------ tool API

    def list(self, limit=20):
        """The newest ``limit`` uploads, read from the saved headers only (never the document)."""
        out = []
        for rid in self.ids()[:max(0, int(limit))]:
            headers = self.load_headers(rid)
            received = headers.get("received_utc")
            if not isinstance(received, str):
                try:
                    received = _utc_iso(id_time(rid))
                except (ValueError, OverflowError, OSError):
                    received = None
            fmt = headers.get("x-bc-format")
            fmt_int = _int_or_none(fmt)
            problems = headers.get("problems")
            stored_as = self.stored_as(rid)
            out.append({
                "id": rid,
                "received_utc": received,
                "bytes": self.size(rid),
                "compressed": stored_as == "zip",
                "stored_as": stored_as,
                "format": fmt_int if fmt_int is not None else fmt,
                "game_version": headers.get("x-bc-game-version"),
                "session_count": _int_or_none(headers.get("x-bc-session-count")),
                "problems": [str(p) for p in problems] if isinstance(problems, list) else [],
            })
        return out

    def load_summary(self, result_id="latest", include_decisions=False, max_decisions=50, include_steam_ids=False):
        """What get_race_result shows for one upload. Raises ``ResultsError`` (bad_argument for an unknown id,
        stored_file_unreadable, unsupported_format). Blocking (parses up to 256 MB): call it in a thread."""
        rid = self.resolve(result_id)
        if rid is None:
            shown = str(result_id)
            shown = shown if len(shown) <= 64 else shown[:64] + "..."
            raise ResultsError("bad_argument", "no stored upload %r; see list_race_results" % shown, id=shown)
        for attempt in range(3):
            try:
                document, headers = self.load(rid)
                break
            except FileNotFoundError:
                # deleted (BC_RESULTS_KEEP) between listing and reading
                again = self.resolve(result_id) if attempt < 2 else None
                if again is None:
                    raise ResultsError("bad_argument", "upload %s is no longer stored (BC_RESULTS_KEEP deletes the "
                                       "oldest); see list_race_results" % rid, id=rid)
                rid = again
            except ValueError as e:
                raise ResultsError("stored_file_unreadable", "upload %s cannot be read: %s" % (rid, e), id=rid)
        problems = headers.get("problems") if isinstance(headers.get("problems"), list) else []
        if not isinstance(document, dict):
            raise ResultsError("stored_file_unreadable", "upload %s is not a JSON object" % rid, id=rid)
        fmt = document.get("format")
        if not _is_supported_format(fmt):
            raise ResultsError("unsupported_format",
                               "upload %s has format %r; this server reads format %d only" % (rid, fmt,
                                                                                              SUPPORTED_FORMAT),
                               id=rid, format=fmt, problems=problems)
        document = _plain(document)
        max_decisions = max(0, int(max_decisions))
        out = {"ok": True, "id": rid}
        for key in _TOP_KEYS:
            out[key] = document.get(key)
        if include_steam_ids:
            out["host_steam_id"] = _steam_str(document.get("host_steam_id"))
        out["sessions"] = []
        sessions = document.get("sessions")
        for s_ in sessions if isinstance(sessions, list) else []:
            if not isinstance(s_, dict):
                continue
            rows = []
            classification = s_.get("classification")
            for c in classification if isinstance(classification, list) else []:
                if not isinstance(c, dict):
                    continue
                row = dict(c)
                if include_steam_ids:
                    if "steam_id" in row:
                        row["steam_id"] = _steam_str(row["steam_id"])
                else:
                    row.pop("steam_id", None)
                rows.append(row)
            raw_decisions = s_.get("decisions")
            decisions = [d for d in (raw_decisions if isinstance(raw_decisions, list) else []) if isinstance(d, dict)]
            counts = {}
            for d in decisions:
                key = "%s/%s" % (d.get("decision_type"), d.get("chosen_by"))
                counts[key] = counts.get(key, 0) + 1
            header = s_.get("header")
            entry = {"kind": s_.get("kind"), "track": s_.get("track"), "started_at": s_.get("started_at"),
                     "ended_at": s_.get("ended_at"), "race_id": s_.get("race_id"),
                     "header": header if isinstance(header, dict) else None,
                     "classification": rows, "decisions_count": len(decisions), "decisions_by_type": counts}
            if include_decisions:
                entry["decisions"] = [{k: v for k, v in d.items() if k not in _DECISION_DROPPED}
                                      for d in decisions[:max_decisions]]
                if len(decisions) > max_decisions:
                    entry["decisions_truncated"] = True
            out["sessions"].append(entry)
        if problems:
            out["problems"] = [str(p) for p in problems]
        return out


class _DeadlineReader(io.RawIOBase):
    """Socket reads that time out after REQUEST_TIMEOUT_S of silence and, when ``deadline`` (time.monotonic) is
    set, also once it has passed: a client that trickles a byte now and then cannot hold a connection longer."""

    def __init__(self, sock):
        super().__init__()
        self._sock = sock
        self.deadline = None

    def readable(self):
        return True

    def readinto(self, buffer):
        wait = REQUEST_TIMEOUT_S
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("deadline passed")
            wait = min(wait, left)
        self._sock.settimeout(wait)
        return self._sock.recv_into(buffer)


def _handler(store, token, on_upload):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "backseat-champions-mcp"
        timeout = REQUEST_TIMEOUT_S        # also the write timeout of answers

        def setup(self):
            super().setup()
            plain = self.rfile
            self._reader = _DeadlineReader(self.connection)
            # request line + headers: HEADER_DEADLINE_S from now (a timeout there closes without an answer)
            self._reader.deadline = time.monotonic() + HEADER_DEADLINE_S
            self.rfile = io.BufferedReader(self._reader, 1 << 16)
            plain.close()

        def _refuse(self, code, why):
            log.warning("refused a race results upload: %s", why)
            self._answer(code)

        def _answer(self, code):
            try:
                self.connection.settimeout(REQUEST_TIMEOUT_S)     # the read deadline may have left it near 0
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
            except OSError:
                pass
            self.close_connection = True

        def do_POST(self):
            try:
                self._post()
            except (socket.timeout, TimeoutError):
                self._refuse(408, "the body stalled for %d s or did not arrive within %.0f s"
                             % (REQUEST_TIMEOUT_S, self._body_deadline_s))
            except Exception as e:          # never close a connection without an answer
                log.exception("race results receiver: unexpected error")
                self._refuse(500, "%s: %s" % (type(e).__name__, e))

        _body_deadline_s = 0.0

        def _read_body(self, length):
            self._body_deadline_s = body_deadline(length)
            self._reader.deadline = time.monotonic() + self._body_deadline_s
            parts = []
            got = 0
            while got < length:
                chunk = self.rfile.read1(min(1 << 16, length - got)) if hasattr(self.rfile, "read1") \
                    else self.rfile.read(min(1 << 16, length - got))
                if not chunk:
                    break
                parts.append(chunk)
                got += len(chunk)
            return b"".join(parts)

        def _post(self):
            if self.headers.get("Origin") is not None or self.headers.get("X-BC-Format") is None:
                return self._refuse(403, "not sent by the game (Origin header present or X-BC-Format missing)")
            if token and not hmac.compare_digest(self.headers.get("Authorization", "").encode("utf-8"),
                                                 ("Bearer " + token).encode("utf-8")):
                return self._refuse(401, "wrong or missing bearer token")
            if self.headers.get("Transfer-Encoding") is not None:
                return self._refuse(411, "Transfer-Encoding is not supported; send Content-Length")
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1 or not _DIGITS.match(lengths[0].strip()):
                return self._refuse(411, "no single valid Content-Length")
            length = int(lengths[0].strip())
            if length > MAX_UPLOAD_BYTES:
                return self._refuse(413, "%d bytes is over the %d byte limit" % (length, MAX_UPLOAD_BYTES))
            body = self._read_body(length)
            if len(body) != length:
                return self._refuse(400, "body ended after %d of %d bytes" % (len(body), length))
            zipped = self.headers.get("Content-Type", "").split(";")[0].strip().lower() == "application/zip"
            with store.parsing:                  # bounded: unpacking / parsing can take hundreds of MB
                try:
                    document = parse_upload(body, zipped)
                except ValueError as e:
                    return self._refuse(422, "unreadable upload: %s" % e)
                problems = check_document(document, self.headers.get("X-BC-Format"))
                del document
            if problems and problems[0] == "the document is not a JSON object":
                return self._refuse(422, problems[0])
            meta = {k.lower(): v for k, v in self.headers.items() if k.lower().startswith("x-bc-")}
            try:
                stamp = store.save(body, meta, problems, zipped=zipped)
            except OSError as e:
                return self._refuse(500, "cannot save the upload: %s" % e)
            log.info("race results saved as %s (%d bytes%s%s)", stamp, len(body), ", zipped" if zipped else "",
                     "; problems: %s" % "; ".join(problems) if problems else "")
            if on_upload is not None:           # before answering: a status read right after the 200 counts it
                try:
                    on_upload(stamp)
                except Exception:
                    log.exception("race results: upload callback failed")
            self._answer(200)

        def _not_post(self):
            self._refuse(405, "only POST is served")

        do_GET = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = do_PATCH = _not_post

        def log_message(self, *args):      # never write the default access log (it would go to stderr unformatted)
            pass

    return Handler


class _Server(http.server.ThreadingHTTPServer):
    """At most MAX_CONNECTIONS connections are served at once. When all are taken, a new connection waits up to
    SLOT_WAIT_S for one (a burst of real uploads is queued, not dropped: the game never retries); if none frees up,
    it is closed and so is every further one until a slot frees (stalled connections cannot pile up threads, and the
    listen backlog keeps draining)."""
    request_queue_size = LISTEN_BACKLOG
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._closing = threading.Event()
        self._saturated = False
        super().__init__(*args, **kwargs)

    def _take_slot(self):
        if self._slots.acquire(blocking=False):
            return True
        if self._saturated:
            return False
        deadline = time.monotonic() + SLOT_WAIT_S
        while not self._closing.is_set() and time.monotonic() < deadline:
            if self._slots.acquire(timeout=0.1):
                return True
        self._saturated = True
        log.warning("race results receiver: all %d connections busy for %.0f s; closing new ones until one frees",
                    MAX_CONNECTIONS, SLOT_WAIT_S)
        return False

    def process_request(self, request, client_address):
        if not self._take_slot():
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release()
            raise

    def _release(self):
        self._saturated = False
        self._slots.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release()

    def shutdown(self):
        self._closing.set()
        super().shutdown()


class _Server6(_Server):
    address_family = socket.AF_INET6


class ResultsReceiver:
    """Starts and stops the HTTP receiver; ``error`` says why it is not running."""

    def __init__(self, host, port, token, folder, keep=DEFAULT_KEEP):
        self.host = host
        self.port = port
        self.token = token or ""
        self.store = ResultsStore(folder, keep)
        self.error = None
        self.received = 0
        self._server = None
        self._thread = None

    def _count(self, stamp):
        self.received += 1

    def start(self):
        if self._server is not None:
            return
        try:
            self.store.makedirs()
            self.store.prune()
            family_server = _Server6 if ":" in self.host else _Server
            self._server = family_server((self.host, self.port), _handler(self.store, self.token, self._count))
            self.port = self._server.server_address[1]
            self._thread = threading.Thread(target=self._server.serve_forever, name="bc-results", daemon=True)
            self._thread.start()
            self.error = None
            log.info("receiving race results on http://%s:%d/ into %s", self.host, self.port, self.store.folder)
        except OSError as e:
            self._server = None
            self.error = str(e)
            log.warning("cannot receive race results on %s:%s: %s", self.host, self.port, e)

    def stop(self):
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    @property
    def url(self):
        host = "[%s]" % self.host if ":" in self.host else self.host
        return "http://%s:%d/" % (host, self.port)

    def status(self):
        out = {"enabled": True, "listening": self._server is not None, "endpoint": self.url,
               "token_required": bool(self.token), "folder": self.store.folder,
               "uploads_received_this_run": self.received, "uploads_stored": len(self.store.ids()),
               "keep": self.store.keep}
        if self.error:
            out["error"] = self.error
            out["hint"] = "The port may be in use. Set BC_RESULTS_PORT to a free port and restart this server."
        return out
