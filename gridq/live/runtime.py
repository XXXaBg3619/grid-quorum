"""The live runtime: wall-clock kernel, UDP transport, status endpoint, console log and main loop.

  LiveKernel    kernel.Kernel's interface on the wall clock: now is seconds
                since t0, which every node of a demo shares (GRIDQ_T0), so all
                agree on dispatch slots. Events due in the past run at once.
  UdpTransport  one JSON object per datagram to a peer named like its
                container (Docker's DNS; a background thread resolves names,
                so a stopped peer never blocks the loop) or to a fixed address
                (tests). Loss is the protocol's business, as in the simulator.
  LiveLog       EventLog's append(): prints the events worth watching, keeps
                the newest ones for the status endpoint.
  start_http    GET /status (and /chain on validators) and POST /control on a
                daemon thread; control requests are queued for the main loop.
  serve         the main loop: messages, due events, control requests and a
                fresh status snapshot four times a second, all on one thread.
"""
from __future__ import annotations

import heapq
import itertools
import json
import math
import os
import queue
import selectors
import socket
import sys
import threading
import time
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..config import GridConfig

PORT = 7000                 # UDP: protocol messages
STATUS_PORT = 8000          # HTTP: status and control
MAX_DATAGRAM = 60000        # bytes; larger messages are not sent (catch-up and sync replies are cut to fit)
_PRINT = threading.Lock()   # one console line at a time (cluster.py runs every node in one process)


def live_config(period: float = 8.0, n_validators: int = 7, **kw) -> GridConfig:
    """The demo's parameters: a dispatch slot every `period` seconds, timeouts scaled to it.

    Round 0 waits period / 4 for a proposal and period / 8 per vote step, so a
    failed round costs half a slot; commands live three slots; a validator
    insists on reports it has held for half a slot."""
    base = dict(arch="D", n_validators=n_validators, dispatch_period_s=period,
                round_timeout_s=period / 4.0, vote_timeout_s=period / 8.0, timeout_delta_s=period / 16.0,
                command_ttl_s=3.0 * period, report_inclusion_s=period / 2.0, horizon_h=24.0 * 365)
    base.update(kw)
    return GridConfig(**base)


def env_config() -> GridConfig:
    return live_config(float(os.environ.get("GRIDQ_PERIOD", "8")), int(os.environ.get("GRIDQ_VALIDATORS", "7")))


def env_t0() -> float:
    """The demo's shared start time (Unix seconds); 0 means the Unix epoch, which also works."""
    return float(os.environ.get("GRIDQ_T0") or 0)


# ---------------------------------------------------------------- time

class LiveKernel:
    def __init__(self, t0: float):
        self.t0 = t0
        self.horizon_s = math.inf
        self.processed = 0
        self._q: list = []
        self._ctr = itertools.count()

    @property
    def now(self) -> float:
        return time.time() - self.t0

    def schedule(self, t: float, prio: int, fn: Callable, *args) -> None:
        heapq.heappush(self._q, (t, prio, next(self._ctr), fn, args))

    def after(self, delay: float, prio: int, fn: Callable, *args) -> None:
        self.schedule(self.now + delay, prio, fn, *args)

    def pending(self) -> int:
        return len(self._q)

    def next_due(self) -> Optional[float]:
        return self._q[0][0] if self._q else None

    def run_due(self) -> int:
        """Run every event that is due; returns how many ran."""
        n = 0
        while self._q and self._q[0][0] <= self.now:
            _, _, _, fn, args = heapq.heappop(self._q)
            fn(*args)
            n += 1
        self.processed += n
        return n


# ---------------------------------------------------------------- messages

