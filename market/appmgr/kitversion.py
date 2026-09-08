"""Static compatibility checks for the shared kit runtime."""
from __future__ import annotations

import ast
import os
import re
from typing import Optional, Tuple

from . import paths

KIT_VERSIONING_SINCE = (1, 6, 4)
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
_TARGETS = {"__version__", "__legacy_version__", "__api_version__"}


class KitIncompatible(Exception):
    pass


def parse_version(value: str) -> Optional[Tuple[int, ...]]:
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value.strip()):
        return None
    return tuple(int(x) for x in value.strip().split("."))


def compare(a: Tuple[int, ...], b: Tuple[int, ...]) -> int:
    n = max(len(a), len(b))
    aa, bb = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (aa > bb) - (aa < bb)


def _read_metadata(kit_dir: Optional[str] = None) -> dict[str, str]:
    init = os.path.join(kit_dir or paths.KIT_DIR, "__init__.py")
    try:
        with open(init, "r", encoding="utf-8") as source:
            tree = ast.parse(source.read(), filename=init)
    except (OSError, SyntaxError, UnicodeError):
        return {}
    result: dict[str, str] = {}
    seen = set()
    literal_targets: set[int] = set()
    for node in tree.body:
        names = []
        value = None
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            literal_targets.update(id(t) for t in node.targets if isinstance(t, ast.Name))
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            literal_targets.add(id(node.target))
            value = node.value
        for name in names:
            if name not in _TARGETS:
                continue
            if name in seen:
                raise KitIncompatible("kit metadata declares %s more than once" % name)
            seen.add(name)
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                raise KitIncompatible("kit metadata %s must be a string literal" % name)
            result[name] = value.value
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and node.id in _TARGETS
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and id(node) not in literal_targets):
            raise KitIncompatible("kit metadata %s is assigned dynamically" % node.id)
        if isinstance(node, ast.alias) and (node.asname or node.name) in _TARGETS:
            raise KitIncompatible("kit version metadata must not be imported")
    for key, value in result.items():
        if parse_version(value) is None:
            raise KitIncompatible("invalid kit metadata version %s" % key)
    return result


def metadata(kit_dir: Optional[str] = None) -> dict[str, str]:
    return _read_metadata(kit_dir)


def installed_version(kit_dir: Optional[str] = None, *, api: bool = False) -> Optional[str]:
    values = _read_metadata(kit_dir)
    if api:
        return values.get("__api_version__")
    return values.get("__legacy_version__", values.get("__version__"))


def _parse_one(text: str) -> tuple[str, Tuple[int, ...], bool] | None:
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"\s*(>=|<=|==|!=|>|<)?\s*([0-9]+(?:\.[0-9]+)*)(\.\*)?\s*", text)
    if not m:
        return None
    op, raw, wildcard = m.group(1) or ">=", m.group(2), bool(m.group(3))
    if wildcard and op not in ("==", "!="):
        return None
    return op, tuple(int(x) for x in raw.split(".")), wildcard


def parse_requirement(req: str) -> list[tuple[str, Tuple[int, ...], bool]] | None:
    if not isinstance(req, str) or not req.strip():
        return None
    parts = req.split(",")
    parsed = [_parse_one(part) for part in parts]
    return parsed if all(parsed) else None


def _satisfies(have: Tuple[int, ...], item) -> bool:
    op, want, wildcard = item
    if wildcard:
        return have[:len(want)] == want if op == "==" else have[:len(want)] != want
    c = compare(have, want)
    return {">=": c >= 0, ">": c > 0, "==": c == 0,
            "<=": c <= 0, "<": c < 0, "!=": c != 0}[op]


def satisfies(have: Tuple[int, ...], op: str, want: Tuple[int, ...]) -> bool:
    """Compare one non-wildcard requirement (kept as a small public helper)."""
    return _satisfies(have, (op, want, False))


def check(manifest: dict, kit_dir: Optional[str] = None) -> Optional[str]:
    version = 1 if not isinstance(manifest, dict) else manifest.get("manifest_version", 1)
    compatibility = manifest.get("compatibility")
    req = (compatibility.get("kit_api") if isinstance(compatibility, dict) else None) \
        if version == 2 else manifest.get("kit")
    if req in (None, ""):
        return None
    requirements = parse_requirement(req)
    if requirements is None:
        raise KitIncompatible("unparsable kit requirement %r" % req)
    is_api = version == 2
    have_raw = installed_version(kit_dir, api=is_api)
    if have_raw is None:
        if is_api or any(compare(want, KIT_VERSIONING_SINCE) >= 0 for _, want, _ in requirements):
            kind = "kit API" if is_api else "kit"
            raise KitIncompatible("installed %s does not report a version" % kind)
        return None
    have = parse_version(have_raw)
    if have is None or not all(_satisfies(have, item) for item in requirements):
        raise KitIncompatible("installed kit version %r does not satisfy %s" % (have_raw, req))
    return have_raw
