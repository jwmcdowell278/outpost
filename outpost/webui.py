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
from dataclasses import replace

try:
    from aiohttp import web
except ImportError:
    web = None

from .crypto import generate_channel_key, KEY_SIZE
from .config import (
    EDITABLE_FIELDS,
    editable_config_values,
    save_config_fields,
    export_config_text,
    validate_config_yaml,
    write_config_text,
    add_channel_to_file,
    rename_channel_in_file,
    rotate_channel_key_in_file,
    add_peer_to_file,
    remove_peer_from_file,
    _validate_volume,
)

log = logging.getLogger(__name__)

# Compass-star-over-mesh-nodes glyph (see branding/outpost-icon.svg for the
# full badge this is a simplified, small-size-legible version of).
_FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <circle cx="256" cy="256" r="248" fill="#181b12"/>
  <circle cx="256" cy="256" r="234" fill="none" stroke="#4b5320" stroke-width="20"/>
  <path d="M 256.0,150.0 L 272.0,222.0 L 330.4,184.0 L 294.0,242.4 L 362.0,256.0 L 294.0,269.6 L 330.4,328.0 L 272.0,290.0 L 256.0,362.0 L 240.0,290.0 L 181.6,328.0 L 218.0,269.6 L 150.0,256.0 L 218.0,242.4 L 181.6,184.0 L 240.0,222.0 Z"
        fill="#e8a33d"/>
  <circle cx="256" cy="256" r="26" fill="#181b12" stroke="#e8a33d" stroke-width="10"/>
</svg>
"""

_FAVICON_LINK = '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'

# Shared <head> styling for both pages - kept as one constant so the two
# pages can't visually drift apart.
_STYLE = """
<style>
  :root {
    color-scheme: light dark;
    --bg: #f3f1e7; --card: #fff; --text: #1a1a1a; --muted: #716b59;
    --border: #ddd6c0; --accent: #b9791f; --accent-text: #fff; --danger: #c33;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #14160f; --card: #1c2016; --text: #ece6d8; --muted: #9aa083; --border: #363c2c; --accent: #e8a33d; --accent-text: #14160f; }
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, system-ui, sans-serif; max-width: 34rem; margin: 0 auto;
    padding: 1rem 1rem 3rem; background: var(--bg); color: var(--text);
  }
  h1 { font-size: 1.35rem; margin: 1rem 0 0.5rem; display: flex; align-items: center; gap: 0.5rem; letter-spacing: 0.02em; }
  h1 img.brand { height: 1.5em; width: 1.5em; }
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

_BRAND_H1 = '<h1><img class="brand" src="/favicon.svg" alt="">Outpost</h1>'

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
  <a href="/peers"{cls('peers')}>Peers</a>
  <a href="/settings"{cls('settings')}>Settings</a>
