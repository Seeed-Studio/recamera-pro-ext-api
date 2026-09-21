"""Import names are not distribution names. Inspect payload imports without executing them."""
import ast
import sys
from pathlib import PurePosixPath


PLATFORM_IMPORTS = frozenset({"cv2", "jinja2", "kit", "markupsafe", "numpy", "recamera_ext", "rknnlite"})


def required_imports(trees):
    """Separate mandatory imports from ImportError fallbacks and TYPE_CHECKING.

    Lazy imports still need packaging. Dynamic imports and optional fallbacks
    remain runtime concerns rather than guesses about which branch will execute.
    """
    result = []
    class Visitor(ast.NodeVisitor):
        optional = False
        def record(self, node, name):
            result.append({"module": name, "location": f"{relative}:{node.lineno}", "optional": self.optional})
        def visit_Import(self, node):
            for alias in node.names:
                self.record(node, alias.name)
        def visit_ImportFrom(self, node):
            if not node.level and node.module:
                self.record(node, node.module)
        def visit_If(self, node):
            if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING" or (
                isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING"
            ):
                for child in node.orelse:
                    self.visit(child)
                return
            self.generic_visit(node)
        def visit_Try(self, node):
            guarded = any(any(isinstance(n, ast.Name) and n.id in {"ImportError", "ModuleNotFoundError"}
                              for n in ast.walk(handler.type)) for handler in node.handlers if handler.type)
            previous = self.optional
            self.optional |= guarded
            for child in node.body:
                self.visit(child)
            self.optional = previous
            for child in [*node.handlers, *node.orelse, *node.finalbody]:
                self.visit(child)
    for relative, tree in trees:
        Visitor().visit(tree)
    return result


def check_imports(trees, entry, wheel_roots=(), declared=()):
    paths = {path for path, _ in trees}
    entry_parent = PurePosixPath(entry).parent
    local = {PurePosixPath(p).parts[0].removesuffix(".py") for p in paths}
    for p in paths:
        path = PurePosixPath(p)
        if path.is_relative_to(entry_parent):
            local.add(path.relative_to(entry_parent).parts[0].removesuffix(".py"))
    provided = local | PLATFORM_IMPORTS | set(wheel_roots) | set(sys.stdlib_module_names)
    missing, optional, private = [], [], set()
    for record in required_imports(trees):
        root = record["module"].split(".")[0]
        if root not in provided:
            (optional if record["optional"] else missing).append(record)
        elif root in set(wheel_roots) and not record["optional"]:
            private.add(record["module"])
    for module in declared:
        if module.split(".")[0] not in provided:
            missing.append({"module": module, "location": "python.imports", "optional": False})
    return {"missing": missing, "optional_unverified": optional, "private_imports": sorted(private)}
