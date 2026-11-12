"""Static publication checks; never import or execute application/wheel code."""
from __future__ import annotations

import sys
import zipfile


# Imports supplied by the firmware's supported Python runtime. PIL is also
# shipped by the firmware, though applications may carry their own Pillow.
PLATFORM_IMPORTS = frozenset({
    "PIL", "cv2", "jinja2", "kit", "markupsafe", "numpy", "recamera_ext", "rknnlite",
})


def _provides(files: set[str], module: str) -> bool:
    path = module.replace(".", "/")
    if path + ".py" in files or path + "/__init__.py" in files:
        return True
    # CPython/abi3 native extensions, or a namespace package with importable
    # descendants. dist-info/top_level.txt is not evidence of an actual module.
    return any(
        (name.startswith(path + ".") and "/" not in name[len(path):] and name.endswith(".so"))
        or (name.startswith(path + "/") and name.endswith((".py", ".so")))
        for name in files
    )


def validate_runtime_payload(manifest: dict, open_wheel) -> None:
    """Reject missing install probes/models after payload hashes are verified.

    open_wheel returns a context-managed binary stream for a declared bundled
    wheel. No host site-packages are consulted: the host is not the device.
    This is a presence check; target import execution remains an AppMgr check.
    """
    if manifest.get("manifest_version") != 2:
        return
    imports = manifest["python"]["imports"]
    pending = {name for name in imports
               if name.split(".", 1)[0] not in PLATFORM_IMPORTS | sys.stdlib_module_names}
    if pending:
        files = set()
        for wheel in manifest["python"]["wheels"]:
            if wheel["source"] != "bundled":
                continue
            try:
                with open_wheel(wheel["file"]) as source, zipfile.ZipFile(source) as archive:
                    files.update(info.filename for info in archive.infolist() if not info.is_dir())
            except (OSError, zipfile.BadZipFile) as exc:
                raise ValueError(f"Invalid wheel for import probes: {wheel['file']}: {exc}") from exc
        missing = sorted(name for name in pending if not _provides(files, name))
        if missing:
            raise ValueError(
                "python.imports not provided by bundled wheels or firmware: "
                + ", ".join(missing))

    scheduled = any(claim["name"] == "npu.rknn" and claim["mode"] in ("scheduled", "brokered")
                    for claim in manifest["resources"]["claims"])
    if scheduled:
        models = {a["file"] for a in manifest["artifacts"]
                  if a["kind"] == "rknn" and a["source"] == "bundled"}
        if not models:
            raise ValueError("Scheduled NPU application requires a bundled RKNN artifact")
        for model in manifest.get("models", []):
            if model.get("file") not in models:
                raise ValueError(f"Model {model.get('file')!r} has no bundled RKNN artifact")