class UdpTransport:
    def __init__(self, port: int = PORT, *, peers: Iterable[str] = (),
                 addresses: Optional[Mapping[str, Tuple[str, int]]] = None,
                 host: str = "0.0.0.0", resolve_every_s: float = 1.0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.addresses: Dict[str, Tuple[str, int]] = dict(addresses or {})
        self._dns: Dict[str, Tuple[str, int]] = {}
        self._names = sorted(set(peers) - set(self.addresses))
        self.stats: Counter = Counter()
        self._closed = threading.Event()
        if self._names:
            threading.Thread(target=self._resolve, args=(resolve_every_s,), daemon=True).start()

    def _resolve(self, every: float) -> None:
        while not self._closed.is_set():
            for name in self._names:
                try:
                    self._dns[name] = (socket.gethostbyname(name.lower()), self.port)
                except OSError:
                    self._dns.pop(name, None)       # stopped or cut off: Docker's DNS forgets it
            self._closed.wait(every)

    def address(self, name: str) -> Optional[Tuple[str, int]]:
        return self.addresses.get(name) or self._dns.get(name)

    def send(self, dst: str, msg: dict) -> bool:
        data = json.dumps(msg, separators=(",", ":")).encode()
        if len(data) > MAX_DATAGRAM:
            self.stats["too_big"] += 1
            return False
        addr = self.address(dst)
        if addr is None:
            self.stats["unresolved"] += 1
            return False
        try:
            self.sock.sendto(data, addr)
        except OSError:                             # no route while the container is cut off
            self.stats["send_error"] += 1
            return False
        self.stats["sent"] += 1
        return True

    def receive(self) -> List[dict]:
        out = []
        while True:
            try:
                data, _ = self.sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self.stats["recv_error"] += 1
                break
            try:
                msg = json.loads(data)
            except ValueError:
                self.stats["bad_json"] += 1
                continue
            if isinstance(msg, dict):
                out.append(msg)
        self.stats["received"] += len(out)
        return out

    def close(self) -> None:
        self._closed.set()
        self.sock.close()


# ---------------------------------------------------------------- console log

def _short(x, n: int = 8) -> str:
    return "nil" if x is None else str(x)[:n]


def describe(kind: str, d: Mapping) -> Optional[str]:
    """One console line for an event worth watching, else None."""
    if kind == "commit":
        return (f"commit  h={d['height']} r={d['round']} block {_short(d['block'])}  "
                f"{len(d.get('sigs') or {})} signatures ({d.get('via')})")
    if kind == "evidence":
        return f"EVIDENCE {d['offender']} signed two different {d['kind']}s at h={d['height']} r={d['round']}"
    if kind == "round_timeout" and d.get("step") == "precommit":
        return f"round h={d['height']} r={d['round']} failed, starting round {d['round'] + 1}"
    if kind == "fault_action" and isinstance(d.get("sent"), dict) and len(d["sent"]) > 1:
        parts = "; ".join(f"{_short(b)} to {','.join(to)}" for b, to in d["sent"].items())
        return f"FAULT   {d.get('action')} h={d.get('height')} r={d.get('round')}: {parts}"
    if kind == "command_accepted":
        return (f"execute {d['feeder']} -> {d['target']}  (seq {d['seq']}, block {d['height']}, "
                f"from {d.get('from') or '?'})")
    if kind == "decision_applied":
        return f"block {d.get('height')} verified and applied (slot {d.get('slot')})"
    return None


class LiveLog:
    def __init__(self, name: str, t0: float, keep: int = 60, echo: bool = True):
        self.name, self.t0, self.echo = name, t0, echo
        self.recent: deque = deque(maxlen=keep)
        self.evidence: List[dict] = []
        self.counts: Counter = Counter()

    def append(self, t: float, kind, actor: str, data: dict, visibility: str = "public"):
        k = getattr(kind, "value", str(kind))
        self.counts[k] += 1
        if k == "evidence":
            self.evidence.append({"t": round(t, 3), **{x: data.get(x) for x in ("offender", "kind", "height", "round")}})
        line = describe(k, data)
        if line is not None:
            self.note(t, k, line)

    def note(self, t: float, kind: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.t0 + t))
        self.recent.append({"t": round(t, 3), "time": stamp, "node": self.name, "kind": kind, "text": text})
        if self.echo:
            with _PRINT:
                sys.stdout.write(f"{stamp} {self.name:>3}  {text}\n")
                sys.stdout.flush()


