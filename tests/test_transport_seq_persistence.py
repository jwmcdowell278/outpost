"""Verifies MeshTransport persists its seq high-water mark across restarts.

The encryption nonce is derived from (sender_id, seq) - see crypto.py - which
is only safe as long as a (channel key, nonce) pair never repeats. sender_id
is persisted across restarts (config._resolve_sender_id) specifically to
avoid nonce collisions, but a seq counter that reset to 0 on every restart
would defeat that: it would replay the exact nonces used in the previous
run under the same channel key, and separately it would make every peer's
stale/duplicate check (client.py's `_last_seq_seen`) silently drop this
node's post-restart audio as old duplicates of its own higher pre-restart
seq, which looks like "this node intermittently stops being heard"."""
from __future__ import annotations

from outpost.transport import MeshTransport, _SEQ_PERSIST_MARGIN, _SEQ_PERSIST_EVERY


def _make_transport(seq_persist_path=None) -> MeshTransport:
    return MeshTransport(sender_id=1, peers=[], on_packet=lambda *a: None, seq_persist_path=seq_persist_path)


def test_seq_starts_at_zero_with_no_persist_path():
    transport = _make_transport()
    assert transport.next_seq() == 0
    assert transport.next_seq() == 1


def test_seq_starts_at_zero_on_first_run_with_persist_path(tmp_path):
    seq_path = str(tmp_path / "outpost.yaml.seq")
    transport = _make_transport(seq_persist_path=seq_path)
    assert transport.next_seq() == 0


def test_stop_flushes_seq_high_water_mark(tmp_path):
    seq_path = str(tmp_path / "outpost.yaml.seq")
    transport = _make_transport(seq_persist_path=seq_path)
    for _ in range(10):
        transport.next_seq()
    transport.stop()

    with open(seq_path) as f:
        assert int(f.read().strip()) == 10


def test_restart_resumes_well_above_prior_high_water_mark_not_at_zero(tmp_path):
    """The core regression: a restarted node must never hand out a seq that
    overlaps (or falls below) what it already used last run, otherwise it
    reuses a nonce under the same channel key and/or gets treated as a
    stale duplicate sender by every peer still remembering its old,
    higher seq."""
    seq_path = str(tmp_path / "outpost.yaml.seq")

    first_run = _make_transport(seq_persist_path=seq_path)
    for _ in range(500):
        first_run.next_seq()
    last_seq_used = first_run.next_seq()  # 500
    first_run.stop()

    second_run = _make_transport(seq_persist_path=seq_path)
    resumed_seq = second_run.next_seq()

    assert resumed_seq > last_seq_used
    assert resumed_seq >= last_seq_used + _SEQ_PERSIST_MARGIN


def test_seq_is_periodically_flushed_without_explicit_stop(tmp_path):
    """An unclean shutdown (crash/power loss) never calls stop(), so the
    periodic in-band flush (every _SEQ_PERSIST_EVERY calls) is what bounds
    how far a restart has to jump - verify it actually writes without a
    clean stop()."""
    seq_path = str(tmp_path / "outpost.yaml.seq")
    transport = _make_transport(seq_persist_path=seq_path)

    for _ in range(_SEQ_PERSIST_EVERY):
        transport.next_seq()

    with open(seq_path) as f:
        persisted = int(f.read().strip())
    assert persisted > 0


def test_corrupt_or_missing_persist_file_falls_back_to_zero(tmp_path):
    seq_path = str(tmp_path / "outpost.yaml.seq")
    with open(seq_path, "w") as f:
        f.write("not-a-number")

    transport = _make_transport(seq_persist_path=seq_path)
    assert transport.next_seq() == 0
