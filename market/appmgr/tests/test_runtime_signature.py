"""Runtime bundles are verified before they are unpacked as root (C1).

Why this test exists
--------------------
App packages have been signature-checked at install for a while
(installer.inspect -> signing.verify_package). Runtime bundles -- the voice
wheels and the GStreamer hwcodec `.so`s -- were NOT: they were path-gated and
member-vetted but never authenticated, so any client that could reach the
loopback appmgr could upload a forged wheel/.so and have it installed as root
into the SHARED /userdata/rknnenv and /userdata/lib. `voiceruntime._verify_and_extract`
closes that hole by putting runtime bundles through the same detached-signature
gate under the same REQUIRE_SIGNATURE policy as app packages.

The gate is also bound to a single open fd (TOCTOU C12): the bytes that are
verified are the bytes that are unpacked. These tests exercise the gate with a
throwaway EC P-256 keypair and a real openssl, so a signed bundle unpacks, an
unsigned one is refused under the default policy, and a bundle signed by the
wrong key is refused always.

No device, no network: the "package" is a tiny well-formed tar.gz under an
allowed root, and the trust anchor is a keypair generated in setUp.
"""
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MARKET = os.path.dirname(os.path.dirname(_HERE))
if _MARKET not in sys.path:
    sys.path.insert(0, _MARKET)

from appmgr import installer, paths, signing, voiceruntime  # noqa: E402


def _have_openssl() -> bool:
    return shutil.which("openssl") is not None


