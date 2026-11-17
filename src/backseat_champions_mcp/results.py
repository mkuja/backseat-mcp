"""Optional HTTP receiver for the race results the game uploads when you host.

The game sends one ``POST`` per finished race weekend to every endpoint listed under Race Results in its Telemetry
tab: a JSON document (or a ZIP holding it as ``results.json``) with ``X-BC-*`` headers and, when the endpoint has a
token, ``Authorization: Bearer <token>``. Nothing is retried, so this receiver saves every upload it can read and
answers 2xx for it.

The same guards as the game's own ``gameapi_client.py results-server``: a request with an ``Origin`` header or
without ``X-BC-Format`` is refused (a web page in a browser cannot forge that), bodies over 16 MB and ZIPs that
unpack to over 256 MB are refused, and it only listens off loopback when a token is set.

Runs in a daemon thread next to the asyncio server; it shares nothing with it but the folder on disk.
"""
import datetime
import hmac
import http.server
import io
import json
import logging
import os
import re
import threading
import zipfile

log = logging.getLogger("backseat_champions_mcp.results")

MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
RESULT_SUFFIX = ".results.json"
HEADERS_SUFFIX = ".headers.json"
_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9]{6}$")
_REQUIRED = ("format", "game_version", "sessions")


def unzip_results(body):
    """The ``results.json`` entry of a "Send as ZIP" upload, counted as it inflates."""
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = archive.namelist()
        if names != ["results.json"]:
            raise ValueError("expected a ZIP holding only results.json, got %r" % names[:5])
        out = bytearray()
        with archive.open("results.json") as entry:
            while True:
                chunk = entry.read(1 << 20)
                if not chunk:
                    break
                out += chunk
                if len(out) > MAX_UNPACKED_BYTES:
                    raise ValueError("the ZIP unpacks to more than %d bytes" % MAX_UNPACKED_BYTES)
    return bytes(out)


def check_document(document):
    """Problems that make a results document unusable for the tools (not a full schema validation)."""
    if not isinstance(document, dict):
        return ["the document is not a JSON object"]
    problems = ["missing %s" % key for key in _REQUIRED if key not in document]
    sessions = document.get("sessions")
    if sessions is not None and not isinstance(sessions, list):
        problems.append("sessions is not an array")
    for i, session in enumerate(sessions if isinstance(sessions, list) else []):
        if not isinstance(session, dict) or not isinstance(session.get("classification"), list):
            problems.append("sessions[%d] has no classification array" % i)
    return problems


class ResultsStore:
    """The uploads saved in one folder: ``<stamp>.results.json`` and ``<stamp>.headers.json``."""

    def __init__(self, folder):
        self.folder = folder

    def save(self, document_bytes, headers, problems):
        os.makedirs(self.folder, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        with open(os.path.join(self.folder, stamp + RESULT_SUFFIX), "wb") as f:
            f.write(document_bytes)
        meta = dict(headers)
        if problems:
            meta["problems"] = problems
        with open(os.path.join(self.folder, stamp + HEADERS_SUFFIX), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1)
        return stamp

    def ids(self):
        """Upload ids, newest first."""
        try:
            names = os.listdir(self.folder)
        except OSError:
            return []
        found = [n[:-len(RESULT_SUFFIX)] for n in names if n.endswith(RESULT_SUFFIX)]
        return sorted((i for i in found if _ID.match(i)), reverse=True)

    def resolve(self, result_id):
        """A stored id for ``"latest"`` or an id; ``None`` when there is no such upload."""
        ids = self.ids()
        if not ids:
            return None
        if result_id in (None, "", "latest"):
            return ids[0]
        result_id = str(result_id).strip()
        # Only ids this store produced ever reach the filesystem.
        return result_id if _ID.match(result_id) and result_id in ids else None

    def load(self, result_id):
        """``(document, headers)`` of a stored upload."""
        with open(os.path.join(self.folder, result_id + RESULT_SUFFIX), encoding="utf-8") as f:
            document = json.load(f)
        return document, self.load_headers(result_id)

    def load_headers(self, result_id):
        """The saved ``X-BC-*`` headers (and problems) of an upload, without reading the document."""
        try:
            with open(os.path.join(self.folder, result_id + HEADERS_SUFFIX), encoding="utf-8") as f:
                headers = json.load(f)
        except (OSError, ValueError):
            return {}
        return headers if isinstance(headers, dict) else {}

    def size(self, result_id):
        try:
            return os.path.getsize(os.path.join(self.folder, result_id + RESULT_SUFFIX))
        except OSError:
            return None


def _handler(store, token, on_upload):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "backseat-champions-mcp"

        def _refuse(self, code, why):
            log.warning("refused a race results upload: %s", why)
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            if self.headers.get("Origin") is not None or self.headers.get("X-BC-Format") is None:
                return self._refuse(403, "not sent by the game (Origin header present or X-BC-Format missing)")
            if token and not hmac.compare_digest(self.headers.get("Authorization", "").encode("utf-8"),
                                                 ("Bearer " + token).encode("utf-8")):
                return self._refuse(401, "wrong or missing bearer token")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._refuse(411, "no Content-Length")
            if length < 0 or length > MAX_UPLOAD_BYTES:
                return self._refuse(413, "%d bytes is over the %d byte limit" % (length, MAX_UPLOAD_BYTES))
            body = self.rfile.read(length)
            zipped = self.headers.get("Content-Type", "").split(";")[0].strip().lower() == "application/zip"
            try:
                raw = unzip_results(body) if zipped else body
                document = json.loads(raw.decode("utf-8"))
            except (zipfile.BadZipFile, ValueError, UnicodeDecodeError) as e:
                return self._refuse(422, "unreadable upload: %s" % e)
            problems = check_document(document)
            if problems and not isinstance(document, dict):
                return self._refuse(422, problems[0])
            meta = {k.lower(): v for k, v in self.headers.items() if k.lower().startswith("x-bc-")}
            try:
                stamp = store.save(raw, meta, problems)
            except OSError as e:
                return self._refuse(500, "cannot save the upload: %s" % e)
            log.info("race results saved as %s (%d bytes%s)", stamp, len(body), ", zipped" if zipped else "")
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            if on_upload is not None:
                on_upload(stamp)

        def do_GET(self):
            self._refuse(405, "only POST is served")

        def log_message(self, *args):      # never write the default access log (it would go to stderr unformatted)
            pass

    return Handler


class ResultsReceiver:
    """Starts and stops the HTTP receiver; ``error`` says why it is not running."""

    def __init__(self, host, port, token, folder):
        self.host = host
        self.port = port
        self.token = token or ""
        self.store = ResultsStore(folder)
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
            os.makedirs(self.store.folder, exist_ok=True)
            family_server = http.server.ThreadingHTTPServer
            if ":" in self.host:
                import socket

                class family_server(http.server.ThreadingHTTPServer):  # noqa: N801
                    address_family = socket.AF_INET6
            self._server = family_server((self.host, self.port), _handler(self.store, self.token, self._count))
            self._server.daemon_threads = True
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
               "uploads_received_this_run": self.received, "uploads_stored": len(self.store.ids())}
        if self.error:
            out["error"] = self.error
            out["hint"] = "The port may be in use. Set BC_RESULTS_PORT to a free port and restart this server."
        return out
