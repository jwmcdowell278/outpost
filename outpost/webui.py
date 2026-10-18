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

from .crypto import generate_channel_key, KEY_SIZE
from .config import (
    EDITABLE_FIELDS,
    editable_config_values,
    save_config_fields,
    add_channel_to_file,
    rename_channel_in_file,
    rotate_channel_key_in_file,
)

log = logging.getLogger(__name__)

# Shared <head> styling for both pages - kept as one constant so the two
# pages can't visually drift apart.
_STYLE = """
<style>
  :root {
    color-scheme: light dark;
    --bg: #f4f5f7; --card: #fff; --text: #1a1a1a; --muted: #666;
    --border: #e1e3e6; --accent: #2a7; --accent-text: #fff; --danger: #c33;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #15171a; --card: #1e2125; --text: #eee; --muted: #9aa0a6; --border: #2c3034; }
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, system-ui, sans-serif; max-width: 34rem; margin: 0 auto;
    padding: 1rem 1rem 3rem; background: var(--bg); color: var(--text);
  }
  h1 { font-size: 1.35rem; margin: 1rem 0 0.5rem; }
  h2 { font-size: 0.95rem; margin: 0 0 0.6rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  nav.tabs { display: flex; gap: 0.3rem; border-bottom: 1px solid var(--border); margin: 0 0 0.2rem; }
  nav.tabs a {
    padding: 0.5rem 0.9rem; font-size: 0.9rem; font-weight: 600; text-decoration: none;
    color: var(--muted); border-bottom: 2px solid transparent; margin-bottom: -1px;
  }
  nav.tabs a.active { color: var(--text); border-bottom-color: var(--accent); }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 0.6rem; padding: 1rem; margin: 0.9rem 0; }
  .row { margin: 0.5rem 0; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  button {
    font-size: 0.95rem; padding: 0.45rem 0.9rem; cursor: pointer; border-radius: 0.4rem;
    border: 1px solid var(--border); background: var(--card); color: var(--text);
  }
  button.active { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
  #ptt { width: 100%; padding: 1.1rem; font-size: 1.1rem; font-weight: 600; }
  #ptt.pressed { background: var(--danger); color: white; border-color: var(--danger); }
  label { font-size: 0.85rem; color: var(--muted); }
  input[type=text], input[type=password], input[type=number] {
    font-size: 0.9rem; padding: 0.3rem 0.5rem; border-radius: 0.35rem; border: 1px solid var(--border);
    background: var(--bg); color: var(--text);
  }
  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  th, td { text-align: left; padding: 0.3rem 0.4rem; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; }
  .pill { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 1rem; font-size: 0.72rem; background: var(--border); }
  .pill.live { background: var(--accent); color: var(--accent-text); }
  .field-row { justify-content: space-between; }
  .field-row .name { font-size: 0.88rem; }
  .muted { color: var(--muted); font-size: 0.8rem; }
  #saveMsg { font-size: 0.82rem; min-height: 1.1rem; }
  #saveMsg.error { color: var(--danger); }
  #saveMsg.ok { color: var(--accent); }
</style>
"""

# Shared token-input + fetch helper script, included on both pages since
# each is a separate full page load (no client-side router).
_CONNECTION_SCRIPT = """
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
"""


def _nav(active: str) -> str:
    def cls(name: str) -> str:
        return ' class="active"' if name == active else ""
    return f"""<nav class="tabs">
  <a href="/"{cls('status')}>Status</a>
  <a href="/channels"{cls('channels')}>Channels</a>
  <a href="/settings"{cls('settings')}>Settings</a>
</nav>"""


_INDEX_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outpost</title>
{_STYLE}
</head>
<body>
<h1>Outpost</h1>
{_nav('status')}

<div class="card">
  <h2>Connection</h2>
  <div class="row"><label>Token (if configured): <input id="token" type="password" size="20"></label></div>
  <div class="muted" id="statusLine">loading...</div>
