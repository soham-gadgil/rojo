"""Patches Rojo's Studio plugin so an agent can connect and disconnect it (agent/plugin.patch, made
against Rojo v7.7.0).

The patched plugin asks a local control service (rojoctl.py daemon, 127.0.0.1:34860) once a second
what it should do and tells it its state. With no service running it behaves exactly like stock
Rojo. A connect ordered by the agent accepts the first sync without the confirmation dialog only
when that sync deletes nothing in Studio; otherwise the normal dialog opens for a person.

Run from the root of a clean Rojo checkout:  python agent/apply_patch.py
Then build:                                  rojo build plugin.project.json --output Rojo.rbxm
To refresh the patch after editing init.lua: git diff v7.7.0 -- plugin/src/App/init.lua > agent/plugin.patch
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "plugin" / "src" / "App" / "init.lua").read_text(encoding="utf-8")
if "startAgentControl" in source:
    raise SystemExit("already patched")
subprocess.run(["git", "apply", "--whitespace=nowarn", str(ROOT / "agent" / "plugin.patch")], cwd=ROOT, check=True)
print("patched plugin/src/App/init.lua")
