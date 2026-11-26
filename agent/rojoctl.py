"""rojoctl: connect and disconnect Studio's Rojo plugin from the command line, and keep every Rojo project on
its own port so several agents can work side by side.

Works with the patched plugin built from this checkout (agent/apply_patch.py). A small control service on
127.0.0.1:34860 sits between this command and the plugin: the plugin polls it once a second, reports its
state, and is told to connect or disconnect. The service starts itself the first time a command needs it.

    python rojoctl.py status                  what each open Studio reports, and any pending order
    python rojoctl.py ports                   the port registry, and what is really serving on each port
    python rojoctl.py claim --project NAME [--port N] [--repo PATH] [--places ID,ID]
                                              give a project its own port (picks a free one if --port is left out)
    python rojoctl.py connect [--place ID] [--port N] [--wait 20]
    python rojoctl.py disconnect [--place ID] [--wait 10]
    python rojoctl.py release [--place ID]    stop steering a place; leave it as it is
    python rojoctl.py stop                    stop the control service

Ports: the registry is rojo-ports.json in the tooling folder (or ROJO_PORTS). One port per project, never
shared. Start each project's server on its registered port: rojo serve <project file> --port <port>.

Connect safety:
- --port can be left out: rojoctl finds the live server whose project allows this place (servePlaceIds), or
  the registry entry that lists the place.
- A connect is refused if the server on that port is not the project the registry names for that port, or
  if that project's servePlaceIds do not include the place.
- The plugin accepts the first sync without a click only when it deletes nothing in Studio. If it would
  delete instances, Rojo's normal confirmation dialog opens for a person, and connect exits with code 2.
  "disconnect" then closes that dialog without applying anything.

"connect" and "disconnect" are each one order: the service forgets it once Studio reports it done (or after
two minutes unmet), so it never reconnects a Studio someone disconnected by hand. Exit code 0 means done.
"""
import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 34860
BASE = f"http://127.0.0.1:{PORT}"
STALE_SECONDS = 8  # a Studio that has not polled for this long is treated as closed
WISH_SECONDS = 120  # an order nobody carried out in this long is forgotten
FIRST_PROJECT_PORT = 34870
REGISTRY = Path(os.environ.get("ROJO_PORTS") or Path(__file__).resolve().parents[2] / "rojo-ports.json")

lock = threading.Lock()
studios = {}  # placeId -> {place, status, host, port, project, removals, seen}
wanted = {}   # placeId -> {"connect": bool, "host": str, "port": str, "asked": float}


# --- Rojo server info (msgpack since Rojo 7.7; only the types /api/rojo uses) -------------------------

def _length(data, i, size):
    return int.from_bytes(data[i:i + size], "big"), i + size


def _unpack(data, i):
    b = data[i]
    i += 1
    if b <= 0x7F:
        return b, i
    if b >= 0xE0:
        return b - 0x100, i
    if 0x80 <= b <= 0x8F or b in (0xDE, 0xDF):
        n, i = (b & 0x0F, i) if b <= 0x8F else _length(data, i, 2 if b == 0xDE else 4)
        out = {}
        for _ in range(n):
            k, i = _unpack(data, i)
            out[k], i = _unpack(data, i)
        return out, i
    if 0x90 <= b <= 0x9F or b in (0xDC, 0xDD):
        n, i = (b & 0x0F, i) if b <= 0x9F else _length(data, i, 2 if b == 0xDC else 4)
        out = []
        for _ in range(n):
            v, i = _unpack(data, i)
            out.append(v)
        return out, i
    if 0xA0 <= b <= 0xBF or b in (0xD9, 0xDA, 0xDB):
        n, i = (b & 0x1F, i) if b <= 0xBF else _length(data, i, {0xD9: 1, 0xDA: 2, 0xDB: 4}[b])
        return data[i:i + n].decode("utf-8", "replace"), i + n
    if b == 0xC0:
        return None, i
    if b in (0xC2, 0xC3):
        return b == 0xC3, i
    if b in (0xCC, 0xCD, 0xCE, 0xCF):
        size = {0xCC: 1, 0xCD: 2, 0xCE: 4, 0xCF: 8}[b]
        return int.from_bytes(data[i:i + size], "big"), i + size
    if b in (0xD0, 0xD1, 0xD2, 0xD3):
        size = {0xD0: 1, 0xD1: 2, 0xD2: 4, 0xD3: 8}[b]
        return int.from_bytes(data[i:i + size], "big", signed=True), i + size
    if b in (0xCA, 0xCB):
        size = 4 if b == 0xCA else 8
        return struct.unpack(">f" if size == 4 else ">d", data[i:i + size])[0], i + size
    raise ValueError(f"unsupported msgpack byte {b:#x}")


