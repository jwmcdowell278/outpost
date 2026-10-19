"""Optional local text-to-speech announcement of the active channel's name
when it changes (see OutpostClient.set_active_channel) - a hands-free
convenience for operators who can't glance at a screen, not an
authenticated signal of anything. On macOS, shells out to the built-in
`say` command: a fresh one-shot process per announcement with no engine
state to leak or cache, which sidesteps pyttsx3's nsss-driver reliability
problems entirely (see ChannelAnnouncer docstring). Elsewhere, falls back
to pyttsx3, which on Linux also needs the system 'espeak' package
installed (apt install espeak) since pyttsx3 itself has no bundled voice.
Degrades to a silent no-op if neither is available, so nodes that leave
enable_channel_announce off are entirely unaffected. Run
`python -m outpost.announce` to list installed voices for config's
channel_announce_voice."""
from __future__ import annotations
import gc
import logging
import queue
import shutil
import subprocess
import sys
import threading

log = logging.getLogger(__name__)

_HAS_SAY = sys.platform == "darwin" and shutil.which("say") is not None


def _find_voice_id(engine, match: str) -> str | None:
    """id of the first installed pyttsx3 voice whose id/name/language
    contains `match` (case-insensitive substring), or None if nothing
    matches - see README for how to list voices available on a given
    machine."""
    needle = match.lower()
    for voice in engine.getProperty("voices"):
        languages = " ".join(getattr(voice, "languages", None) or [])
        if needle in f"{voice.id} {voice.name} {languages}".lower():
            return voice.id
    return None


def _list_say_voices() -> list[str]:
    """Raw `say -v '?'` output, one line per installed macOS voice (name,
    language code, sample text)."""
    result = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True)
    return result.stdout.splitlines()


def _resolve_say_voice(lines: list[str], match: str) -> str | None:
    """Name of the first `say -v '?'` listing line (see _list_say_voices)
    that contains `match` (case-insensitive substring: name, language code
    or sample text all count), or None if nothing matches."""
    needle = match.lower()
    for line in lines:
        if needle in line.lower():
            return line.split()[0]
    return None


class ChannelAnnouncer:
    """Owns a single background thread for its lifetime, so at most one
    announcement is ever being spoken at a time.

    On macOS, each announcement shells out to the `say` command as a brand
    new process - this was chosen over driving pyttsx3's nsss driver
    in-process, which proved unreliable in two different ways even after
    fixing each: (1) reusing one pyttsx3 Engine instance across calls
    degrades after the first call and stops working entirely after a few
    more; (2) even creating a fresh Engine per call isn't enough on its
    own, because pyttsx3.init() caches engines in a module-level
    WeakValueDictionary keyed by driver name, and the native nsss driver
    holds reference cycles that plain refcounting doesn't free
    immediately - so init() can still hand back a stale, already-broken
    engine depending on GC timing, forcing gc.collect() after every
    stop() to compensate. Both were attempts to patch over the same
    underlying fragility of keeping an nsss Engine alive in-process; a
    fresh `say` subprocess per announcement has no engine state to leak or
    cache in the first place, so it sidesteps the whole problem.

    Elsewhere (no `say` binary, e.g. Linux), falls back to the same
    fresh-pyttsx3-engine-plus-gc.collect() approach."""

    def __init__(self, voice: str | None = None) -> None:
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._unavailable = False
        self._voice: str | None = None
        self._use_say = _HAS_SAY
        try:
            if self._use_say:
                if voice:
                    # Resolved once up front (not per-announcement) since the
                    # installed voice list doesn't change at runtime.
                    self._voice = _resolve_say_voice(_list_say_voices(), voice)
                    if self._voice is None:
                        log.warning("channel_announce_voice %r matched no installed `say` voice - using the system default", voice)
            else:
                import pyttsx3
                if voice:
                    probe = pyttsx3.init()
                    self._voice = _find_voice_id(probe, voice)
                    probe.stop()
                    del probe
                    gc.collect()
                    if self._voice is None:
                        log.warning("channel_announce_voice %r matched no installed TTS voice - using the system default", voice)
        except Exception:
            log.warning(
                "enable_channel_announce is on but text-to-speech isn't available "
                "(is 'espeak' installed? pip show pyttsx3) - channel announcements disabled",
                exc_info=True,
            )
            self._unavailable = True
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def announce(self, text: str) -> None:
        """Queues `text` to be spoken - never blocks the caller (e.g. the
        asyncio event loop handling a web UI channel-switch request)."""
        if not self._unavailable:
            self._queue.put(text)

    def _run(self) -> None:
        if not self._use_say:
            import pyttsx3
        while True:
            text = self._queue.get()
            try:
                if self._use_say:
                    cmd = ["say"] + (["-v", self._voice] if self._voice else []) + [text]
                    subprocess.run(cmd, check=False)
                else:
                    engine = pyttsx3.init()
                    if self._voice is not None:
                        engine.setProperty("voice", self._voice)
                    engine.say(text)
                    engine.runAndWait()
                    engine.stop()
                    del engine
                    gc.collect()
            except Exception:
                log.warning("channel announce (text-to-speech) failed", exc_info=True)


def _list_voices() -> None:
    if _HAS_SAY:
        for line in _list_say_voices():
            print(line)
        return
    import pyttsx3
    engine = pyttsx3.init()
    for voice in engine.getProperty("voices"):
        print(f"{voice.id}  |  {voice.name}  |  {getattr(voice, 'languages', None)}")
    engine.stop()


if __name__ == "__main__":
    # python -m outpost.announce - lists installed TTS voices (id/name/
    # language) so you can pick a value for config's channel_announce_voice.
    _list_voices()
