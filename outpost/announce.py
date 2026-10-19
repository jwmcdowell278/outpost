"""Optional local text-to-speech announcement of the active channel's name
when it changes (see OutpostClient.set_active_channel) - a hands-free
convenience for operators who can't glance at a screen, not an
authenticated signal of anything. Uses pyttsx3; on Linux this also needs
the system 'espeak' package installed (apt install espeak), since pyttsx3
itself has no bundled voice. Degrades to a silent no-op if either isn't
available, so nodes that leave enable_channel_announce off are entirely
unaffected. Run `python -m outpost.announce` to list installed voices for
config's channel_announce_voice."""
from __future__ import annotations
import gc
import logging
import queue
import threading

log = logging.getLogger(__name__)


def _find_voice_id(engine, match: str) -> str | None:
    """id of the first installed voice whose id/name/language contains
    `match` (case-insensitive substring), or None if nothing matches - see
    README for how to list voices available on a given machine."""
    needle = match.lower()
    for voice in engine.getProperty("voices"):
        languages = " ".join(getattr(voice, "languages", None) or [])
        if needle in f"{voice.id} {voice.name} {languages}".lower():
            return voice.id
    return None


class ChannelAnnouncer:
    """Owns a single background thread for its lifetime, so at most one
    announcement is ever being spoken at a time - but deliberately creates a
    brand-new pyttsx3 engine for *every* announcement rather than reusing
    one: on macOS (nsss driver), re-running say()/runAndWait() on the same
    engine instance becomes unreliable after the first call and degrades
    further with each subsequent one (seen live as the 1st/2nd channel
    switch sometimes staying silent and later ones never working at all).

    A fresh engine per call isn't enough on its own, though: pyttsx3.init()
    caches engines in a module-level WeakValueDictionary keyed by driver
    name, so unless the *previous* engine has actually been garbage
    collected by the time the next call runs, init() hands back that same
    stale engine instead of a genuinely new one - reference cycles held by
    the native speech driver mean plain refcounting doesn't always free it
    immediately, so this manifested as intermittent silence (works, then
    doesn't, with no obvious pattern). Forcing gc.collect() right after
    stop() guarantees the cache entry is cleared before the next init()."""

    def __init__(self, voice: str | None = None) -> None:
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._unavailable = False
        self._voice_id: str | None = None
        try:
            import pyttsx3
            if voice:
                # Resolved once up front (not per-announcement) since the
                # installed voice list doesn't change at runtime.
                probe = pyttsx3.init()
                self._voice_id = _find_voice_id(probe, voice)
                probe.stop()
                del probe
                gc.collect()
                if self._voice_id is None:
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
        import pyttsx3
        while True:
            text = self._queue.get()
            try:
                engine = pyttsx3.init()
                if self._voice_id is not None:
                    engine.setProperty("voice", self._voice_id)
                engine.say(text)
                engine.runAndWait()
                engine.stop()
                del engine
                gc.collect()
            except Exception:
                log.warning("channel announce (text-to-speech) failed", exc_info=True)


def _list_voices() -> None:
    import pyttsx3
    engine = pyttsx3.init()
    for voice in engine.getProperty("voices"):
        print(f"{voice.id}  |  {voice.name}  |  {getattr(voice, 'languages', None)}")
    engine.stop()


if __name__ == "__main__":
    # python -m outpost.announce - lists installed TTS voices (id/name/
    # language) so you can pick a value for config's channel_announce_voice.
    _list_voices()
