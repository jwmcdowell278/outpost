FROM python:3.11-slim-bookworm

# Runtime libraries Outpost's deps load directly:
# - libopus0: opuslib is a ctypes wrapper, dlopens this at runtime (no compile step)
# - libportaudio2/libasound2: sounddevice's audio I/O backend on Linux
# - libffi8: PyNaCl's cffi bindings
# - libhidapi-hidraw0: hidapi (USB HID PTT, `[hardware]` extra)
# Plus build-essential/portaudio19-dev/python3-dev: some architectures (e.g.
# 32-bit armv7 Raspberry Pi OS) don't have a prebuilt sounddevice/numpy wheel
# on PyPI, so pip falls back to building from source.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libopus0 \
        libportaudio2 \
        libasound2 \
        libffi8 \
        libhidapi-hidraw0 \
        portaudio19-dev \
        build-essential \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY outpost ./outpost
COPY scripts ./scripts

# Install the hardware extras too (hidapi/pyserial/gpiozero) - harmless if
# unused, and means one image works for any --ptt source.
RUN pip install --no-cache-dir -e '.[hardware]'

# Config (with its real channel/mesh keys) is deliberately NOT baked into the
# image - mount it at runtime instead, e.g. `-v $(pwd)/config:/config`.
VOLUME ["/config"]

# listen_port (peer UDP traffic) and web_ui_port (optional web UI) defaults -
# see config/outpost.example.yaml. Irrelevant if you run with --network host
# (recommended - see README's "Running with Docker" section for why).
EXPOSE 5555/udp 8765/tcp

ENTRYPOINT ["python", "scripts/run_client.py"]
CMD ["--help"]