</div>

<div class="card">
  <h2>Channel</h2>
  <div class="row" id="channels"></div>
  <button id="ptt">Hold to talk</button>
</div>

<div class="card">
  <h2>Peers (<span id="peerCount">0</span>)</h2>
  <table id="peerTable"><thead><tr><th>Address</th><th>Candidates</th><th>Age</th></tr></thead><tbody></tbody></table>
</div>

<div class="card">
  <h2>Channel members</h2>
  <table id="memberTable"><thead><tr><th>Sender</th><th>Address</th><th>Age</th></tr></thead><tbody></tbody></table>
</div>

<script>
{_CONNECTION_SCRIPT}

function fmtAge(s) {{
  if (s < 60) return s.toFixed(0) + 's';
  if (s < 3600) return (s / 60).toFixed(0) + 'm';
  return (s / 3600).toFixed(1) + 'h';
}}

function fillTable(bodyId, rows, renderRow) {{
  const tbody = document.querySelector('#' + bodyId + ' tbody');
  tbody.textContent = '';
  for (const row of rows) {{
    const tr = document.createElement('tr');
    tr.innerHTML = renderRow(row);
    tbody.appendChild(tr);
  }}
}}

async function refresh() {{
  try {{
    const s = await api('/api/status');
    document.getElementById('statusLine').textContent =
      `sender_id ${{s.sender_id}} · AGC ${{s.agc_enabled ? 'on' : 'off'}} · HPF ${{s.highpass_enabled ? 'on' : 'off'}} · squelch ${{s.squelch_enabled ? 'on' : 'off'}} · relay ${{s.relay_enabled ? 'on' : 'off'}}` +
      (s.tx_lockout_enabled ? ` · lockout ${{s.channel_busy ? 'BUSY' : 'on'}}` : '');

    const channelsDiv = document.getElementById('channels');
    channelsDiv.textContent = '';
    for (const name of s.channels) {{
      const btn = document.createElement('button');
      btn.textContent = name;
      if (name === s.active_channel) btn.classList.add('active');
      btn.onclick = () => api('/api/channel', {{
        method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ name }}),
      }}).then(refresh).catch(e => alert(e.message));
      channelsDiv.appendChild(btn);
    }}

    document.getElementById('peerCount').textContent = s.peer_count;
    fillTable('peerTable', s.peers, p =>
      `<td>${{p.host}}:${{p.port}}${{p.static ? ' <span class="pill">static</span>' : ''}}</td><td>${{p.candidate_count}}</td><td>${{fmtAge(p.age_s)}}</td>`);
    fillTable('memberTable', s.channel_members, m =>
      `<td>${{m.sender_id}}</td><td>${{m.host}}:${{m.port}}</td><td>${{fmtAge(m.age_s)}}</td>`);
  }} catch (e) {{
    document.getElementById('statusLine').textContent = 'error: ' + e.message;
  }}
}}

function setPtt(pressed) {{
  api('/api/ptt', {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ pressed }}),
  }}).then(refresh).catch(e => alert(e.message));
}}

const pttBtn = document.getElementById('ptt');
pttBtn.addEventListener('mousedown', () => {{ pttBtn.classList.add('pressed'); setPtt(true); }});
pttBtn.addEventListener('touchstart', (e) => {{ e.preventDefault(); pttBtn.classList.add('pressed'); setPtt(true); }});
for (const ev of ['mouseup', 'mouseleave', 'touchend', 'touchcancel']) {{
  pttBtn.addEventListener(ev, () => {{ pttBtn.classList.remove('pressed'); setPtt(false); }});
}}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""

_SETTINGS_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outpost · Settings</title>
{_STYLE}
</head>
<body>
<h1>Outpost</h1>
{_nav('settings')}

<div class="card">
  <h2>Connection</h2>
  <div class="row"><label>Token (if configured): <input id="token" type="password" size="20"></label></div>
