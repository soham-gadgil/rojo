"""Patches Rojo's Studio plugin (this checkout, v7.7.0) so an agent can connect and disconnect it.

The patched plugin asks a local control service (rojoctl.py daemon, 127.0.0.1:34860) once a second
what it should do and tells it its state. With no service running it behaves exactly like stock
Rojo. A connect ordered by the agent accepts the first sync without the confirmation dialog.

Run from the repository root:  python agent/apply_patch.py
Then build:                    rojo build plugin.project.json --output Rojo.rbxm
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "plugin" / "src" / "App" / "init.lua"
source = path.read_text(encoding="utf-8")

if "startAgentControl" in source:
    raise SystemExit("already patched")


def replace(old, new):
    global source
    assert source.count(old) == 1, old[:60]
    source = source.replace(old, new)


# 1. Start the control loop when the plugin loads in Edit.
replace(
    """	if self:isAutoConnectPlaytestServerAvailable() then
		self:useRunningConnectionInfo()
		self:startSession()
	end
""",
    """	if self:isAutoConnectPlaytestServerAvailable() then
		self:useRunningConnectionInfo()
		self:startSession()
	end

	if RunService:IsEdit() then
		self:startAgentControl()
	end
""",
)

# 2. Stop it when the plugin unloads.
replace(
    """function App:willUnmount()
	self:endSession()
""",
    """function App:willUnmount()
	self.agentControlStopped = true
	self:endSession()
""",
)

# 3. A connect ordered by the agent needs no confirmation click.
replace(
    """		-- Play solo auto-connect does not require confirmation
		if self:isAutoConnectPlaytestServerAvailable() then""",
    """		-- A connect ordered through the agent control service does not require confirmation
		if self.agentConnect then
			Log.trace("Accepting patch without confirmation because the agent control service ordered the connect")
			return "Accept"
		end

		-- Play solo auto-connect does not require confirmation
		if self:isAutoConnectPlaytestServerAvailable() then""",
)

# 4. A manual disconnect or connect clears the agent flag; the loop itself.
replace(
    """function App:endSession()
	if self.serveSession == nil then
		return
	end
""",
    """-- Agent control: polls a local service that an agent's CLI talks to, reports this Studio's sync
-- state, and connects or disconnects when told. Does nothing if the service is not running.
function App:startAgentControl()
	local HttpService = game:GetService("HttpService")
	local base = "http://localhost:34860"

	task.spawn(function()
		while not self.agentControlStopped do
			local host, port = self:getHostAndPort()
			local status = self.state.appStatus
			local project = if status == AppStatus.Connected then tostring(self.state.projectName or "") else ""
			local url = string.format(
				"%s/poll?placeId=%s&place=%s&status=%s&host=%s&port=%s&project=%s",
				base,
				tostring(game.PlaceId),
				HttpService:UrlEncode(game.Name),
				tostring(status),
				HttpService:UrlEncode(tostring(host)),
				HttpService:UrlEncode(tostring(port)),
				HttpService:UrlEncode(project)
			)

			local ok, response = pcall(function()
				return HttpService:RequestAsync({ Url = url, Method = "GET" })
			end)
			local order = nil
			if ok and response.Success then
				local decoded, body = pcall(HttpService.JSONDecode, HttpService, response.Body)
				if decoded and type(body) == "table" then
					order = body
				end
			end

			if order and order.action == "connect" then
				if status == AppStatus.NotConnected or status == AppStatus.Error then
					Log.info("Agent control: connecting to {}:{}", order.host, order.port)
					self.setHost(tostring(order.host))
					self.setPort(tostring(order.port))
					self.agentConnect = true
					self:startSession()
				end
			elseif order and order.action == "disconnect" then
				if self.serveSession ~= nil then
					Log.info("Agent control: disconnecting")
					self:endSession()
				end
			end

			-- Poll quickly while the service answers, slowly while it is absent.
			task.wait(if ok then 1 else 6)
		end
	end)
end

function App:endSession()
	self.agentConnect = false
	if self.serveSession == nil then
		return
	end
""",
)

path.write_text(source, encoding="utf-8", newline="\n")
print("patched", path)
