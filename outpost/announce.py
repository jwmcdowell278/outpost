"""Optional local text-to-speech announcement of the active channel's name
when it changes (see OutpostClient.set_active_channel) - a hands-free
convenience for operators who can't glance at a screen, not an
authenticated signal of anything. Uses pyttsx3; on Linux this also needs
the system 'espeak' package installed (apt install espeak), since pyttsx3
itself has no bundled voice. Degrades to a silent no-op if either isn't
available, so nodes that leave enable_channel_announce off are entirely
unaffected."""
from __future__ import annotations
import logging
import queue
import threading

log = logging.getLogger(__name__)


class ChannelAnnouncer:
    """Owns a single background thread and TTS engine for its lifetime -
    pyttsx3 engines aren't safe to drive from more than one thread, so every
    announcement is queued to this one thread rather than spawning a fresh
    thread/engine per call."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._unavailable = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def announce(self, text: str) -> None:
        """Queues `text` to be spoken - never blocks the caller (e.g. the
        asyncio event loop handling a web UI channel-switch request)."""
        if not self._unavailable:
            self._queue.put(text)

    def _run(self) -> None:
        try:
            import pyttsx3
            engine = pyttsx3.init()
        except Exception:
            log.warning(
                "enable_channel_announce is on but text-to-speech isn't available "
                "(is 'espeak' installed? pip show pyttsx3) - channel announcements disabled",
                exc_info=True,
            )
            self._unavailable = True
            return
        while True:
            text = self._queue.get()
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception:
                log.warning("channel announce (text-to-speech) failed", exc_info=True)
