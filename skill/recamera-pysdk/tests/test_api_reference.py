"""Public API documentation is executable coverage, without native imports."""
import ast
import json
import re
import sys

import pytest

from .conftest import REPO, SKILL
import api_reference as api


@pytest.fixture(scope="module")
def generated():
    lock = json.loads(api.LOCK.read_text())
    return api.build(REPO, lock["source_revision"], json.loads(api.NOTES.read_text()),
                     json.loads(api.HTTP_NOTES.read_text()))


def test_all_bundled_pages_and_inventory_match_sources(generated):
    pages, inventory = generated
    assert inventory == json.loads(api.LOCK.read_text())
    for name, text in pages.items():
        assert (api.DEST / name).read_text() == text, name
    # Real re-exports, source methods, data fields and platform boundaries.
    symbols = inventory["symbols"]
    for name in ("kit.Device", "kit.Frame", "kit.ImageOps", "kit.ResultBatch",
                 "kit.workflow.runtime.Pipeline.run", "kit.workflow.queue.InputQueue.put",
                 "recamera_ext.FrameSource.acquire", "recamera_ext.OsdSink",
                 "recamera_ext.BorrowedBuffer", "recamera_ext.ErrorCode",
                 "kit.runtime.engine.TensorSpec.shape"):
        assert name in symbols, name
    assert "POST /api/app-center/v1/apps" in inventory["http_routes"]
    assert symbols["kit.adapters.Frame"]["target"] == "kit.frame.Frame"
    assert symbols["kit.adapters.Capabilities"]["target"] == "kit.capabilities.Capabilities"
    header = (REPO / "sdk/include/recamera_ext.h").read_text().rstrip()
    assert header in pages["c-abi.md"]


def test_offline_document_links_resolve():
    paths = [SKILL / "SKILL.md", SKILL / "references/doc-router.md",
             SKILL / "references/capability-routing.md", SKILL / "references/sdk-contracts.md"]
    paths += list(api.DEST.rglob("*.md"))
    for path in paths:
        for target in re.findall(r"\[[^\]\n]*\]\(([^)\s]+)\)", path.read_text()):
            if "://" in target or target.startswith("#"):
                continue
            relative = target.split("#", 1)[0]
            assert (path.parent / relative).is_file(), (path, target)


@pytest.fixture
def mini_sdk(tmp_path):
    """A source-only SDK which must never execute during documentation."""
    for name in api.EXTRA_SOURCES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("" if name.endswith(".py") else "fixture header/license\n")
    (tmp_path / "kit").mkdir()
    (tmp_path / "kit/__init__.py").write_text('''
raise AssertionError("Never import SDK source for API extraction")
from .thing import Item as Alias
__all__ = ["Alias"]
''')
    (tmp_path / "kit/thing.py").write_text('''
from dataclasses import dataclass
class _Owner:
    def close(self) -> None:
        """Release the owner."""
class Item(_Owner):
    """An item with a property and an async method."""
    @property
    def value(self) -> int:
        """Read the current value."""
        return 1
    @value.setter
    def value(self, value: int) -> None:
        """Replace the value."""
        pass
    async def invoke(self, x: int, /, *, timeout: float = 1.0) -> str:
        """Return the invocation result."""
        return str(x)
@dataclass(frozen=True)
class Config:
    """Input configuration."""
    enabled: bool = True
    names: tuple[str, ...] = ()
''')
    notes = {"modules": {"kit": "Fixture exports", "kit.thing": "Fixture types"},
             "symbols": {}, "common": {}}
    return tmp_path, notes


def render(fixture):
    root, notes = fixture
    return api.build(root, "a" * 40, notes, {})


def test_alias_inherited_method_setter_and_data_fields(mini_sdk):
    pages, state = render(mini_sdk)
    symbols = state["symbols"]
    assert symbols["kit.Alias"]["target"] == "kit.thing.Item"
    assert symbols["kit.thing.Config.enabled"]["declaration"] == "enabled: bool = True"
    assert "kit.thing.Item.close" in symbols  # inherited private base
    assert "kit.thing.Item.value.setter" in symbols
    assert "async def invoke(self, x: int, /, *, timeout: float=1.0) -> str" in pages["python/kit-thing.md"]
    assert not any(key.startswith("kit.thing._Owner") for key in symbols)
    assert "kit.thing.dataclass" not in symbols  # implementation import


def test_new_module_requires_a_description(mini_sdk):
    root, _ = mini_sdk
    (root / "kit/new.py").write_text('"""A new public module."""\n')
    with pytest.raises(ValueError, match="Review module descriptions.*kit.new"):
        render(mini_sdk)


def test_new_undocumented_method_is_not_silently_covered(mini_sdk):
    root, _ = mini_sdk
    path = root / "kit/thing.py"
    path.write_text(path.read_text() + '\ndef new_method(x):\n    return x\n')
    with pytest.raises(ValueError, match="Missing API explanation: kit.thing.new_method"):
        render(mini_sdk)


