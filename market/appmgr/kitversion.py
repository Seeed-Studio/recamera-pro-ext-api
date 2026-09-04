"""
Kit compatibility gate: does the installed kit satisfy an app's `manifest.kit`?

An app package carries the app's own files but NOT the shared `kit` runtime
(`docs/guide/kit-design.md` §0.6), so an app that uses a kit API added last
month installs cleanly onto a device whose kit predates it and then dies at
startup with `ModuleNotFoundError`. That is not hypothetical: face-analysis
0.2.0 imports `kit.logic.attributes`, added in kit 1.6.4, and before this module
existed nothing stopped it landing on a 1.6.3 kit -- the `"kit": ">=..."` field
every manifest already carried was inert documentation that no code read.

This module makes that field load-bearing. `installer.inspect()` calls
`check()`, so both the install path and the "validate without installing" path
report the same verdict, and the refusal happens before extraction.

## Reading the installed kit's version

By parsing `__version__` out of `<KIT_PARENT>/kit/__init__.py` as TEXT -- never
by importing it. appmgr runs under the system interpreter while apps run under
`/userdata/rknnenv`, kit may be absent or half-installed, and importing it would
execute arbitrary code from a directory appmgr is meant to be auditing.

## The pre-versioning kit

Kit gained `__version__` in 1.6.4. An older kit has no version to read, and
"unknown" must not become a blanket refusal: every app shipped before this gate
declares `"kit": ">=0.1.0"`, and failing those closed would brick the App Center
on exactly the devices that have not upgraded yet.

So the rule is scoped to what is actually knowable:

  * requirement below 1.6.4  -> ALLOW. Such a constraint predates versioning and
    was never enforced; there is no new information to act on.
  * requirement >= 1.6.4 and kit version unknown -> REFUSE. A kit that cannot
    state its version IS older than 1.6.4, because stating it is what 1.6.4
    added. This is the case the gate exists for.
  * kit version known -> compare properly.

## Version syntax

Dotted numeric releases (`1.6.4`, `0.2`, `2`) compared component-wise with
missing components treated as 0, so `1.6` == `1.6.0` and `1.6.4` > `1.6`.
Requirements accept `>=` (what every manifest uses), `>`, `==`, `<=`, `<`, and a
bare version meaning `>=`. A requirement that does not parse is a REFUSAL, not a
shrug: a typo'd constraint silently degrading to "anything goes" would recreate
the exact failure this module prevents. Suffixed versions (`1.6.4-rc1`) are not
supported and are rejected rather than guessed at.
"""
from __future__ import annotations

import os
import re
from typing import Optional, Tuple

from . import paths

# The release in which `kit/__init__.py` gained `__version__`. A kit without a
# readable version is, by construction, older than this.
KIT_VERSIONING_SINCE = (1, 6, 4)

_VERSION_RE = re.compile(
    r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_REQ_RE = re.compile(r"^\s*(>=|<=|==|>|<)?\s*([0-9]+(?:\.[0-9]+)*)\s*$")


class KitIncompatible(Exception):
    """The installed kit does not satisfy the app's declared requirement."""


def parse_version(text: str) -> Optional[Tuple[int, ...]]:
    """'1.6.4' -> (1, 6, 4). None if it is not a plain dotted numeric version."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", t):
        return None
    return tuple(int(p) for p in t.split("."))


def _pad(a: Tuple[int, ...], n: int) -> Tuple[int, ...]:
    return a + (0,) * (n - len(a))


def compare(a: Tuple[int, ...], b: Tuple[int, ...]) -> int:
    """-1 / 0 / 1, comparing component-wise with missing components as 0."""
    n = max(len(a), len(b))
    pa, pb = _pad(a, n), _pad(b, n)
    return (pa > pb) - (pa < pb)


def installed_version(kit_dir: Optional[str] = None) -> Optional[str]:
    """`__version__` of the installed kit, or None if it cannot be read.

    Text-parsed, never imported -- see the module docstring. None covers every
    "cannot know" case alike: kit absent, `__init__.py` missing or unreadable,
    or a pre-1.6.4 kit that simply has no `__version__` line.
    """
    d = kit_dir if kit_dir is not None else paths.KIT_DIR
    init = os.path.join(d, "__init__.py")
    try:
        with open(init, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(8192)
    except OSError:
        return None
    m = _VERSION_RE.search(head)
    return m.group(1).strip() if m else None


def parse_requirement(req: str) -> Optional[Tuple[str, Tuple[int, ...]]]:
    """'>=1.6.4' -> ('>=', (1,6,4)). A bare version means '>='. None if invalid."""
    if not isinstance(req, str):
        return None
    m = _REQ_RE.match(req)
    if not m:
        return None
    ver = parse_version(m.group(2))
    if ver is None:
        return None
    return (m.group(1) or ">="), ver


def satisfies(have: Tuple[int, ...], op: str, want: Tuple[int, ...]) -> bool:
    c = compare(have, want)
    return {
        ">=": c >= 0, ">": c > 0, "==": c == 0, "<=": c <= 0, "<": c < 0,
    }[op]


def check(manifest: dict, kit_dir: Optional[str] = None) -> Optional[str]:
    """Raise `KitIncompatible` if the installed kit fails `manifest["kit"]`.

    Returns the installed kit version string when it is known and the check
    passed, or None when there was nothing to verify (no requirement declared,
    or a pre-versioning requirement on a kit that cannot state its version).
    Callers surface the return value; the refusal path is the exception.
    """
    req_raw = manifest.get("kit")
    if req_raw in (None, ""):
        return None                      # app declares no requirement

    parsed = parse_requirement(req_raw)
    if parsed is None:
        raise KitIncompatible(
            f"manifest declares an unparsable kit requirement {req_raw!r}; "
            f"expected something like \">=1.6.4\"")
    op, want = parsed

    have_str = installed_version(kit_dir)
    if have_str is None:
        # Pre-versioning kit. Only act where the requirement actually tells us
        # something -- see the module docstring.
        if compare(want, KIT_VERSIONING_SINCE) >= 0:
            want_s = ".".join(str(x) for x in want)
            raise KitIncompatible(
                f"this app needs kit {req_raw}, but the installed kit does not "
                f"report a version, which means it predates kit "
                f"{'.'.join(str(x) for x in KIT_VERSIONING_SINCE)}. Install the "
                f"kit from release v{want_s} or newer "
                f"(recamera-ext-kit-v{want_s}.tar.gz), then install this app.")
        return None                      # nothing knowable, nothing to enforce

    have = parse_version(have_str)
    if have is None:
        raise KitIncompatible(
            f"installed kit reports an unparsable version {have_str!r}")

    if not satisfies(have, op, want):
        raise KitIncompatible(
            f"this app needs kit {req_raw}, but the installed kit is "
            f"{have_str}. Upgrade the kit "
            f"(recamera-ext-kit-v{'.'.join(str(x) for x in want)}.tar.gz or "
            f"newer), then install this app.")
    return have_str
