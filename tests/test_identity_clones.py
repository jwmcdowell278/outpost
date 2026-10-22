"""Cloned-node protection: an auto-generated sender_id that turns up on
different hardware is replaced (outpost/identity.py, config._resolve_sender_id),
and a node logs an error if it hears another node using its sender_id."""
from __future__ import annotations
import base64
import logging
import pytest

from outpost import config as config_mod, identity
from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel, load_config
from outpost.crypto import ChannelCipher, generate_channel_key
from outpost.protocol import Header, VERSION, TYPE_CHANNEL_ANNOUNCE


def _write_config(path, extra=""):
    path.write_text(f"""
mesh_key: "{base64.b64encode(generate_channel_key()).decode()}"
channels:
  - id: 1
    name: ops
    key: "{base64.b64encode(generate_channel_key()).decode()}"
{extra}
""")


@pytest.fixture
def hw(monkeypatch):
    state = {"ids": {"aaaa"}}
    monkeypatch.setattr(config_mod, "hardware_ids", lambda: set(state["ids"]))
    return state


def test_same_hardware_keeps_sender_id(tmp_path, hw):
    path = tmp_path / "outpost.yaml"
    _write_config(path)
    assert load_config(str(path)).sender_id == load_config(str(path)).sender_id


def test_cloned_to_other_hardware_gets_new_sender_id(tmp_path, hw, caplog):
    path = tmp_path / "outpost.yaml"
    _write_config(path)
    original = load_config(str(path)).sender_id

    hw["ids"] = {"bbbb"}  # same files, different machine
    with caplog.at_level(logging.WARNING):
        cloned = load_config(str(path)).sender_id
    assert cloned != original
    assert "different hardware" in caplog.text
    # ...and it's stable on the new machine from then on.
    assert load_config(str(path)).sender_id == cloned


def test_added_hardware_does_not_change_sender_id(tmp_path, hw):
    path = tmp_path / "outpost.yaml"
    _write_config(path)
    original = load_config(str(path)).sender_id
    hw["ids"] = {"aaaa", "dongle"}
    assert load_config(str(path)).sender_id == original


def test_old_identity_file_is_adopted_and_fingerprinted(tmp_path, hw):
    path = tmp_path / "outpost.yaml"
    _write_config(path)
    (tmp_path / "outpost.yaml.identity").write_text("12345")
    assert load_config(str(path)).sender_id == 12345
    assert "hardware:" in (tmp_path / "outpost.yaml.identity").read_text()


def test_unreadable_hardware_skips_the_check(tmp_path, hw):
    path = tmp_path / "outpost.yaml"
    _write_config(path)
    original = load_config(str(path)).sender_id
    hw["ids"] = set()
    assert load_config(str(path)).sender_id == original


def test_explicit_sender_id_is_never_replaced(tmp_path, hw):
    path = tmp_path / "outpost.yaml"
    _write_config(path, "sender_id: 42\n")
    hw["ids"] = {"zzzz"}
    assert load_config(str(path)).sender_id == 42


def test_linux_ids_use_only_physical_nics(tmp_path):
    for name, mac, physical in [("eth0", "aa:bb:cc:00:00:01", True), ("wlan0", "aa:bb:cc:00:00:02", True),
                                ("docker0", "02:42:00:00:00:01", False), ("lo", "00:00:00:00:00:00", False)]:
        d = tmp_path / name
        d.mkdir()
        (d / "address").write_text(mac + "\n")
        if physical:
            (d / "device").mkdir()
    assert identity._linux_ids(str(tmp_path)) == {"aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"}


# --- runtime clash detection -------------------------------------------

def _client_and_cipher():
    key = generate_channel_key()
    client = OutpostClient(OutpostConfig(
        sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
        channels={1: Channel(1, "ops", key)}, enable_mdns=False, enable_pex=False,
    ), "ops")
    return client, ChannelCipher(key)


def _announce(cipher, token, seq=500):
    header = Header(VERSION, TYPE_CHANNEL_ANNOUNCE, 1, 1, seq)
    return header, cipher.encrypt(header, token)


def test_another_node_with_our_sender_id_logs_an_error(caplog):
    client, cipher = _client_and_cipher()
    with caplog.at_level(logging.ERROR):
        client._on_packet(*_announce(cipher, b"othernod"), ("10.0.0.9", 5555))
        client._on_packet(*_announce(cipher, b"othernod", 501), ("10.0.0.9", 5555))
    assert caplog.text.count("using this node's sender_id") == 1  # rate-limited


def test_our_own_announce_looped_back_is_not_a_clash(caplog):
    client, cipher = _client_and_cipher()
    with caplog.at_level(logging.ERROR):
        client._on_packet(*_announce(cipher, client._instance_token), ("10.0.0.3", 5555))
    assert "sender_id" not in caplog.text


def test_forged_announce_is_not_reported():
    client, _ = _client_and_cipher()
    header = Header(VERSION, TYPE_CHANNEL_ANNOUNCE, 1, 1, 5)
    client._on_packet(header, b"\x00" * 40, ("10.0.0.66", 5555))
    assert client._clash_warned_at < 0