@unittest.skipUnless(_have_openssl(), "openssl required for signature tests")
class RuntimeSignatureGateTests(unittest.TestCase):
    """Focused on voiceruntime._verify_and_extract -- the one chokepoint both the
    wheels and files install paths funnel through."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtsig.")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # A throwaway release keypair: prime256v1 EC, exactly the curve the
        # device-side verify_package expects. The public half becomes the test's
        # trust anchor; the private half signs the bundle.
        self.priv = os.path.join(self.tmp, "priv.pem")
        self.pub = os.path.join(self.tmp, "pub.pem")
        subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey",
                        "-noout", "-out", self.priv], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["openssl", "ec", "-in", self.priv, "-pubout",
                        "-out", self.pub], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Production refuses group/world-writable trust anchors. Developer
        # machines commonly run with umask 0002, so make the fixture explicit.
        os.chmod(self.pub, 0o644)

        # A DIFFERENT keypair, to forge a signature that is valid crypto but wrong
        # signer.
        self.priv_other = os.path.join(self.tmp, "priv_other.pem")
        subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey",
                        "-noout", "-out", self.priv_other], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Point the device trust anchor + policy at the test values.
        self._prev_pub = paths.RELEASE_PUBKEY
        self._prev_req = paths.REQUIRE_SIGNATURE
        self._prev_roots = paths.ALLOWED_PKG_ROOTS
        paths.RELEASE_PUBKEY = self.pub
        paths.REQUIRE_SIGNATURE = True
        # The bundle lives under self.tmp, so the installer's root gate must allow
        # it. realpath: macOS temp dirs are symlinks; the gate resolves before
        # comparing.
        paths.ALLOWED_PKG_ROOTS = tuple(
            set(self._prev_roots) | {os.path.realpath(self.tmp)})
        self.addCleanup(setattr, paths, "RELEASE_PUBKEY", self._prev_pub)
        self.addCleanup(setattr, paths, "REQUIRE_SIGNATURE", self._prev_req)
        self.addCleanup(setattr, paths, "ALLOWED_PKG_ROOTS", self._prev_roots)

    def _bundle(self, name="voice-runtime-9.9.9.tar.gz") -> str:
        """A minimal well-formed runtime tarball under the allowed root."""
        payload = os.path.join(self.tmp, "payload")
        files = os.path.join(payload, "files")
        os.makedirs(files, exist_ok=True)
        with open(os.path.join(files, "hello.txt"), "wb") as f:
            f.write(b"payload bytes\n")
        tgz = os.path.join(self.tmp, name)
        with tarfile.open(tgz, "w:gz") as tar:
            tar.add(files, arcname="files")
        return tgz

    def _sign(self, pkg: str, key: str) -> str:
        """Write `<pkg>.sig` (base64 DER ECDSA-SHA256) -- the exact command
        packaging/sign.py uses -- and return the sidecar path."""
        der = pkg + ".der"
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", key,
                        "-out", der, pkg], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import base64
        with open(der, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        os.unlink(der)
        with open(pkg + ".sig", "w") as f:
            f.write(b64 + "\n")
        return pkg + ".sig"

    # -- the gate --------------------------------------------------------- #
    def test_signed_bundle_extracts(self):
        pkg = self._bundle()
        self._sign(pkg, self.priv)
        dest = os.path.join(self.tmp, "out")
        os.makedirs(dest)
        names = voiceruntime._verify_and_extract(pkg, None, dest)
        self.assertIn("files/hello.txt", names)
        self.assertTrue(os.path.isfile(os.path.join(dest, "files", "hello.txt")))

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"),
                         "Linux /proc fd paths required")
    def test_proc_fd_verification_inherits_only_open_package(self):
        """A CLOEXEC package fd remains the verification source after unlink.

        Unlinking makes it impossible for openssl to fall back to reopening the
        original pathname and therefore directly exercises fd inheritance while
        preserving the single-open-file TOCTOU guarantee.
        """
        pkg = self._bundle()
        sig_path = self._sign(pkg, self.priv)
        with open(sig_path) as f:
            signature_b64 = f.read().strip()

        package_fd = os.open(pkg, os.O_RDONLY)
        try:
            self.assertFalse(os.get_inheritable(package_fd))
            proc_path = f"/proc/self/fd/{package_fd}"
            os.unlink(pkg)

            status = signing.verify_package(
                proc_path, signature_b64, pubkey=self.pub,
            )
            self.assertTrue(status["verified"])
        finally:
            os.close(package_fd)

    def test_unsigned_bundle_refused_by_default(self):
        pkg = self._bundle()   # no .sig written, no explicit signature
        dest = os.path.join(self.tmp, "out")
        os.makedirs(dest)
        with self.assertRaises(voiceruntime.RuntimeError_) as cm:
            voiceruntime._verify_and_extract(pkg, None, dest)
        self.assertIn("signature", str(cm.exception).lower())
        # Nothing was written: the gate rejected before extraction.
        self.assertEqual(os.listdir(dest), [])

    def test_bad_signature_refused(self):
        pkg = self._bundle()
        self._sign(pkg, self.priv_other)   # valid crypto, WRONG signer
        dest = os.path.join(self.tmp, "out")
        os.makedirs(dest)
        with self.assertRaises(voiceruntime.RuntimeError_) as cm:
            voiceruntime._verify_and_extract(pkg, None, dest)
        self.assertIn("signature rejected", str(cm.exception).lower())
        self.assertEqual(os.listdir(dest), [])

    def test_explicit_signature_arg_is_honoured(self):
        """The catalog forwards the base64 signature directly (no sidecar on the
        device); the explicit arg must verify the same way."""
        import base64
        pkg = self._bundle()
        sig_path = self._sign(pkg, self.priv)
        with open(sig_path) as f:
            b64 = f.read().strip()
        os.unlink(sig_path)                # force reliance on the explicit arg
        dest = os.path.join(self.tmp, "out")
        os.makedirs(dest)
        names = voiceruntime._verify_and_extract(pkg, b64, dest)
        self.assertIn("files/hello.txt", names)

    def test_unsigned_allowed_when_policy_off(self):
        paths.REQUIRE_SIGNATURE = False
        pkg = self._bundle()
        dest = os.path.join(self.tmp, "out")
        os.makedirs(dest)
        names = voiceruntime._verify_and_extract(pkg, None, dest)
        self.assertIn("files/hello.txt", names)

    def test_tampered_bundle_after_signing_is_refused(self):
        """Signature is over the raw bytes: mutating the tar after signing must
        fail verification."""
        pkg = self._bundle()
        self._sign(pkg, self.priv)
        with open(pkg, "ab") as f:
            f.write(b"\x00trailing garbage")
        dest = os.path.join(self.tmp, "out")
        os.makedirs(dest)
        with self.assertRaises(voiceruntime.RuntimeError_):
            voiceruntime._verify_and_extract(pkg, None, dest)


class InstallForwardsSignatureTests(unittest.TestCase):
    """install() must thread `signature` down to the verify/extract chokepoint on
    BOTH the wheels and files branches -- otherwise the HTTP route could pass a
    signature that install() silently drops, defeating the gate."""

    def setUp(self):
        self._orig = voiceruntime._verify_and_extract
        self.seen = []

        def _spy(pkg_path, signature, dest_dir):
            self.seen.append((pkg_path, signature))
            # Raise a NON-signature error so install() stops right after the gate
            # without needing a real venv/probe.
            raise voiceruntime.RuntimeError_("stopped after gate (test)")

        voiceruntime._verify_and_extract = _spy
        self.addCleanup(setattr, voiceruntime, "_verify_and_extract", self._orig)

    def test_files_branch_forwards_signature(self):
        # hwcodec is kind:"files"; patch its dest to a temp userdata so the
        # pre-check passes and we reach the gate.
        tmp = tempfile.mkdtemp(prefix="fwd.")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        prev_root = voiceruntime.USERDATA_ROOT
        voiceruntime.USERDATA_ROOT = tmp
        self.addCleanup(setattr, voiceruntime, "USERDATA_ROOT", prev_root)
        # Redirect the staging dir off the real (read-only in CI) /userdata.
        prev_stage = paths.APPSTAGE_DIR
        paths.APPSTAGE_DIR = os.path.join(tmp, "appstage")
        self.addCleanup(setattr, paths, "APPSTAGE_DIR", prev_stage)
        spec = voiceruntime.RUNTIMES["hwcodec"]
        prev_dest = spec["dest"]
        spec["dest"] = os.path.join(tmp, "lib")
        self.addCleanup(spec.__setitem__, "dest", prev_dest)
        # A real, well-formed package under an allowed root -- install()'s early
        # path/root/size gate runs BEFORE the spy, so the path must be valid.
        prev_roots = paths.ALLOWED_PKG_ROOTS
        paths.ALLOWED_PKG_ROOTS = tuple(set(prev_roots) | {os.path.realpath(tmp)})
        self.addCleanup(setattr, paths, "ALLOWED_PKG_ROOTS", prev_roots)
        pkg = os.path.join(tmp, "gst-hwcodec-1.0.0.tar.gz")
        import io
        with tarfile.open(pkg, "w:gz") as tar:
            info = tarfile.TarInfo("files/x")
            data = b"x"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with self.assertRaises(voiceruntime.RuntimeError_):
            voiceruntime.install("hwcodec", pkg, signature="SIG-FILES")
        self.assertEqual(self.seen[-1][1], "SIG-FILES")


if __name__ == "__main__":
    unittest.main()
