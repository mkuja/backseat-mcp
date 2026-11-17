"""A minimal raw JSON-RPC (MCP over stdio) client for end-to-end tests.

Unlike mcp's ClientSession it can send a tools/call and never look at the answer, without sending
notifications/cancelled: this is what a client whose own timeout cuts the call (the Claude desktop bridge at ~80 s)
does, as far as the server can tell. mcp's read_timeout_seconds sends a cancel, so it cannot simulate that.
"""
import asyncio
import json
import os
import sys
import time

PROTOCOL_VERSION = "2025-06-18"


class RpcError(Exception):
    pass


class RpcServer:
    def __init__(self, env, cwd, errlog_path):
        self.env = env
        self.cwd = cwd
        self.errlog_path = errlog_path
        self.proc = None
        self.next_id = 0
        self.pending = {}            # id -> future (calls whose answer we want)
        self.answers = {}            # id -> (monotonic time, raw message) for every answer, wanted or not
        self.notifications = []
        self._reader = None
        self._errlog = None

    async def start(self):
        env = dict(os.environ)
        env.update(self.env)
        self._errlog = open(self.errlog_path, "ab")
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "backseat_champions_mcp", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=self._errlog, env=env, cwd=self.cwd, limit=16 * 1024 * 1024)
        self._reader = asyncio.ensure_future(self._read())
        init = await self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                                 "clientInfo": {"name": "e2e-raw", "version": "0"}}, timeout=20)
        await self.notify("notifications/initialized")
        self.server_info = init
        return init

    async def _read(self):
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                self.answers[msg["id"]] = (time.monotonic(), msg)
                fut = self.pending.pop(msg["id"], None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
            elif "method" in msg and "id" in msg:
                # a server -> client request (ping, roots...): answer with an empty result
                await self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
            else:
                self.notifications.append(msg)
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(RpcError("server closed stdout"))

    async def _send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def send_request(self, method, params, want=True):
        self.next_id += 1
        rid = self.next_id
        if want:
            self.pending[rid] = asyncio.get_running_loop().create_future()
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid

    async def request(self, method, params, timeout=30):
        rid = await self.send_request(method, params)
        fut = self.pending[rid]
        try:
            msg = await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            # deliberately no notifications/cancelled
            raise RpcError("no answer to %s within %.1f s" % (method, timeout))
        if "error" in msg:
            raise RpcError("JSON-RPC error: %r" % (msg["error"],))
        return msg["result"]

    async def call(self, name, args=None, timeout=90):
        """tools/call -> the tool's JSON result (dict) with _is_error and _elapsed."""
        t0 = time.monotonic()
        result = await self.request("tools/call", {"name": name, "arguments": args or {}}, timeout=timeout)
        return parse_tool_result(result, time.monotonic() - t0)

    async def abandon(self, name, args=None):
        """Send tools/call and never read its answer, never cancel it. Returns the request id."""
        return await self.send_request("tools/call", {"name": name, "arguments": args or {}}, want=False)

    def answer_of(self, rid):
        got = self.answers.get(rid)
        if got is None:
            return None
        return parse_tool_result(got[1].get("result") or {}, None)

    async def kill(self):
        if self.proc is None:
            return
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except BaseException:
                pass
        if self._errlog is not None:
            self._errlog.close()
            self._errlog = None

    async def close(self):
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 3)
            except (asyncio.TimeoutError, OSError, BrokenPipeError, ConnectionResetError):
                pass
        await self.kill()

    def stderr_text(self):
        try:
            with open(self.errlog_path, "r", errors="replace") as f:
                return f.read()
        except OSError:
            return ""


def parse_tool_result(result, elapsed):
    content = result.get("content") or [{}]
    text = content[0].get("text", "")
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            data = {"_value": data}
    except ValueError:
        data = {"_text": text}
    data["_is_error"] = bool(result.get("isError"))
    data["_elapsed"] = elapsed
    return data
