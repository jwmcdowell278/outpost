"""Lightweight local web UI: view live status and control an OutpostClient
(switch active channel, web-based push-to-talk) without a hardware PTT
source or a restart. Requires the 'web' extra (aiohttp) - the import is
guarded so the rest of the app never needs aiohttp installed unless
enable_web_ui is actually turned on.

Security: binds to 127.0.0.1 by default - do not change that to a
non-loopback address without also setting web_ui_token, since the
channel/PTT control endpoints are otherwise unauthenticated. Status/HTML
responses never include channel or mesh keys.
"""
from __future__ import annotations
import base64
import logging

try:
    from aiohttp import web
except ImportError:
    web = None

from .crypto import generate_channel_key

log = logging.getLogger(__name__)


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outpost</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 28rem; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.25rem; }
  .row { margin: 0.75rem 0; }
  button { font-size: 1rem; padding: 0.5rem 1rem; margin: 0.15rem; cursor: pointer; }
  button.active { background: #2a7; color: white; }
  #ptt { width: 100%; padding: 1rem; font-size: 1.1rem; }
  #ptt.pressed { background: #c33; color: white; }
  #status { font-size: 0.85rem; color: #555; white-space: pre-wrap; }
  label { font-size: 0.85rem; color: #555; }
</style>
</head>
<body>
<h1>Outpost</h1>
<div class="row">
  <label>Token (if configured): <input id="token" type="password" size="20"></label>
</div>
<div class="row" id="channels"></div>
<div class="row">
  <button id="ptt">Hold to talk</button>
</div>
<div class="row">
  <button id="genkey">Generate key</button>
  <input id="genkeyOut" type="text" size="46" readonly placeholder="generated key appears here">
  <button id="copykey" type="button">Copy</button>
  <div><label>Paste into a channel's or mesh_key's "key:" field in your config file - not saved automatically.</label></div>
</div>
<pre id="status">loading...</pre>
<script>
const tokenInput = document.getElementById('token');
tokenInput.value = localStorage.getItem('outpost_token') || '';
tokenInput.addEventListener('change', () => localStorage.setItem('outpost_token', tokenInput.value));

function authHeaders() {
  const t = tokenInput.value;
  return t ? { 'Authorization': 'Bearer ' + t } : {};
}

async function api(path, options) {
  const resp = await fetch(path, { ...options, headers: { ...(options && options.headers), ...authHeaders() } });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

async function refresh() {
  try {
    const s = await api('/api/status');
    document.getElementById('status').textContent = JSON.stringify(s, null, 2);
    const channelsDiv = document.getElementById('channels');
    channelsDiv.textContent = '';
    for (const name of s.channels) {
      const btn = document.createElement('button');
      btn.textContent = name;
      if (name === s.active_channel) btn.classList.add('active');
      btn.onclick = () => api('/api/channel', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      }).then(refresh).catch(e => alert(e.message));
      channelsDiv.appendChild(btn);
    }
  } catch (e) {
    document.getElementById('status').textContent = 'error: ' + e.message;
  }
}

function setPtt(pressed) {
  api('/api/ptt', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pressed }),
  }).then(refresh).catch(e => alert(e.message));
}

const pttBtn = document.getElementById('ptt');
pttBtn.addEventListener('mousedown', () => { pttBtn.classList.add('pressed'); setPtt(true); });
pttBtn.addEventListener('touchstart', (e) => { e.preventDefault(); pttBtn.classList.add('pressed'); setPtt(true); });
for (const ev of ['mouseup', 'mouseleave', 'touchend', 'touchcancel']) {
  pttBtn.addEventListener(ev, () => { pttBtn.classList.remove('pressed'); setPtt(false); });
}

document.getElementById('genkey').addEventListener('click', () => {
  api('/api/generate_key', { method: 'POST' })
    .then(r => { document.getElementById('genkeyOut').value = r.key; })
    .catch(e => alert(e.message));
});
document.getElementById('copykey').addEventListener('click', () => {
  const out = document.getElementById('genkeyOut');
  if (out.value) navigator.clipboard.writeText(out.value);
});

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class WebUI:
    def __init__(self, client, host: str = "127.0.0.1", port: int = 8765, token: str | None = None):
        if web is None:
            raise RuntimeError(
                "Web UI requires aiohttp - install the 'web' extra: pip install -e '.[web]'"
            )
        self.client = client
        self.host = host
        self.port = port
        self.token = token
        self._runner: web.AppRunner | None = None

        self.app = web.Application(middlewares=[web.middleware(self._make_auth_middleware())])
        self.app.add_routes([
            web.get("/", self._handle_index),
            web.get("/api/status", self._handle_status),
            web.post("/api/channel", self._handle_set_channel),
            web.post("/api/ptt", self._handle_set_ptt),
            web.post("/api/generate_key", self._handle_generate_key),
        ])

    def _make_auth_middleware(self):
        # A plain closure, not a bound method - aiohttp's `middleware`
        # decorator sets an attribute on the callable, which bound methods
        # don't support.
        async def auth_middleware(request, handler):
            if self.token and request.headers.get("Authorization") != f"Bearer {self.token}":
                raise web.HTTPUnauthorized(text="missing or invalid token")
            return await handler(request)
        return auth_middleware

    async def _handle_index(self, request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def _handle_status(self, request) -> web.Response:
        c = self.client
        return web.json_response({
            "sender_id": c.config.sender_id,
            "active_channel": c.active_channel.name,
            "channels": [ch.name for ch in c.config.channels.values()],
            "ptt_active": c._ptt_active,
            "agc_enabled": c.agc is not None,
            "noise_suppressor": type(c.noise_suppressor).__name__,
            "peer_count": len(c.transport.peers),
        })

    async def _handle_set_channel(self, request) -> web.Response:
        data = await request.json()
        name = data.get("name")
        if not name:
            raise web.HTTPBadRequest(text="missing 'name'")
        try:
            self.client.set_active_channel(name)
        except KeyError:
            raise web.HTTPNotFound(text=f"no channel named {name!r}")
        return web.json_response({"active_channel": self.client.active_channel.name})

    async def _handle_set_ptt(self, request) -> web.Response:
        data = await request.json()
        self.client.set_ptt(bool(data.get("pressed")))
        return web.json_response({"ptt_active": self.client._ptt_active})

    async def _handle_generate_key(self, request) -> web.Response:
        # Generates a fresh key for the caller to copy into their config -
        # never applied automatically, since channels/mesh_key are only
        # loaded from config at startup.
        key = base64.b64encode(generate_channel_key()).decode()
        return web.json_response({"key": key})

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("web UI listening on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