</div>

<div class="card">
  <h2>Settings</h2>
  <div id="settingsFields">loading...</div>
  <div class="row">
    <button id="saveSettings">Save</button>
    <span id="saveMsg"></span>
  </div>
</div>

<div class="card">
  <h2>Key generator</h2>
  <div class="row">
    <button id="genkey">Generate key</button>
    <input id="genkeyOut" type="text" size="40" readonly placeholder="generated key appears here">
    <button id="copykey" type="button">Copy</button>
  </div>
  <div class="muted">Paste into a channel's or mesh_key's "key:" field in your config file - not saved automatically.</div>
</div>

<script>
{_CONNECTION_SCRIPT}

function settingsInput(name, field) {{
  const v = field.value;
  if (typeof v === 'boolean') {{
    return `<input type="checkbox" data-field="${{name}}" ${{v ? 'checked' : ''}}>`;
  }}
  if (typeof v === 'number') {{
    return `<input type="number" step="any" data-field="${{name}}" value="${{v}}" size="10">`;
  }}
  return `<input type="text" data-field="${{name}}" value="${{v ?? ''}}" size="20">`;
}}

async function loadSettings() {{
  const container = document.getElementById('settingsFields');
  try {{
    const cfg = await api('/api/config');
    container.textContent = '';
    for (const [name, field] of Object.entries(cfg.fields)) {{
      const row = document.createElement('div');
      row.className = 'row field-row';
      const livePill = field.live ? '<span class="pill live">live</span>' : '<span class="pill">restart</span>';
      row.innerHTML = `<span class="name">${{name}} ${{livePill}}</span>${{settingsInput(name, field)}}`;
      container.appendChild(row);
    }}
    document.getElementById('saveSettings').disabled = !cfg.can_save;
    if (!cfg.can_save) {{
      document.getElementById('saveMsg').textContent = 'no config file path - settings are read-only here';
    }}
  }} catch (e) {{
    container.textContent = 'error: ' + e.message;
  }}
}}

document.getElementById('saveSettings').addEventListener('click', async () => {{
  const msg = document.getElementById('saveMsg');
  msg.className = ''; msg.textContent = 'saving...';
  const updates = {{}};
  for (const el of document.querySelectorAll('#settingsFields [data-field]')) {{
    const name = el.dataset.field;
    updates[name] = el.type === 'checkbox' ? el.checked : (el.type === 'number' ? Number(el.value) : el.value);
  }}
  try {{
    const result = await api('/api/config', {{
      method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(updates),
    }});
    msg.className = 'ok';
    msg.textContent = result.restart_required.length
      ? `saved - restart required for: ${{result.restart_required.join(', ')}}`
      : 'saved and applied';
  }} catch (e) {{
    msg.className = 'error';
    msg.textContent = e.message;
  }}
}});

document.getElementById('genkey').addEventListener('click', () => {{
  api('/api/generate_key', {{ method: 'POST' }})
    .then(r => {{ document.getElementById('genkeyOut').value = r.key; }})
    .catch(e => alert(e.message));
}});
document.getElementById('copykey').addEventListener('click', () => {{
  const out = document.getElementById('genkeyOut');
  if (out.value) navigator.clipboard.writeText(out.value);
}});

