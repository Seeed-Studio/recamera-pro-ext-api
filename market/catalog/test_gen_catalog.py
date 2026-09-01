"""
Tests for gen_catalog._build_models -- the shared-model resolver that turns a
models.json entry into catalog `models[]` entries.

Covers:
  * the real voice-transcribe entry: 10 entries across TWO target dirs (asr +
    asr/kws), each entry's sha256/size equal to the actual staged bytes, and the
    KWS files' URLs carrying the `kws/` staging segment;
  * per-entry structure is exactly {url, filename, sha256, size, target_path}
    (unchanged shape so the closed-source frontend's per-entry putModel loop
    keeps working);
  * backward compatibility: the OLD single-target form {target_path, files}
    still resolves.

Runnable with plain stdlib: `python3 test_gen_catalog.py` (or via pytest).
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import gen_catalog  # noqa: E402

MODELS_DIR = os.path.normpath(os.path.join(_HERE, "..", "packaging", "models"))
BASE = "https://cdn.example/recamera_pro/models/"
ENTRY_KEYS = {"url", "filename", "sha256", "size", "target_path"}


def _sha_size(path):
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
            n += len(c)
    return h.hexdigest(), n


@unittest.skipUnless(
    os.environ.get("RECAMERA_RELEASE_ARTIFACT_TESTS") == "1",
    "requires staged release packages",
)
class CapabilitiesForwardingTests(unittest.TestCase):
    """`capabilities` must survive manifest -> catalog.

    The store decides BEFORE install whether an app needs something the device
    may not have (`"audio"` -> the ~18 MB voice runtime, INSTALL_ASSETS_SPEC
    §3.1). It can only see the catalog. voice-transcribe's manifest has carried
    `["audio", "output"]` all along, but build_catalog listed a fixed set of keys
    and this was not among them, so the front end's check could never fire.
    """
    DIST = os.path.normpath(os.path.join(_HERE, "..", "packaging", "dist"))

    def _catalog(self):
        return gen_catalog.build_catalog(self.DIST, "https://cdn.example/pkgs/")

    def test_voice_transcribe_keeps_audio_capability(self):
        apps = {a["id"]: a for a in self._catalog()["apps"]}
        self.assertIn("voice-transcribe", apps, "voice-transcribe not in dist/")
        self.assertEqual(apps["voice-transcribe"].get("capabilities"),
                         ["audio", "output"])

    def test_catalog_never_invents_capabilities(self):
        """Only apps whose manifest declares them get the key at all."""
        for app in self._catalog()["apps"]:
            pkg = os.path.join(self.DIST, app["package"]["filename"])
            declared = gen_catalog._read_manifest(pkg).get("capabilities")
            if isinstance(declared, list):
                self.assertEqual(app.get("capabilities"), declared, app["id"])
            else:
                self.assertNotIn("capabilities", app, app["id"])

    def test_non_list_capabilities_is_dropped_not_forwarded(self):
        """A malformed manifest must not put a string where the UI expects a list."""
        with tempfile.TemporaryDirectory() as d:
            man = {"id": "bogus-app", "name": "Bogus", "version": "0.0.1",
                   "capabilities": "audio"}          # string, not a list
            pkg = os.path.join(d, "bogus-app-0.0.1-arm64.tar.gz")
            import io
            import tarfile as _tf
            blob = json.dumps(man).encode()
            with _tf.open(pkg, "w:gz") as tar:
                ti = _tf.TarInfo("manifest.json")
                ti.size = len(blob)
                tar.addfile(ti, io.BytesIO(blob))
            app = gen_catalog.build_catalog(d, "https://cdn.example/pkgs/")["apps"][0]
            self.assertNotIn("capabilities", app)


@unittest.skipUnless(
    os.environ.get("RECAMERA_RELEASE_ARTIFACT_TESTS") == "1",
    "requires staged release packages",
)
class RuntimeBundleTests(unittest.TestCase):
    """`runtimes` tells the store WHERE to get an on-demand runtime.

    Without it the store knows an app needs the audio runtime (capabilities) but
    has no URL to fetch it from, so the whole §3 path is inert. Keyed by
    capability, because that is the side the store is coming from.
    """
    DIST = os.path.normpath(os.path.join(_HERE, "..", "packaging", "dist"))
    BASE = "https://cdn.example/pkgs/"

    def test_bundle_present_yields_a_fetchable_descriptor(self):
        with tempfile.TemporaryDirectory() as rt:
            blob = b"not really a tarball, but the bytes are what get hashed"
            p = os.path.join(rt, "voice-runtime-1.0.0.tar.gz")
            with open(p, "wb") as f:
                f.write(blob)
            cat = gen_catalog.build_catalog(self.DIST, self.BASE, runtimes_dir=rt)
            got = cat["runtimes"]["audio"]
            sha, size = _sha_size(p)
            self.assertEqual(got, {
                "url": self.BASE + "voice-runtime-1.0.0.tar.gz",
                "filename": "voice-runtime-1.0.0.tar.gz",
                "sha256": sha,
                "size": size,
            })

    def test_absent_bundle_omits_the_key_rather_than_stubbing_it(self):
        """A stub with a dead URL would fail mid-download instead of up front."""
        with tempfile.TemporaryDirectory() as rt:
            cat = gen_catalog.build_catalog(self.DIST, self.BASE, runtimes_dir=rt)
            self.assertNotIn("runtimes", cat)

    def test_newest_wins_when_several_bundles_are_staged(self):
        with tempfile.TemporaryDirectory() as rt:
            for v in ("1.0.0", "1.0.2", "1.0.1"):
                with open(os.path.join(rt, f"voice-runtime-{v}.tar.gz"), "wb") as f:
                    f.write(v.encode())
            cat = gen_catalog.build_catalog(self.DIST, self.BASE, runtimes_dir=rt)
            self.assertEqual(cat["runtimes"]["audio"]["filename"],
                             "voice-runtime-1.0.2.tar.gz")

    def test_capability_key_matches_what_apps_declare(self):
        """The join between the two halves: app.capabilities[] -> runtimes{}."""
        with tempfile.TemporaryDirectory() as rt:
            with open(os.path.join(rt, "voice-runtime-1.0.0.tar.gz"), "wb") as f:
                f.write(b"x")
            cat = gen_catalog.build_catalog(self.DIST, self.BASE, runtimes_dir=rt)
            voice = next(a for a in cat["apps"] if a["id"] == "voice-transcribe")
            needed = [c for c in voice["capabilities"] if c in cat["runtimes"]]
            self.assertEqual(needed, ["audio"],
                             "voice-transcribe declares 'audio' but no runtime "
                             "in the catalog answers to that key")


class BuildModelsTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(_HERE, "models.json")) as f:
            self.spec = {k: v for k, v in json.load(f).items()
                         if not k.startswith("_")}

    @unittest.skipUnless(
        os.environ.get("RECAMERA_RELEASE_ARTIFACT_TESTS") == "1",
        "requires staged release models",
    )
    def test_voice_transcribe_two_groups(self):
        models = gen_catalog._build_models(
            "voice-transcribe", self.spec, MODELS_DIR, BASE)
        # 5 (asr) + 5 (asr/kws) = 10 entries.
        self.assertEqual(len(models), 10)

        by_target = {}
        for m in models:
            self.assertEqual(set(m), ENTRY_KEYS, f"entry shape changed: {set(m)}")
            by_target.setdefault(m["target_path"], []).append(m)

        self.assertEqual(set(by_target), {
            "/userdata/local/models/asr",
            "/userdata/local/models/asr/kws",
        })
        self.assertEqual(len(by_target["/userdata/local/models/asr"]), 5)
        self.assertEqual(len(by_target["/userdata/local/models/asr/kws"]), 5)

    @unittest.skipUnless(
        os.environ.get("RECAMERA_RELEASE_ARTIFACT_TESTS") == "1",
        "requires staged release models",
    )
    def test_sha256_and_url_match_staged(self):
        models = gen_catalog._build_models(
            "voice-transcribe", self.spec, MODELS_DIR, BASE)
        app_stage = os.path.join(MODELS_DIR, "voice-transcribe")
        for m in models:
            kws = m["target_path"].endswith("/kws")
            staged = os.path.join(app_stage, "kws" if kws else "", m["filename"])
            sha, size = _sha_size(staged)
            self.assertEqual(m["sha256"], sha, m["filename"])
            self.assertEqual(m["size"], size, m["filename"])
            # KWS files carry the staging subdir in their URL; others do not.
            expect = BASE + "voice-transcribe/" + ("kws/" if kws else "") + m["filename"]
            self.assertEqual(m["url"], expect)

    def test_backward_compatible_single_target(self):
        # The pre-groups form must still resolve. Stage a throwaway file and
        # point an old-style entry at it.
        tmp = tempfile.mkdtemp(prefix="models_bc.")
        app_stage = os.path.join(tmp, "legacy-app")
        os.makedirs(app_stage)
        with open(os.path.join(app_stage, "model.rknn"), "wb") as f:
            f.write(b"legacy-bytes" * 50)
        spec = {"legacy-app": {
            "target_path": "/userdata/local/models",
            "files": ["model.rknn"],
        }}
        models = gen_catalog._build_models("legacy-app", spec, tmp, BASE)
        self.assertEqual(len(models), 1)
        m = models[0]
        self.assertEqual(set(m), ENTRY_KEYS)
        self.assertEqual(m["target_path"], "/userdata/local/models")
        self.assertEqual(m["url"], BASE + "legacy-app/model.rknn")

    def test_absent_app_yields_empty(self):
        self.assertEqual(gen_catalog._build_models("nope", self.spec, MODELS_DIR, BASE), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
