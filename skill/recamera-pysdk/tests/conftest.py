import json
from pathlib import Path
import re
import sys

import pytest

SKILL = Path(__file__).resolve().parents[1]
REPO = SKILL.parents[1]
sys.path.insert(0, str(SKILL / "scripts"))


@pytest.fixture
def manifest():
    # Exercise the documented minimum package, not a separate test-only schema.
    text = (SKILL / "references/app-packaging.md").read_text()
    return json.loads(re.search(r"```json\n(.*?)\n```", text, re.S)[1])


@pytest.fixture
def make_app(tmp_path, manifest):
    def make(source=None, files=None):
        app = tmp_path / "app"
        app.mkdir(exist_ok=True)
        (app / "manifest.json").write_text(json.dumps(manifest))
        source = source or "from kit.app import App\nclass Demo(App):\n    owns_loop = True\n    needs_model = False\n    def run(self):\n        pass\n"
        (app / "app.py").write_text(source)
        for name, content in (files or {}).items():
            path = app / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return app
    return make
