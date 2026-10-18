"""Tests for config loading, in particular the zero-coordination sender_id."""
from __future__ import annotations
import base64
import pytest
import yaml

from outpost.config import (
    DEFAULT_LISTEN_PORT, EDITABLE_FIELDS, OutpostConfig, editable_config_values,
    load_config, save_config_fields,
    add_channel_to_file, rename_channel_in_file, rotate_channel_key_in_file,
    add_peer_to_file, remove_peer_from_file,
    export_config_text, validate_config_yaml, write_config_text,
)
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


def test_highpass_and_squelch_fields_are_loaded_from_yaml(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(
        config_path,
        extra=(
            "enable_highpass: false\n"
            "highpass_cutoff_hz: 150\n"
            "enable_squelch: true\n"
            "squelch_threshold_rms: 450\n"
            "squelch_hold_ms: 250\n"
        ),
    )

    config = load_config(str(config_path))

    assert config.enable_highpass is False
    assert config.highpass_cutoff_hz == 150
    assert config.enable_squelch is True
    assert config.squelch_threshold_rms == 450
    assert config.squelch_hold_ms == 250


def test_subnet_scan_fields_are_loaded_from_yaml(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(
        config_path,
        extra=(
            "enable_subnet_scan: true\n"
            "subnet_scan_interval_s: 45\n"
            "subnet_scan_max_hosts: 256\n"
        ),
    )

    config = load_config(str(config_path))

    assert config.enable_subnet_scan is True
    assert config.subnet_scan_interval_s == 45
    assert config.subnet_scan_max_hosts == 256


def test_sender_alias_defaults_to_none_when_omitted(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path)

    config = load_config(str(config_path))

    assert config.sender_alias is None


def test_sender_alias_is_loaded_from_yaml(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path, extra='sender_alias: "Basecamp"\n')

    config = load_config(str(config_path))

    assert config.sender_alias == "Basecamp"


def test_validate_alias_strips_whitespace_and_rejects_too_long():
    validator, _live = EDITABLE_FIELDS["sender_alias"]
    assert validator(None) is None
    assert validator("  Basecamp  ") == "Basecamp"
    assert validator("   ") is None  # blank clears to None
    with pytest.raises(ValueError):
        validator("x" * 33)
    with pytest.raises(ValueError):
        validator(123)


def test_log_file_defaults_to_none_when_omitted(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path)

    config = load_config(str(config_path))

    assert config.log_file is None
    assert config.log_max_bytes == 10_000_000
    assert config.log_backup_count == 5


def test_log_file_settings_are_loaded_from_yaml(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(
        config_path,
        extra='log_file: "/var/log/outpost/outpost.log"\nlog_max_bytes: 500000\nlog_backup_count: 2\n',
    )

    config = load_config(str(config_path))

    assert config.log_file == "/var/log/outpost/outpost.log"
    assert config.log_max_bytes == 500000
    assert config.log_backup_count == 2


def test_log_file_settings_are_not_editable_via_web_ui():
    # A filesystem path must never be editable through the web UI's config
    # endpoint - that would be an arbitrary-file-write primitive for anyone
    # holding (or guessing) web_ui_token.
    assert "log_file" not in EDITABLE_FIELDS
    assert "log_max_bytes" not in EDITABLE_FIELDS
    assert "log_backup_count" not in EDITABLE_FIELDS

    cfg = OutpostConfig(sender_id=1, listen_port=5555, mesh_key=generate_channel_key())
    values = editable_config_values(cfg)
    assert "log_file" not in values
    assert "log_max_bytes" not in values
    assert "log_backup_count" not in values


def test_editable_config_values_excludes_secrets_and_identity():
    cfg = OutpostConfig(sender_id=1, listen_port=5555, mesh_key=generate_channel_key())
    values = editable_config_values(cfg)

    assert "bitrate" in values
    assert "mesh_key" not in values
    assert "sender_id" not in values
    assert "channels" not in values
    assert "web_ui_token" not in values
    assert "enable_highpass" in values
    assert "highpass_cutoff_hz" in values
    assert "enable_squelch" in values
    assert "squelch_threshold_rms" in values
    assert "squelch_hold_ms" in values
    assert "sender_alias" in values


@pytest.mark.parametrize("field,bad_value", [
    ("bitrate", -1),
    ("listen_port", 70000),
    ("pex_interval_s", 0),
    ("enable_agc", "yes"),
    ("highpass_cutoff_hz", -1),
    ("squelch_threshold_rms", -1),
    ("squelch_hold_ms", -1),
])
def test_editable_field_validators_reject_bad_values(field, bad_value):
    validator, _live = EDITABLE_FIELDS[field]
    with pytest.raises(ValueError):
        validator(bad_value)


def test_squelch_threshold_and_hold_accept_zero():
    # Zero is a valid (if degenerate) value - "always open" gate / no hold.
    threshold_validator, _ = EDITABLE_FIELDS["squelch_threshold_rms"]
    hold_validator, _ = EDITABLE_FIELDS["squelch_hold_ms"]
    assert threshold_validator(0) == 0.0
    assert hold_validator(0) == 0.0


def test_save_config_fields_updates_only_given_keys(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    config_path.write_text("mesh_key: abc\nbitrate: 16000\nchannels: []\n")

    save_config_fields(str(config_path), {"bitrate": 8000, "enable_agc": False})

    raw = yaml.safe_load(config_path.read_text())
    assert raw["bitrate"] == 8000
    assert raw["enable_agc"] is False
    assert raw["mesh_key"] == "abc"  # untouched
    assert raw["channels"] == []  # untouched


def test_add_channel_to_file_appends_entry(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path)
    new_key = generate_channel_key()

    add_channel_to_file(str(config_path), 2, "tac", new_key)

    raw = yaml.safe_load(config_path.read_text())
    assert raw["channels"][0]["name"] == "ops"  # untouched
    assert raw["channels"][1] == {
        "id": 2, "name": "tac", "key": base64.b64encode(new_key).decode(),
    }


def test_rename_channel_in_file_updates_only_matching_id(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path)

    rename_channel_in_file(str(config_path), 1, "renamed")

    raw = yaml.safe_load(config_path.read_text())
    assert raw["channels"][0]["name"] == "renamed"
    assert raw["channels"][0]["key"] == _CHANNEL_KEY  # key untouched


def test_rename_channel_in_file_raises_for_unknown_id(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path)

    with pytest.raises(KeyError):
        rename_channel_in_file(str(config_path), 99, "nope")


def test_rotate_channel_key_in_file_replaces_key_only(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path)
    new_key = generate_channel_key()

    rotate_channel_key_in_file(str(config_path), 1, new_key)

    raw = yaml.safe_load(config_path.read_text())
    assert raw["channels"][0]["name"] == "ops"  # untouched
    assert raw["channels"][0]["key"] == base64.b64encode(new_key).decode()


def test_add_peer_to_file_appends_entry(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path, extra="peers:\n  - host: 10.0.0.1\n    port: 5555\n")

    add_peer_to_file(str(config_path), "10.0.0.2", 6000)

    raw = yaml.safe_load(config_path.read_text())
    assert raw["peers"] == [
        {"host": "10.0.0.1", "port": 5555},
        {"host": "10.0.0.2", "port": 6000},
    ]


def test_remove_peer_from_file_removes_matching_entry_only(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(
        config_path,
        extra="peers:\n  - host: 10.0.0.1\n    port: 5555\n  - host: 10.0.0.2\n    port: 6000\n",
    )

    remove_peer_from_file(str(config_path), "10.0.0.1", 5555)

    raw = yaml.safe_load(config_path.read_text())
    assert raw["peers"] == [{"host": "10.0.0.2", "port": 6000}]


def test_remove_peer_from_file_raises_for_unknown_address(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path, extra="peers:\n  - host: 10.0.0.1\n    port: 5555\n")

    with pytest.raises(KeyError):
        remove_peer_from_file(str(config_path), "10.0.0.9", 5555)


def test_export_config_text_returns_file_verbatim(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path, extra="# a comment\nbitrate: 8000\n")

    assert export_config_text(str(config_path)) == config_path.read_text()


def test_validate_config_yaml_accepts_a_well_formed_config(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path)
    validate_config_yaml(config_path.read_text())  # must not raise


def test_validate_config_yaml_rejects_non_yaml():
    with pytest.raises(ValueError, match="not valid YAML"):
        validate_config_yaml("not: valid: yaml: at: all: [")


def test_validate_config_yaml_rejects_non_mapping_top_level():
    with pytest.raises(ValueError, match="mapping"):
        validate_config_yaml("- just\n- a\n- list\n")


def test_validate_config_yaml_rejects_missing_mesh_key():
    with pytest.raises(ValueError, match="mesh_key"):
        validate_config_yaml("channels:\n  - id: 1\n    name: ops\n    key: " + _CHANNEL_KEY + "\n")


def test_validate_config_yaml_rejects_malformed_mesh_key():
    with pytest.raises(ValueError, match="mesh_key"):
        validate_config_yaml(
            "mesh_key: not-valid-base64!!!\nchannels:\n  - id: 1\n    name: ops\n    key: "
            + _CHANNEL_KEY + "\n"
        )


def test_validate_config_yaml_rejects_missing_channels():
    with pytest.raises(ValueError, match="channels"):
        validate_config_yaml(f'mesh_key: "{_MESH_KEY}"\n')


def test_validate_config_yaml_rejects_malformed_channel_entry():
    with pytest.raises(ValueError, match="channels entry"):
        validate_config_yaml(f'mesh_key: "{_MESH_KEY}"\nchannels:\n  - id: 1\n    name: ops\n')


def test_validate_config_yaml_rejects_malformed_peer_entry():
    with pytest.raises(ValueError, match="peers entry"):
        validate_config_yaml(
            f'mesh_key: "{_MESH_KEY}"\nchannels:\n  - id: 1\n    name: ops\n    key: "{_CHANNEL_KEY}"\n'
            'peers:\n  - host: 10.0.0.1\n'
        )


def test_write_config_text_overwrites_file_verbatim(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path)

    new_text = f'mesh_key: "{_MESH_KEY}"\nchannels:\n  - id: 1\n    name: renamed\n    key: "{_CHANNEL_KEY}"\n'
    write_config_text(str(config_path), new_text)

    assert config_path.read_text() == new_text
    assert not (tmp_path / "outpost.yaml.tmp").exists()