loadSettings();
</script>
</body>
</html>
"""

_CHANNELS_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outpost · Channels</title>
{_STYLE}
</head>
<body>
<h1>Outpost</h1>
{_nav('channels')}

<div class="card">
  <h2>Connection</h2>
  <div class="row"><label>Token (if configured): <input id="token" type="password" size="20"></label></div>
</div>

<div class="card">
  <h2>Channels</h2>
  <div id="channelList">loading...</div>
  <div class="muted" id="channelsMsg"></div>
</div>

<div class="card" id="newChannelCard">
  <h2>New channel</h2>
  <div class="row">
    <input id="newName" type="text" size="16" placeholder="name">
    <input id="newKey" type="text" size="40" placeholder="key (base64, optional - leave blank to generate one)">
    <button id="genNewKey" type="button">Generate key</button>
    <button id="createChannel">Create</button>
  </div>
  <div class="muted">Creating a channel takes effect immediately on this node - no restart needed. Copy the resulting key into every other peer's config that should be able to join it.</div>
</div>

<script>
{_CONNECTION_SCRIPT}

function newKeyBanner(id, label, key) {{
  const div = document.createElement('div');
  div.className = 'row';
  div.innerHTML = `
    <span class="pill live">${{label}}</span>
    <input type="text" size="40" readonly value="${{key}}">
    <button type="button" class="copyKeyBtn">Copy</button>
  `;
  div.querySelector('.copyKeyBtn').addEventListener('click', () => navigator.clipboard.writeText(key));
  return div;
}}

async function loadChannels() {{
  const list = document.getElementById('channelList');
  const msg = document.getElementById('channelsMsg');
  try {{
    const data = await api('/api/channels');
    list.textContent = '';
    for (const ch of data.channels.sort((a, b) => a.id - b.id)) {{
      const row = document.createElement('div');
      row.className = 'row field-row';
      row.innerHTML = `
        <span class="name">#${{ch.id}}</span>
        <input type="text" value="${{ch.name}}" data-id="${{ch.id}}" class="chName" size="16">
        <button type="button" class="renameBtn" data-id="${{ch.id}}">Rename</button>
        <button type="button" class="rotateBtn" data-id="${{ch.id}}">Rotate key</button>
      `;
      list.appendChild(row);
    }}
    document.getElementById('createChannel').disabled = !data.can_save;
    if (!data.can_save) {{
      msg.textContent = 'no config file path - channels are read-only here';
    }}
  }} catch (e) {{
    list.textContent = 'error: ' + e.message;
  }}
}}

document.getElementById('channelList').addEventListener('click', async (e) => {{
  const id = e.target.dataset.id;
  if (!id) return;
  const msg = document.getElementById('channelsMsg');
  if (e.target.classList.contains('renameBtn')) {{
    const name = document.querySelector(`.chName[data-id="${{id}}"]`).value.trim();
    if (!name) return;
    try {{
      await api(`/api/channels/${{id}}/rename`, {{
        method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ name }}),
      }});
      msg.className = 'ok'; msg.textContent = `renamed #${{id}} to ${{name}}`;
    }} catch (err) {{
      msg.className = 'error'; msg.textContent = err.message;
    }}
  }} else if (e.target.classList.contains('rotateBtn')) {{
    if (!confirm('Rotate this channel\\'s key? Every other peer on it needs the new key before they can hear/be heard again.')) return;
    try {{
      const result = await api(`/api/channels/${{id}}/rotate_key`, {{ method: 'POST' }});
      msg.className = ''; msg.textContent = '';
      document.getElementById('channelList').after(newKeyBanner(id, `#${{id}} new key`, result.key));
    }} catch (err) {{
      msg.className = 'error'; msg.textContent = err.message;
    }}
  }}
}});

document.getElementById('genNewKey').addEventListener('click', () => {{
  api('/api/generate_key', {{ method: 'POST' }})
    .then(r => {{ document.getElementById('newKey').value = r.key; }})
    .catch(e => {{ document.getElementById('channelsMsg').className = 'error'; document.getElementById('channelsMsg').textContent = e.message; }});
}});

document.getElementById('createChannel').addEventListener('click', async () => {{
  const name = document.getElementById('newName').value.trim();
  const key = document.getElementById('newKey').value.trim();
  const msg = document.getElementById('channelsMsg');
  if (!name) {{ msg.className = 'error'; msg.textContent = 'name is required'; return; }}
  try {{
    const result = await api('/api/channels', {{
      method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(key ? {{ name, key }} : {{ name }}),
    }});
    document.getElementById('newName').value = '';
    document.getElementById('newKey').value = '';
    msg.className = ''; msg.textContent = '';
    document.getElementById('newChannelCard').after(newKeyBanner(result.id, `#${{result.id}} ${{result.name}} key`, result.key));
    loadChannels();
  }} catch (err) {{
    msg.className = 'error'; msg.textContent = err.message;
  }}
}});

loadChannels();
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
            web.get("/settings", self._handle_settings_page),
            web.get("/channels", self._handle_channels_page),
            web.get("/api/status", self._handle_status),
            web.post("/api/channel", self._handle_set_channel),
            web.post("/api/ptt", self._handle_set_ptt),
            web.post("/api/generate_key", self._handle_generate_key),
            web.get("/api/config", self._handle_get_config),
            web.post("/api/config", self._handle_set_config),
            web.get("/api/channels", self._handle_get_channels),
            web.post("/api/channels", self._handle_create_channel),
            web.post(r"/api/channels/{channel_id:\d+}/rename", self._handle_rename_channel),
            web.post(r"/api/channels/{channel_id:\d+}/rotate_key", self._handle_rotate_channel_key),
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

    async def _handle_settings_page(self, request) -> web.Response:
        return web.Response(text=_SETTINGS_HTML, content_type="text/html")

    async def _handle_channels_page(self, request) -> web.Response:
        return web.Response(text=_CHANNELS_HTML, content_type="text/html")

    async def _handle_status(self, request) -> web.Response:
        c = self.client
        return web.json_response({
            "sender_id": c.config.sender_id,
            "active_channel": c.active_channel.name,
            "channels": [ch.name for ch in c.config.channels.values()],
            "ptt_active": c._ptt_active,
            "agc_enabled": c.agc_enabled,
            "agc_target_rms": c.agc.target_rms,
            "relay_enabled": c.enable_relay,
            "highpass_enabled": c.highpass_enabled,
            "squelch_enabled": c.squelch_enabled,
            "squelch_threshold_rms": c.squelch.threshold_rms,
            "tx_lockout_enabled": c.enable_tx_lockout,
            "channel_busy": c.channel_busy(),
            "peer_count": len(c.transport.peers),
            "peers": c.transport.peer_details(),
            "channel_members": c.channel_member_details(),
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

    async def _handle_get_channels(self, request) -> web.Response:
        channels = [{"id": ch.id, "name": ch.name} for ch in self.client.config.channels.values()]
        return web.json_response({"channels": channels, "can_save": self.client.config_path is not None})

    async def _handle_create_channel(self, request) -> web.Response:
        if self.client.config_path is None:
            raise web.HTTPBadRequest(text="this client wasn't started from a config file - nothing to save to")
        data = await request.json()
        name = (data.get("name") or "").strip()
        if not name:
            raise web.HTTPBadRequest(text="missing 'name'")
        if any(ch.name == name for ch in self.client.config.channels.values()):
            raise web.HTTPBadRequest(text=f"a channel named {name!r} already exists")

        raw_key = data.get("key")
        if raw_key:
            try:
                key = base64.b64decode(raw_key)
            except Exception:
                raise web.HTTPBadRequest(text="'key' must be base64")
            if len(key) != KEY_SIZE:
                raise web.HTTPBadRequest(text=f"'key' must decode to {KEY_SIZE} bytes")
        else:
            key = generate_channel_key()

        new_id = max((ch.id for ch in self.client.config.channels.values()), default=0) + 1
        add_channel_to_file(self.client.config_path, new_id, name, key)
        self.client.add_channel(new_id, name, key)
        return web.json_response({"id": new_id, "name": name, "key": base64.b64encode(key).decode()})

    async def _handle_rename_channel(self, request) -> web.Response:
        if self.client.config_path is None:
            raise web.HTTPBadRequest(text="this client wasn't started from a config file - nothing to save to")
        channel_id = int(request.match_info["channel_id"])
        if channel_id not in self.client.config.channels:
            raise web.HTTPNotFound(text=f"no channel with id {channel_id}")
        data = await request.json()
        name = (data.get("name") or "").strip()
        if not name:
            raise web.HTTPBadRequest(text="missing 'name'")
        if any(ch.name == name and ch.id != channel_id for ch in self.client.config.channels.values()):
            raise web.HTTPBadRequest(text=f"a channel named {name!r} already exists")

        rename_channel_in_file(self.client.config_path, channel_id, name)
        self.client.rename_channel(channel_id, name)
        return web.json_response({"id": channel_id, "name": name})

    async def _handle_rotate_channel_key(self, request) -> web.Response:
        if self.client.config_path is None:
            raise web.HTTPBadRequest(text="this client wasn't started from a config file - nothing to save to")
        channel_id = int(request.match_info["channel_id"])
        if channel_id not in self.client.config.channels:
            raise web.HTTPNotFound(text=f"no channel with id {channel_id}")

        key = generate_channel_key()
        rotate_channel_key_in_file(self.client.config_path, channel_id, key)
        self.client.rotate_channel_key(channel_id, key)
        return web.json_response({"id": channel_id, "key": base64.b64encode(key).decode()})

    async def _handle_get_config(self, request) -> web.Response:
        values = editable_config_values(self.client.config)
        fields = {
            name: {"value": values[name], "live": live}
            for name, (_validator, live) in EDITABLE_FIELDS.items()
        }
        return web.json_response({"fields": fields, "can_save": self.client.config_path is not None})

    async def _handle_set_config(self, request) -> web.Response:
        if self.client.config_path is None:
            raise web.HTTPBadRequest(text="this client wasn't started from a config file - nothing to save to")
        data = await request.json()
        if not isinstance(data, dict) or not data:
            raise web.HTTPBadRequest(text="expected a non-empty JSON object of field -> value")

        validated = {}
        for name, raw_value in data.items():
            spec = EDITABLE_FIELDS.get(name)
            if spec is None:
                raise web.HTTPBadRequest(text=f"{name!r} is not an editable setting")
            validator, _live = spec
            try:
                validated[name] = validator(raw_value)
            except (TypeError, ValueError) as exc:
                raise web.HTTPBadRequest(text=f"{name}: {exc}")

        save_config_fields(self.client.config_path, validated)

        applied_live = []
        restart_required = []
        for name, value in validated.items():
            if EDITABLE_FIELDS[name][1]:
                applied_live.append(name)
            else:
                restart_required.append(name)
        if "enable_agc" in validated:
            self.client.agc_enabled = validated["enable_agc"]
        if "agc_target_rms" in validated:
            self.client.agc.target_rms = validated["agc_target_rms"]
        if "enable_relay" in validated:
            self.client.enable_relay = validated["enable_relay"]
        if "enable_highpass" in validated:
            self.client.highpass_enabled = validated["enable_highpass"]
        if "highpass_cutoff_hz" in validated:
            self.client.highpass.set_cutoff(validated["highpass_cutoff_hz"])
        if "enable_squelch" in validated:
            self.client.squelch_enabled = validated["enable_squelch"]
        if "squelch_threshold_rms" in validated:
            self.client.squelch.threshold_rms = validated["squelch_threshold_rms"]
        if "squelch_hold_ms" in validated:
            self.client.squelch.hold_frames = max(1, round(validated["squelch_hold_ms"] / self.client.config.frame_ms))
        if "enable_tx_lockout" in validated:
            self.client.enable_tx_lockout = validated["enable_tx_lockout"]
        if "tx_lockout_timeout_ms" in validated:
            self.client.tx_lockout_timeout_s = validated["tx_lockout_timeout_ms"] / 1000.0

        return web.json_response({
            "saved": sorted(validated),
            "applied_live": applied_live,
            "restart_required": restart_required,
        })

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
