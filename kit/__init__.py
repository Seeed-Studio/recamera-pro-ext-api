"""reCamera Pro shared app runtime.

`__version__` is the KIT CONTRACT version an app can depend on, and it is the
same number as the release tarball the kit ships in
(`recamera-ext-kit-v<ver>.tar.gz`) -- there is deliberately no second numbering
scheme to keep in sync. `release/build-release.sh --version` rewrites the
literal below at pack time, so the value in a shipped kit always matches the
package it came out of; the value committed here is the last released one.

An app declares what it needs as `"kit": ">=<ver>"` in its manifest, and
appmgr refuses to install an app whose requirement the installed kit does not
meet (`market/appmgr/kitversion.py`). Before 1.6.4 the kit carried NO version at
all and the manifest field was inert documentation, which is how
face-analysis 0.2.0 -- importing the then-new `kit.logic.attributes` -- could be
installed onto a 1.6.3 kit and die with ModuleNotFoundError at startup. Treat a
kit without `__version__` as "older than 1.6.4"; that is what the gate does.

**Bump this whenever the kit gains an API an app may rely on** (a new module, a
new public function/field), and raise the depending app's `manifest.kit` to
match in the same change. Removing or renaming public kit API is a breaking
change and needs more than a bump -- apps pinned with `>=` will not be protected
from it.
"""

__version__ = "1.6.5"
