"""Setuptools build hooks for the production kit wheel."""

from pathlib import Path
from shutil import rmtree

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


class build_py(_build_py):
    """Keep colocated test modules out of the production wheel."""

    def find_package_modules(self, package, package_dir):
        modules = super().find_package_modules(package, package_dir)
        return [
            module
            for module in modules
            if not module[1].startswith("test_") and module[1] != "setup"
        ]

    def run(self):
        super().run()

        # ``build/lib`` is incremental. Prune files produced by an older build
        # configuration so an excluded test cannot survive into a later wheel.
        package_root = Path(self.build_lib) / "kit"
        rmtree(package_root / "tests", ignore_errors=True)
        for test_module in package_root.rglob("test_*.py"):
            test_module.unlink()
        (package_root / "setup.py").unlink(missing_ok=True)


setup(cmdclass={"build_py": build_py})