# ---------------------------------------------------------------- status endpoint

def start_http(port: int, get: Callable[[str, dict], Tuple[int, str, bytes]],
               post: Optional[Callable[[str, dict], Tuple[int, str, bytes]]] = None,
               host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """get(path, query) and post(path, json body) return (status, content type, body)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            try:
                self._reply(*get(url.path, {k: v[-1] for k, v in parse_qs(url.query).items()}))
            except Exception as exc:                # a bad query must not kill the endpoint
                self._reply(400, "application/json", json.dumps({"error": repr(exc)}).encode())

        def do_POST(self):
            if post is None:
                return self._reply(405, "application/json", b'{"error": "read only"}')
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
                self._reply(*post(urlparse(self.path).path, data))
            except Exception as exc:
                self._reply(400, "application/json", json.dumps({"error": repr(exc)}).encode())

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def json_reply(obj, code: int = 200) -> Tuple[int, str, bytes]:
    return code, "application/json", json.dumps(obj).encode()


class LiveNode:
    """What serve() needs from a node; ValidatorNode and SubstationNode fill it in."""

    def __init__(self, kernel: LiveKernel, transport: UdpTransport, log: LiveLog):
        self.kernel, self.transport, self.log = kernel, transport, log
        self.controls: "queue.SimpleQueue[dict]" = queue.SimpleQueue()
        self.snapshot: dict = {}

    def start(self) -> None:
        raise NotImplementedError

    def on_message(self, msg: dict) -> None:
        raise NotImplementedError

    def status(self) -> dict:
        raise NotImplementedError

    def control(self, req: dict) -> None:
        self.log.note(self.kernel.now, "control", f"ignored control request {req!r}")

    # HTTP thread: read the snapshot, queue control requests
    def http_get(self, path: str, query: dict):
        if path == "/status":
            return json_reply(self.snapshot)
        return json_reply({"error": "not found"}, 404)

    def http_post(self, path: str, data: dict):
        if path == "/control" and isinstance(data, dict):
            self.controls.put(data)
            return json_reply({"queued": data}, 202)
        return json_reply({"error": "not found"}, 404)


def serve(node: LiveNode, *, status_port: Optional[int] = None, stop: Optional[threading.Event] = None,
          snapshot_every_s: float = 0.25) -> None:
    """Run the node until `stop` is set (or for ever)."""
    kernel, transport = node.kernel, node.transport
    sel = selectors.DefaultSelector()
    sel.register(transport.sock, selectors.EVENT_READ)
    httpd = start_http(status_port, node.http_get, node.http_post) if status_port is not None else None
    node.start()
    next_snap = -math.inf
    try:
        while stop is None or not stop.is_set():
            due = kernel.next_due()
            wait = 0.1 if due is None else min(max(due - kernel.now, 0.0), 0.1)
            if wait > 0.0:
                sel.select(wait)
            for msg in transport.receive():
                try:
                    node.on_message(msg)
                except Exception as exc:            # messages are untrusted input
                    transport.stats["bad_message"] += 1
                    node.log.note(kernel.now, "error", f"dropped a bad {msg.get('type')!r} message: {exc!r}")
            kernel.run_due()
            while True:
                try:
                    req = node.controls.get_nowait()
                except queue.Empty:
                    break
                node.control(req)
            if kernel.now >= next_snap:
                node.snapshot = node.status()
                next_snap = kernel.now + snapshot_every_s
    finally:
        sel.close()
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()


def run_forever(node: LiveNode, status_port: Optional[int]) -> None:
    """Container entry: serve until SIGTERM (docker stop) or Ctrl-C."""
    import signal
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        serve(node, status_port=status_port, stop=stop)
    except KeyboardInterrupt:
        pass
    finally:
        node.transport.close()
