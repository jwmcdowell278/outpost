"""Generate a fresh base64-encoded channel key for config/outpost.yaml.

Run with the outpost package installed (pip install -e .):
    python scripts/gen_channel_key.py
"""
import base64

from outpost.crypto import generate_channel_key

if __name__ == "__main__":
    print(base64.b64encode(generate_channel_key()).decode())
