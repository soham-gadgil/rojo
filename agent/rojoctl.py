"""rojoctl: connect and disconnect Studio's Rojo plugin from the command line.

Works with the patched plugin built from this checkout (agent/apply_patch.py). A small control
service on 127.0.0.1:34860 sits between this command and the plugin: the plugin polls it once a
second, reports its state, and is told to connect or disconnect. The service starts itself the
first time any command needs it.

    python rojoctl.py status                       what each open Studio reports, and wanted state
    python rojoctl.py connect [--port 34872] [--place ID] [--wait 20]
    python rojoctl.py disconnect [--place ID] [--wait 10]
    python rojoctl.py release [--place ID]         stop steering a place; leave it as it is
    python rojoctl.py stop                         stop the control service

--place may be left out when exactly one Studio is reporting. "connect" and "disconnect" are each
one order: the service forgets it once Studio reports it done (or after two minutes unmet), so it
never reconnects a Studio the owner has disconnected by hand. Run "connect" again after a Studio
restart. Exit code 0 means the wanted state was reached within --wait.
"""
import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 34860
BASE = f"http://127.0.0.1:{PORT}"
STALE_SECONDS = 8  # a Studio that has not polled for this long is treated as closed
WISH_SECONDS = 120  # an order nobody carried out in this long is forgotten

lock = threading.Lock()
studios = {}  # placeId -> {place, status, host, port, project, seen}
wanted = {}   # placeId -> {"connect": bool, "host": str, "port": str}


def server_up(host, port):
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/rojo", timeout=0.6):
            return True
    except urllib.error.HTTPError:
        return True  # it answered, so something is serving there
    except OSError:
        return False


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
            with lock:
                studios[place] = {"place": query.get("place", ""), "status": query.get("status", ""), "host": query.get("host", ""),
                                  "port": query.get("port", ""), "project": query.get("project", ""), "seen": time.time()}
                wish = wanted.get(place)
                status = query.get("status", "")
                # A wish is one order, not a state to hold (owner, 2026-11-25): once Studio reports
                # it done, or it has gone unmet for WISH_SECONDS, it is dropped, so a connection the
                # owner then changes by hand stays as they left it.
                done = wish and status == ("Connected" if wish["connect"] else "NotConnected")
                if wish and (done or time.time() - wish["asked"] > WISH_SECONDS):
                    wanted.pop(place, None)
                    wish = None
            order = {}
            if wish and wish["connect"]:
                # Only order a connect when a server is really there, so a stopped server is not hammered.
                if status in ("NotConnected", "Error") and server_up(wish["host"], wish["port"]):
                    order = {"action": "connect", "host": wish["host"], "port": wish["port"]}
            elif wish and not wish["connect"]:
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
                    wanted[place] = {"connect": query.get("mode") == "connect", "host": query.get("host", "localhost"), "port": query.get("port", "34872"),
                                     "asked": time.time()}
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


def wait_for(place, statuses, seconds):
    deadline = time.time() + seconds
    info = {}
    while time.time() < deadline:
        info = call("/state")["studios"].get(place, {})
        if info.get("status") in statuses:
            return True, info
        time.sleep(0.5)
    return False, info


def main():
    parser = argparse.ArgumentParser(description="Connect and disconnect Studio's Rojo plugin.")
    parser.add_argument("command", choices=["status", "connect", "disconnect", "release", "stop", "daemon"])
    parser.add_argument("--place", help="placeId of the Studio to steer")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", default="34872")
    parser.add_argument("--wait", type=float, default=None, help="seconds to wait for the wanted state")
    args = parser.parse_args()

    if args.command == "daemon":
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
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
    call(f"/want?placeId={place}&mode={args.command}&host={urllib.parse.quote(args.host)}&port={args.port}")
    if args.command == "connect":
        if not server_up(args.host, args.port):
            print(f"warning: no Rojo server is answering on {args.host}:{args.port}; the plugin will connect when one is")
        ok, info = wait_for(place, {"Connected"}, args.wait if args.wait is not None else 20)
    else:
        ok, info = wait_for(place, {"NotConnected"}, args.wait if args.wait is not None else 10)
    print(json.dumps({"place": place, "reached": ok, "studio": info}, indent=2))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