def server_info(host, port):
    """The Rojo server's info on host:port: a dict ({} if something other than Rojo answers), or None if
    nothing answers."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/rojo", timeout=0.8) as response:
            body = response.read()
    except urllib.error.HTTPError:
        return {}
    except OSError:
        return None
    try:
        return json.loads(body)
    except ValueError:
        pass
    try:
        info, _ = _unpack(body, 0)
        return info if isinstance(info, dict) else {}
    except (ValueError, IndexError, KeyError):
        return {}


def server_up(host, port):
    return server_info(host, port) is not None


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


# --- Port registry ------------------------------------------------------------------------------------

def load_registry():
    if REGISTRY.exists():
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    return {"note": "One Rojo port per project. Edit with rojoctl claim.", "ports": {}}


def save_registry(registry):
    REGISTRY.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")


# --- Control service ----------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, body, code=200):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = {key: values[0] for key, values in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        if url.path == "/poll":
            place = query.get("placeId", "0")
            status = query.get("status", "")
            with lock:
                studios[place] = {"place": query.get("place", ""), "status": status, "host": query.get("host", ""),
                                  "port": query.get("port", ""), "project": query.get("project", ""),
                                  "removals": query.get("removals", ""), "seen": time.time()}
                wish = wanted.get(place)
                # A wish is one order, not a state to hold (owner, 2026-11-25): once Studio reports it done,
                # or it has gone unmet for WISH_SECONDS, it is dropped, so a connection someone then changes
                # by hand stays as they left it. A connect now waiting on a person's confirmation is done too.
                if wish:
                    # An aborted confirmation leaves the plugin in Error: not connected, nothing applied.
                    reached = ("Connected", "Confirming") if wish["connect"] else ("NotConnected", "Error")
                    if status in reached or time.time() - wish["asked"] > WISH_SECONDS:
                        wanted.pop(place, None)
                        wish = None
            order = {}
            if wish and wish["connect"]:
                # Only order a connect when a server is really there, so a stopped server is not hammered.
                if status in ("NotConnected", "Error") and server_up(wish["host"], wish["port"]):
                    order = {"action": "connect", "host": wish["host"], "port": wish["port"]}
            elif wish and not wish["connect"]:
                # Disconnecting while the confirmation dialog is open aborts that connect: nothing is applied.
                if status in ("Connected", "Connecting", "Confirming"):
                    order = {"action": "disconnect"}
            self.reply(order)
        elif url.path == "/state":
            now = time.time()
            with lock:
                live = {place: dict(info, age=round(now - info["seen"], 1)) for place, info in studios.items() if now - info["seen"] < STALE_SECONDS}
                self.reply({"studios": live, "wanted": wanted})
        elif url.path == "/want":
            place = query["placeId"]
            with lock:
                if query.get("mode") == "release":
                    wanted.pop(place, None)
                else:
                    wanted[place] = {"connect": query.get("mode") == "connect", "host": query.get("host", "localhost"),
                                     "port": query.get("port", ""), "asked": time.time()}
            self.reply({"ok": True})
        elif url.path == "/stop":
            self.reply({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.reply({"error": "unknown path"}, 404)


def call(path):
    with urllib.request.urlopen(BASE + path, timeout=2) as response:
        return json.loads(response.read())


def ensure_daemon():
    try:
        call("/state")
        return
    except OSError:
        pass
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen([sys.executable, __file__, "daemon"], creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(0.15)
        try:
            call("/state")
            return
        except OSError:
            continue
    raise SystemExit("could not start the control service")


def pick_place(state, place):
    if place:
        return place
    if len(state["studios"]) == 1:
        return next(iter(state["studios"]))
    if not state["studios"]:
        raise SystemExit("no Studio is reporting: is Studio open with the patched Rojo plugin? (it polls every 1 to 6 seconds)")
    raise SystemExit("several Studios are reporting; pass --place: " + ", ".join(f"{p} ({i['place']})" for p, i in state["studios"].items()))


def pick_port(host, place, port):
    """The port to connect this place to, and that server's info. Refuses a server that is not the
    project the registry names for that port, or whose servePlaceIds leave this place out."""
    registry = load_registry()["ports"]
    if not port:
        live = [p for p in registry if int(place) in ((server_info(host, p) or {}).get("expectedPlaceIds") or [])]
        listed = [p for p, entry in registry.items() if int(place) in entry.get("places", [])]
        candidates = live or listed
        if len(candidates) != 1:
            raise SystemExit(f"cannot choose a port for place {place} (matches: {candidates or 'none'}); "
                             "pass --port, or list the place with claim --places")
        port = candidates[0]
    port = str(port)
    info = server_info(host, port)
    entry = registry.get(port)
    if info and entry and info.get("projectName") and info["projectName"] != entry["project"]:
        raise SystemExit(f"refused: port {port} is registered to {entry['project']} but is serving {info['projectName']}")
    if info and info.get("expectedPlaceIds") and int(place) not in info["expectedPlaceIds"]:
        raise SystemExit(f"refused: {info.get('projectName')} on port {port} does not list place {place} in servePlaceIds")
    return port, info


def wait_for(place, statuses, seconds):
    deadline = time.time() + seconds
    info = {}
    while time.time() < deadline:
        info = call("/state")["studios"].get(place, {})
        if info.get("status") in statuses:
            return info
        time.sleep(0.5)
    return info


def cmd_ports(host):
    registry = load_registry()
    rows = []
    for port, entry in sorted(registry["ports"].items()):
        info = server_info(host, port)
        serving = "free" if info is None else (info.get("projectName") or "not Rojo")
        flag = "" if info is None or serving == entry["project"] else "  <-- CONFLICT"
        rows.append(f"{port}  {entry['project']:<34} serving: {serving}{flag}")
    for port in range(FIRST_PROJECT_PORT, FIRST_PROJECT_PORT + 40):
        if str(port) not in registry["ports"] and port_in_use(port):
            name = (server_info(host, port) or {}).get("projectName")
            if name:
                rows.append(f"{port}  {'(not registered)':<34} serving: {name}  <-- claim it")
    print(f"registry: {REGISTRY}")
    print("\n".join(rows) or "(empty)")


def cmd_claim(args):
    registry = load_registry()
    ports = registry["ports"]
    for p, entry in ports.items():
        if entry["project"] == args.project and (not args.port or p != str(args.port)):
            raise SystemExit(f"{args.project} already has port {p}")
    port = str(args.port) if args.port else None
    if port and port in ports and ports[port]["project"] != args.project:
        raise SystemExit(f"port {port} belongs to {ports[port]['project']}")
    if port and int(port) == PORT:
        raise SystemExit(f"port {PORT} is the control service")
    if not port:
        port = next(str(p) for p in range(FIRST_PROJECT_PORT, FIRST_PROJECT_PORT + 200)
                    if str(p) not in ports and not port_in_use(p))
    entry = ports.get(port, {"project": args.project})
    if args.repo:
        entry["repo"] = args.repo
    if args.places:
        entry["places"] = [int(x) for x in args.places.split(",") if x.strip()]
    ports[port] = entry
    save_registry(registry)
    print(f"{args.project}: port {port}. Serve with: rojo serve <project file> --port {port}")


def main():
    parser = argparse.ArgumentParser(description="Connect and disconnect Studio's Rojo plugin; one port per project.")
    parser.add_argument("command", choices=["status", "ports", "claim", "connect", "disconnect", "release", "stop", "daemon"])
    parser.add_argument("--place", help="placeId of the Studio to steer")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", help="Rojo server port (connect: found from the registry if left out)")
    parser.add_argument("--project", help="claim: the project name in its .project.json")
    parser.add_argument("--repo", help="claim: the project's repository folder")
    parser.add_argument("--places", help="claim: comma-separated placeIds this project syncs to")
    parser.add_argument("--wait", type=float, default=None, help="seconds to wait for the wanted state")
    args = parser.parse_args()

    if args.command == "daemon":
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
        return
    if args.command == "ports":
        cmd_ports(args.host)
        return
    if args.command == "claim":
        if not args.project:
            raise SystemExit("claim needs --project")
        cmd_claim(args)
        return
    if args.command == "stop":
        try:
            call("/stop")
            print("control service stopped")
        except OSError:
            print("control service was not running")
        return

    ensure_daemon()
    if args.command == "status":
        # Give an already-open Studio one slow poll to show up after the service starts.
        state = call("/state")
        if not state["studios"]:
            time.sleep(7)
            state = call("/state")
        print(json.dumps(state, indent=2))
        return

    state = call("/state")
    if not state["studios"] and not args.place:
        time.sleep(7)
        state = call("/state")
    place = pick_place(state, args.place)
    if args.command == "release":
        call(f"/want?placeId={place}&mode=release")
        print(f"released place {place}")
        return

    if args.command == "connect":
        port, info = pick_port(args.host, place, args.port)
        if info is None:
            print(f"warning: no Rojo server is answering on {args.host}:{port}; the plugin will connect when one is")
        call(f"/want?placeId={place}&mode=connect&host={urllib.parse.quote(args.host)}&port={port}")
        studio = wait_for(place, {"Connected", "Confirming"}, args.wait if args.wait is not None else 20)
        if studio.get("status") == "Confirming":
            print(json.dumps({"place": place, "port": port, "reached": False, "studio": studio}, indent=2))
            print(f"waiting for a person: the first sync would delete {studio.get('removals') or 'some'} instance(s) "
                  "in Studio, so Rojo's confirmation dialog is open in that Studio (rojoctl disconnect closes it "
                  "without applying anything)")
            sys.exit(2)
        ok = studio.get("status") == "Connected"
        print(json.dumps({"place": place, "port": port, "reached": ok, "studio": studio}, indent=2))
        sys.exit(0 if ok else 1)

    call(f"/want?placeId={place}&mode=disconnect")
    studio = wait_for(place, {"NotConnected", "Error"}, args.wait if args.wait is not None else 10)
    ok = studio.get("status") in ("NotConnected", "Error")
    print(json.dumps({"place": place, "reached": ok, "studio": studio}, indent=2))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