</nav>"""


_INDEX_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outpost</title>
{_FAVICON_LINK}
{_STYLE}
</head>
<body>
{_BRAND_H1}
{_nav('status')}

<div class="card">
  <h2>Connection</h2>
  <div class="row"><label>Token (if configured): <input id="token" type="password" size="20"></label></div>
  <div class="muted" id="statusLine">loading...</div>
</div>

<div class="card">
  <h2>Channels</h2>
  <div class="muted">Talk on one channel; listen to as many as you like.</div>
  <table id="channelTable"><thead><tr><th>Talk on</th><th>Listen</th><th>Volume</th><th>Speaking</th></tr></thead><tbody></tbody></table>
  <button id="ptt">Hold to talk</button>
  <div class="muted" id="speakingLine">loading...</div>
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
      `sender_id ${{s.sender_id}}${{s.sender_alias ? ' (' + s.sender_alias + ')' : ''}} · AGC ${{s.agc_enabled ? 'on' : 'off'}} · HPF ${{s.highpass_enabled ? 'on' : 'off'}} · squelch ${{s.squelch_enabled ? 'on' : 'off'}} · relay ${{s.relay_only ? 'only' : (s.relay_enabled ? 'on' : 'off')}}${{s.bridge_role ? ' (' + s.bridge_role + ' bridge)' : ''}}` +
      (s.tx_lockout_enabled ? ` · lockout ${{s.channel_busy ? 'BUSY' : 'on'}}` : '') +
      (s.multicast.enabled ? ` · multicast: ${{s.multicast.site_listeners}} on this site` +
        (s.multicast.active_channel_uses_multicast ? ' (in use)' : ' (unicast - below threshold)') : '');

    const speakingLine = document.getElementById('speakingLine');
    speakingLine.textContent = s.speaking.length
      ? '\U0001F50A speaking: ' + s.speaking.map(p => p.alias || ('sender ' + p.sender_id)).join(', ')
      : '\U0001F507 no one speaking';

    renderChannels(s.channel_list);

    document.getElementById('peerCount').textContent = s.peer_count;
    fillTable('peerTable', s.peers, p =>
      `<td>${{p.host}}:${{p.port}}${{p.static ? ' <span class="pill">static</span>' : ''}}</td><td>${{p.candidate_count}}</td><td>${{fmtAge(p.age_s)}}</td>`);
    fillTable('memberTable', s.channel_members, m =>
      `<td>${{m.sender_id}}</td><td>${{m.host}}:${{m.port}}</td><td>${{fmtAge(m.age_s)}}</td>`);
  }} catch (e) {{
    document.getElementById('statusLine').textContent = 'error: ' + e.message;
  }}
}}

const postJson = (path, body) => api(path, {{
  method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(body),
}});

function renderChannels(list) {{
  const tbody = document.querySelector('#channelTable tbody');
  // Don't rebuild rows while a volume slider is being dragged.
  if (tbody.contains(document.activeElement) && document.activeElement.type === 'range') return;
  tbody.textContent = '';
  for (const ch of list) {{
    const tr = document.createElement('tr');

    const talk = document.createElement('button');
    talk.textContent = ch.name;
    if (ch.active) talk.classList.add('active');
    talk.onclick = () => postJson('/api/channel', {{ name: ch.name }}).then(refresh).catch(e => alert(e.message));

    const listen = document.createElement('input');
    listen.type = 'checkbox';
    listen.checked = ch.active || ch.monitored;
    listen.disabled = ch.active;  // always hear the channel you talk on
    listen.title = ch.active ? 'You always hear the channel you talk on' : 'Also listen to this channel';
    listen.onchange = () => postJson('/api/monitor', {{ name: ch.name, listen: listen.checked }})
      .then(refresh).catch(e => alert(e.message));

    const vol = document.createElement('input');
    vol.type = 'range'; vol.min = 0; vol.max = 100; vol.value = Math.round(ch.volume * 100);
    vol.onchange = () => postJson('/api/monitor', {{ name: ch.name, volume: vol.value / 100 }})
      .catch(e => alert(e.message));

    const speaking = document.createElement('span');
    speaking.textContent = ch.speaking.map(p => p.alias || ('sender ' + p.sender_id)).join(', ');

    for (const el of [talk, listen, vol, speaking]) {{
      const td = document.createElement('td'); td.appendChild(el); tr.appendChild(td);
    }}
    tbody.appendChild(tr);
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
{_FAVICON_LINK}
{_STYLE}
</head>
<body>
{_BRAND_H1}
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
    <button id="restartBtn" type="button" style="display:none;">Restart now</button>
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

<div class="card">
  <h2>Import / export config</h2>
  <div class="row">
    <button id="exportConfig" type="button">Export config</button>
    <span class="muted">Downloads the full outpost.yaml, including channel/mesh keys - handle the file like a password.</span>
  </div>
  <div class="row">
    <input id="importFile" type="file" accept=".yaml,.yml,text/yaml">
    <button id="importConfig" type="button">Import</button>
  </div>
  <div class="muted">Importing replaces the <strong>entire</strong> config file (keys, channels, peers, network
    profile, web UI settings) and requires a restart to take effect - it does not apply anything live. A bad
    import (e.g. a different <code>web_ui_token</code> or <code>web_ui_host</code>) can lock you out of this
    page after the restart, so only import a file you trust.</div>
  <div id="importMsg"></div>
</div>

<script>
{_CONNECTION_SCRIPT}

// Audio device pickers: name -> list of devices, filled by loadSettings.
const AUDIO_DEVICE_FIELDS = {{ input_device: 'input', output_device: 'output' }};
let audioDevices = {{ input: [], output: [] }};

function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }})[c]);
}}

function deviceSelect(name, kind, v) {{
  const devices = audioDevices[kind] || [];
  // Saved value may be a device index (from a hand-edited config) or a name.
  const current = typeof v === 'number' ? devices.find(d => d.index === v) : null;
  const selected = current ? current.name : (v ?? '');
  let opts = `<option value="">(system default)</option>`;
  let found = selected === '';
  for (const d of devices) {{
    const isSel = d.name === selected;
    found = found || isSel;
    const label = d.name + (d.default ? ' (current default)' : '');
    opts += `<option value="${{escapeHtml(d.name)}}" ${{isSel ? 'selected' : ''}}>${{escapeHtml(label)}}</option>`;
  }}
  if (!found) {{
    // Configured value doesn't exactly name a device (substring/index, or unplugged) - keep it selectable.
    opts += `<option value="${{escapeHtml(selected)}}" selected>${{escapeHtml(selected)}} (as configured)</option>`;
  }}
  return `<select data-field="${{name}}">${{opts}}</select>`;
}}

function settingsInput(name, field) {{
  const v = field.value;
  if (name in AUDIO_DEVICE_FIELDS) {{
    return deviceSelect(name, AUDIO_DEVICE_FIELDS[name], v);
  }}
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
    try {{
      audioDevices = await api('/api/audio_devices');
    }} catch (e) {{
      audioDevices = {{ input: [], output: [] }};
    }}
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
    document.getElementById('restartBtn').style.display = result.restart_required.length ? 'inline-block' : 'none';
  }} catch (e) {{
    msg.className = 'error';
    msg.textContent = e.message;
  }}
}});

document.getElementById('restartBtn').addEventListener('click', async () => {{
  const msg = document.getElementById('saveMsg');
  const btn = document.getElementById('restartBtn');
  btn.disabled = true;
  msg.className = ''; msg.textContent = 'restarting...';
  try {{
    await api('/api/restart', {{ method: 'POST' }});
  }} catch (e) {{
    // the process re-execs right after responding, so a dropped connection
    // here is expected, not a real failure.
  }}
  setTimeout(() => location.reload(), 3000);
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

document.getElementById('exportConfig').addEventListener('click', async () => {{
  try {{
    const resp = await fetch('/api/config/export', {{ headers: authHeaders() }});
    if (!resp.ok) throw new Error(await resp.text());
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'outpost.yaml';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  }} catch (e) {{
    alert(e.message);
  }}
}});

document.getElementById('importConfig').addEventListener('click', async () => {{
  const msg = document.getElementById('importMsg');
  const fileInput = document.getElementById('importFile');
  const file = fileInput.files[0];
  if (!file) {{ msg.className = 'error'; msg.textContent = 'choose a file first'; return; }}
  if (!confirm('Replace the entire config file with this one? This requires a restart and can lock you out of this page if it changes the web UI host/token.')) return;
  try {{
    const text = await file.text();
    const result = await api('/api/config/import', {{
      method: 'POST', headers: {{ 'Content-Type': 'application/x-yaml' }}, body: text,
    }});
    msg.className = 'ok';
    msg.textContent = result.restart_required ? 'imported - restart required to take effect' : 'imported';
    document.getElementById('restartBtn').style.display = result.restart_required ? 'inline-block' : 'none';
  }} catch (e) {{
    msg.className = 'error'; msg.textContent = e.message;
  }}
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
{_FAVICON_LINK}
{_STYLE}
</head>
<body>
{_BRAND_H1}
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

_PEERS_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outpost · Peers</title>
{_FAVICON_LINK}
{_STYLE}
</head>
<body>
{_BRAND_H1}
{_nav('peers')}

<div class="card">
  <h2>Connection</h2>
  <div class="row"><label>Token (if configured): <input id="token" type="password" size="20"></label></div>
</div>

<div class="card">
  <h2>Static peers</h2>
  <div id="peerList">loading...</div>
  <div class="muted" id="peersMsg"></div>
</div>

<div class="card" id="newPeerCard">
  <h2>Add peer</h2>
  <div class="row">
    <input id="newHost" type="text" size="20" placeholder="host (IP or hostname)">
    <input id="newPort" type="number" size="6" placeholder="port" value="5555">
    <button id="addPeer">Add</button>
  </div>
  <div class="muted">A static peer is kept forever even if quiet - use this for a link mDNS/subnet scan can't reach (e.g. a WireGuard tunnel). Takes effect immediately on this node - no restart needed. Add the mirror entry on the other end too.</div>
</div>

<script>
{_CONNECTION_SCRIPT}

async function loadPeers() {{
  const list = document.getElementById('peerList');
  const msg = document.getElementById('peersMsg');
  try {{
    const data = await api('/api/peers');
    list.textContent = '';
    if (!data.peers.length) {{
      list.textContent = 'no static peers configured';
    }}
    for (const p of data.peers) {{
      const row = document.createElement('div');
      row.className = 'row field-row';
      row.innerHTML = `
        <span class="name">${{p.host}}:${{p.port}}</span>
        <button type="button" class="removeBtn" data-host="${{p.host}}" data-port="${{p.port}}">Remove</button>
      `;
      list.appendChild(row);
    }}
    document.getElementById('addPeer').disabled = !data.can_save;
    if (!data.can_save) {{
      msg.textContent = 'no config file path - peers are read-only here';
    }}
  }} catch (e) {{
    list.textContent = 'error: ' + e.message;
  }}
}}

document.getElementById('peerList').addEventListener('click', async (e) => {{
  if (!e.target.classList.contains('removeBtn')) return;
  const host = e.target.dataset.host;
  const port = Number(e.target.dataset.port);
  const msg = document.getElementById('peersMsg');
  try {{
    await api('/api/peers/remove', {{
      method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ host, port }}),
    }});
    msg.className = 'ok'; msg.textContent = `removed ${{host}}:${{port}}`;
    loadPeers();
  }} catch (err) {{
    msg.className = 'error'; msg.textContent = err.message;
  }}
}});

document.getElementById('addPeer').addEventListener('click', async () => {{
  const host = document.getElementById('newHost').value.trim();
  const port = Number(document.getElementById('newPort').value);
  const msg = document.getElementById('peersMsg');
  if (!host) {{ msg.className = 'error'; msg.textContent = 'host is required'; return; }}
  try {{
    await api('/api/peers', {{
      method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ host, port }}),
    }});
    document.getElementById('newHost').value = '';
    msg.className = ''; msg.textContent = '';
    loadPeers();
  }} catch (err) {{
    msg.className = 'error'; msg.textContent = err.message;
  }}
}});

loadPeers();
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
            web.get("/favicon.svg", self._handle_favicon),
            web.get("/settings", self._handle_settings_page),
            web.get("/channels", self._handle_channels_page),
            web.get("/peers", self._handle_peers_page),
            web.get("/api/status", self._handle_status),
            web.post("/api/channel", self._handle_set_channel),
            web.post("/api/ptt", self._handle_set_ptt),
            web.post("/api/monitor", self._handle_set_monitor),
            web.post("/api/generate_key", self._handle_generate_key),
            web.get("/api/config", self._handle_get_config),
            web.post("/api/config", self._handle_set_config),
            web.get("/api/config/export", self._handle_export_config),
            web.post("/api/config/import", self._handle_import_config),
            web.get("/api/channels", self._handle_get_channels),
            web.post("/api/channels", self._handle_create_channel),
            web.post(r"/api/channels/{channel_id:\d+}/rename", self._handle_rename_channel),
            web.post(r"/api/channels/{channel_id:\d+}/rotate_key", self._handle_rotate_channel_key),
            web.get("/api/peers", self._handle_get_peers),
            web.post("/api/peers", self._handle_add_peer),
            web.post("/api/peers/remove", self._handle_remove_peer),
            web.post("/api/restart", self._handle_restart),
            web.get("/api/audio_devices", self._handle_audio_devices),
        ])

    # Static page shells carry no secrets and need to load without an
    # Authorization header (a plain browser navigation can never send one) -
    # every api/* route stays behind the token below, so viewing live status
    # and every settings/channel/peer/PTT change still requires it.
    _UNAUTHENTICATED_GET_PATHS = frozenset({"/", "/settings", "/channels", "/peers", "/favicon.svg"})

    def _make_auth_middleware(self):
        # A plain closure, not a bound method - aiohttp's `middleware`
        # decorator sets an attribute on the callable, which bound methods
        # don't support.
        async def auth_middleware(request, handler):
            if (
                self.token
                and not (request.method == "GET" and request.path in self._UNAUTHENTICATED_GET_PATHS)
                and request.headers.get("Authorization") != f"Bearer {self.token}"
            ):
                raise web.HTTPUnauthorized(text="missing or invalid token")
            return await handler(request)
        return auth_middleware

    async def _handle_index(self, request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def _handle_favicon(self, request) -> web.Response:
        return web.Response(text=_FAVICON_SVG, content_type="image/svg+xml")

    async def _handle_settings_page(self, request) -> web.Response:
        return web.Response(text=_SETTINGS_HTML, content_type="text/html")

    async def _handle_channels_page(self, request) -> web.Response:
        return web.Response(text=_CHANNELS_HTML, content_type="text/html")

    async def _handle_peers_page(self, request) -> web.Response:
        return web.Response(text=_PEERS_HTML, content_type="text/html")

    async def _handle_status(self, request) -> web.Response:
        c = self.client
        return web.json_response({
            "sender_id": c.config.sender_id,
            "sender_alias": c.sender_alias,
            "active_channel": c.active_channel.name if c.active_channel else None,
            "relay_only": c.relay_only,
            "channels": [ch.name for ch in c.config.channels.values()],
            "ptt_active": c._ptt_active,
            "agc_enabled": c.agc_enabled,
            "agc_target_rms": c.agc.target_rms,
            "relay_enabled": c.enable_relay,
            # "active"/"standby" for a site bridge (see README "Redundant
            # bridges"), else None.
            "bridge_role": c.bridge_role(),
            "highpass_enabled": c.highpass_enabled,
            "squelch_enabled": c.squelch_enabled,
            "squelch_threshold_rms": c.squelch.threshold_rms,
            "tx_lockout_enabled": c.enable_tx_lockout,
            "echo_guard_enabled": c.enable_echo_guard,
            "channel_busy": c.channel_busy(),
            "peer_count": len(c.transport.peers),
            "peers": c.transport.peer_details(),
            "channel_members": c.channel_member_details(),
            "speaking": c.speaking_details(),
            "channel_list": [
                {
                    "name": ch.name,
                    "active": cid == c.active_channel.id,
                    "monitored": cid in c.monitored_channel_ids,
                    "volume": c.channel_volume(cid),
                    "speaking": c.speaking_details(cid) if cid in c.listening_channel_ids() else [],
                }
                for cid, ch in c.config.channels.items()
            ],
            "duck_monitored_channels": c.mixer.duck_enabled,
            "multicast": c.multicast_status(),
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
        return web.json_response({"active_channel": self.client.active_channel.name if self.client.active_channel else None})

    async def _handle_set_monitor(self, request) -> web.Response:
        """{"name": channel, "listen": bool?, "volume": 0.0-1.0?} - listen to
        a channel besides the active one, and/or set its volume. Applies
        live and is saved to the config file when there is one."""
        data = await request.json()
        name = data.get("name") if isinstance(data, dict) else None
        if not name:
            raise web.HTTPBadRequest(text="missing 'name'")
        try:
            if "listen" in data:
                if not isinstance(data["listen"], bool):
                    raise web.HTTPBadRequest(text="'listen' must be true or false")
                self.client.set_monitor(name, data["listen"])
            if "volume" in data:
                try:
                    volume = _validate_volume(data["volume"])
                except (TypeError, ValueError) as exc:
                    raise web.HTTPBadRequest(text=f"volume: {exc}")
                self.client.set_channel_volume(name, volume)
        except KeyError:
            raise web.HTTPNotFound(text=f"no channel named {name!r}")
        if self.client.config_path is not None:
            save_config_fields(self.client.config_path, {
                "monitor_channels": self.client.config.monitor_channels,
                "channel_volumes": self.client.config.channel_volumes,
            })
        return web.json_response({
            "monitor_channels": self.client.config.monitor_channels,
            "channel_volumes": self.client.config.channel_volumes,
        })

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
        if self.client.relay_only:
            raise web.HTTPBadRequest(text="a relay-only node holds no channel keys")
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

    async def _handle_get_peers(self, request) -> web.Response:
        peers = [{"host": p.host, "port": p.port} for p in self.client.config.peers]
        return web.json_response({"peers": peers, "can_save": self.client.config_path is not None})

    async def _handle_add_peer(self, request) -> web.Response:
        if self.client.config_path is None:
            raise web.HTTPBadRequest(text="this client wasn't started from a config file - nothing to save to")
        data = await request.json()
        host = (data.get("host") or "").strip()
        if not host:
            raise web.HTTPBadRequest(text="missing 'host'")
        try:
            port = int(data.get("port"))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="'port' must be an integer")
        if not (1 <= port <= 65535):
            raise web.HTTPBadRequest(text="'port' must be between 1 and 65535")
        if any(p.host == host and p.port == port for p in self.client.config.peers):
            raise web.HTTPBadRequest(text=f"{host}:{port} is already a peer")

        add_peer_to_file(self.client.config_path, host, port)
        self.client.add_peer(host, port)
        return web.json_response({"host": host, "port": port})

    async def _handle_remove_peer(self, request) -> web.Response:
        if self.client.config_path is None:
            raise web.HTTPBadRequest(text="this client wasn't started from a config file - nothing to save to")
        data = await request.json()
        host = (data.get("host") or "").strip()
        try:
            port = int(data.get("port"))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="'port' must be an integer")

        try:
            remove_peer_from_file(self.client.config_path, host, port)
        except KeyError:
            raise web.HTTPNotFound(text=f"no peer {host}:{port} in config")
        self.client.remove_peer(host, port)
        return web.json_response({"host": host, "port": port})

    async def _handle_get_config(self, request) -> web.Response:
        values = editable_config_values(self.client.config)
        fields = {
            name: {"value": values[name], "live": live}
            for name, (_validator, live) in EDITABLE_FIELDS.items()
        }
        return web.json_response({"fields": fields, "can_save": self.client.config_path is not None})

    async def _handle_audio_devices(self, request) -> web.Response:
        """Mic/speaker devices on this machine, for the Settings page's
        input_device/output_device pickers."""
        from .audio import list_devices_fresh
        try:
            return web.json_response(await list_devices_fresh())
        except Exception as exc:  # PortAudio errors shouldn't break the settings page
            log.warning("could not list audio devices: %s", exc)
            return web.json_response({"input": [], "output": [], "error": str(exc)})

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
        if self.client.relay_only and validated.get("enable_relay") is False:
            raise web.HTTPBadRequest(text="enable_relay: always on for a relay-only node")

        save_config_fields(self.client.config_path, validated)
        # OutpostConfig is frozen, and the fields below are kept live via
        # separate mutable mirror attributes (self.client.agc_enabled etc.)
        # rather than by mutating config in place - replace() keeps
        # self.client.config itself (read by _handle_get_config) in sync so
        # the Settings page doesn't show stale values on next load.
        self.client.config = replace(self.client.config, **validated)

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
        if "enable_echo_guard" in validated:
            self.client.enable_echo_guard = validated["enable_echo_guard"]
        if "echo_guard_hold_ms" in validated:
            self.client.echo_guard_hold_s = validated["echo_guard_hold_ms"] / 1000.0
        if "sender_alias" in validated:
            self.client.sender_alias = validated["sender_alias"]
        if "multicast_min_listeners" in validated:
            self.client.multicast_min_listeners = validated["multicast_min_listeners"]
        if "duck_monitored_channels" in validated:
            self.client.mixer.duck_enabled = validated["duck_monitored_channels"]

        return web.json_response({
            "saved": sorted(validated),
            "applied_live": applied_live,
            "restart_required": restart_required,
        })

    async def _handle_export_config(self, request) -> web.Response:
        if self.client.config_path is None:
            raise web.HTTPBadRequest(text="this client wasn't started from a config file - nothing to export")
        text = export_config_text(self.client.config_path)
        return web.Response(
            text=text, content_type="application/x-yaml",
            headers={"Content-Disposition": 'attachment; filename="outpost.yaml"'},
        )

    async def _handle_import_config(self, request) -> web.Response:
        if self.client.config_path is None:
            raise web.HTTPBadRequest(text="this client wasn't started from a config file - nothing to import into")
        data = await request.read()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise web.HTTPBadRequest(text="uploaded file is not valid UTF-8 text")
        try:
            validate_config_yaml(text)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=f"invalid config: {exc}")
        # Whole-file import can change sender_id/mesh_key/channels/web_ui_*
        # - never applied live, same as those fields' individual edits would
        # need to be; only takes effect after a restart (see /api/restart).
        write_config_text(self.client.config_path, text)
        return web.json_response({"imported": True, "restart_required": True})

    async def _handle_restart(self, request) -> web.Response:
        # Responds before actually restarting - scripts/run_client.py's main
        # loop is what performs the clean stop + re-exec, driven by this
        # Event, and the HTTP response wouldn't be able to complete once
        # that's underway anyway.
        self.client.request_restart()
        return web.json_response({"restarting": True})

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
