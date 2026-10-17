"""Tests for config loading, in particular the zero-coordination sender_id."""
from __future__ import annotations
import base64

from outpost.config import load_config, DEFAULT_LISTEN_PORT
from outpost.crypto import generate_channel_key

_MESH_KEY = base64.b64encode(generate_channel_key()).decode()
_CHANNEL_KEY = base64.b64encode(generate_channel_key()).decode()


def _write_minimal_config(path, extra: str = "") -> None:
    path.write_text(
        f"""
mesh_key: "{_MESH_KEY}"
channels:
  - id: 1
    name: ops
    key: "{_CHANNEL_KEY}"
{extra}
"""
    )


def test_sender_id_and_listen_port_default_when_omitted(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path)

    config = load_config(str(config_path))

    assert config.listen_port == DEFAULT_LISTEN_PORT
    assert isinstance(config.sender_id, int)
    assert 0 <= config.sender_id < 2**32


def test_auto_generated_sender_id_is_stable_across_reloads(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path)

    first = load_config(str(config_path))
    second = load_config(str(config_path))

    assert first.sender_id == second.sender_id
    assert (tmp_path / "outpost.yaml.identity").exists()


def test_explicit_sender_id_is_respected_and_not_overwritten(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path, extra="sender_id: 42\nlisten_port: 6000\n")

    config = load_config(str(config_path))

    assert config.sender_id == 42
    assert config.listen_port == 6000
    assert not (tmp_path / "outpost.yaml.identity").exists()
