"""Every web UI page's script must at least parse - one syntax error stops
the whole page's buttons working. Needs Node.js; skipped without it."""
from __future__ import annotations
import re
import shutil
import subprocess

import pytest

from outpost import webui

PAGES = sorted(n for n in dir(webui) if n.startswith("_") and n.endswith("_HTML"))


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
@pytest.mark.parametrize("page", PAGES)
def test_page_script_parses(page, tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", getattr(webui, page), re.S)
    assert scripts, f"{page} has no script"
    for i, js in enumerate(scripts):
        path = tmp_path / f"{page}{i}.js"
        path.write_text(js)
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