def test_changed_default_and_implementation_cause_drift(mini_sdk):
    root, _ = mini_sdk
    _, before = render(mini_sdk)
    path = root / "kit/thing.py"
    original = path.read_text()
    path.write_text(original.replace("timeout: float = 1.0", "timeout: float = 2.0"))
    _, changed = render(mini_sdk)
    key = "kit.thing.Item.invoke"
    assert before["symbols"][key] != changed["symbols"][key]
    path.write_text(original.replace("return str(x)", 'return "changed"'))
    _, changed = render(mini_sdk)
    assert before["symbols"] == changed["symbols"]
    assert before["source_files"] != changed["source_files"]


def test_discovery_skips_internal_modules_and_build_artifacts(mini_sdk):
    root, _ = mini_sdk
    for relative in ("kit/_internal.py", "kit/test_sample.py", "kit/setup.py",
                     "kit/tests/a.py", "kit/build/a.py", "kit/.cache/a.py"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("invalid Python must never be parsed")
    assert {p.relative_to(root).as_posix() for p in api.discover(root)} == {
        "kit/__init__.py", "kit/thing.py"}
    render(mini_sdk)


def test_lazy_exports_and_relative_imports():
    tree = ast.parse('_EXPORTS = {"Item": "thing"}\nfrom ..errors import Failure as Error\n')
    assert api.imports(tree, "kit.nested", True) == {
        "Item": "kit.nested.thing.Item", "Error": "kit.errors.Failure"}


def test_guarded_public_imports_are_documented_but_function_imports_are_not():
    tree = ast.parse('''
try:
    from .errors import Failure
except ImportError:
    pass
def helper():
    from .thing import Item
''')
    assert api.imports(tree, "kit", True) == {"Failure": "kit.errors.Failure"}


def test_explicit_export_cannot_be_silently_missing(mini_sdk):
    root, _ = mini_sdk
    path = root / "kit/__init__.py"
    path.write_text(path.read_text().replace('["Alias"]', '["Alias", "Missing"]'))
    with pytest.raises(ValueError, match="undocumented exports in kit.*Missing"):
        render(mini_sdk)


def test_upstream_device_example_does_not_embed_an_account_or_address():
    assert api.documentation("tested at operator@10.20.30.40; loopback 127.0.0.1") == (
        "tested at <device-host>; loopback 127.0.0.1")


def test_route_extraction_includes_regex_and_constants():
    source = '''
ICON_ENDPOINT = "/icon"
class Handler:
    def do_GET(self):
        if path == ICON_ENDPOINT: pass
        if re.fullmatch(r"/apps/([a-z]+)/(config|logs)", path): pass
        if re.fullmatch(r"/not-a-route", body): pass
    def do_POST(self):
        if path == "/apps": pass
'''
    assert api.http_routes(source) == {
        "GET /icon", "GET /apps/([a-z]+)/(config|logs)", "POST /apps"}


@pytest.mark.parametrize("condition", ["path == configured_route", "path.startswith('/apps')"])
def test_dynamic_route_needs_explicit_review(condition):
    with pytest.raises(ValueError, match="Review"):
        api.http_routes("class H:\n def do_GET(self):\n  if " + condition + ": pass\n")


def test_added_http_route_fails_incomplete_coverage(mini_sdk):
    root, notes = mini_sdk
    (root / "market/appmgr/server.py").write_text('class H:\n def do_GET(self):\n  if path == "/new": pass\n')
    with pytest.raises(ValueError, match="Review HTTP routes: new=.*GET /new"):
        render(mini_sdk)
    with pytest.raises(ValueError, match="Incomplete HTTP explanation"):
        api.build(root, "a" * 40, notes, {"GET /new": {"path": "/new"}})


def test_check_is_read_only_and_detects_missing_page(mini_sdk, tmp_path, monkeypatch):
    root, notes = mini_sdk
    pages, inventory = render(mini_sdk)
    dest = tmp_path / "docs"
    for name, content in pages.items():
        path = dest / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    files = {"LOCK": inventory, "NOTES": notes, "HTTP_NOTES": {}}
    for name, value in files.items():
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(value))
        monkeypatch.setattr(api, name, path)
    monkeypatch.setattr(api, "DEST", dest)
    monkeypatch.setattr(sys, "argv", ["api_reference.py", "--sdk-root", str(root), "--check"])
    assert api.main() == 0
    missing = dest / "python/kit-thing.md"
    missing.unlink()
    before_lock = api.LOCK.read_bytes()
    assert api.main() == 1
    assert not missing.exists()
    assert api.LOCK.read_bytes() == before_lock


def test_documented_frame_copy_runs_with_real_host_kit():
    # Only this explicitly selected CPU example executes; generator never does.
    features = (api.DEST / "features.md").read_text()
    examples = re.findall(r"```python\n(.*?)\n```", features, re.S)
    assert examples
    namespace = {}
    exec(compile(examples[0], "api/features.md:buffer-example", "exec"), namespace)
    from kit.app import App
    namespace = {}
    exec(compile(examples[1], "api/features.md:cpu-app-example", "exec"), namespace)
    demo = namespace["Demo"]
    assert issubclass(demo, App)
    assert demo.owns_loop and not demo.needs_model and not demo.needs_frames
