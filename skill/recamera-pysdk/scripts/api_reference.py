"""Build/check the bundled API reference from source without importing the SDK.

The inventory is deliberately wider than the recommended application API:
compatibility adapters and platform-only entry points remain visible, with
their caller restrictions in api/features.md. Never execute native imports.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re

SKILL = Path(__file__).resolve().parents[1]
DEST = SKILL / "references/api"
LOCK = SKILL / "scripts/api-reference-lock.json"
NOTES = SKILL / "scripts/api-notes.json"
HTTP_NOTES = SKILL / "scripts/api-http-notes.json"
REPOSITORY = "https://github.com/Seeed-Studio/recamera-pro-ext-api"
EXCLUDED = {"setup.py", "demo_detect.py", "demo_live.py", "ws_probe.py"}
SPECIAL = {"__init__", "__enter__", "__exit__", "__iter__", "__next__", "__call__", "__or__",
           "__len__", "__getitem__"}
EXTRA_SOURCES = (
    "sdk/include/recamera_ext.h", "market/appmgr/server.py",
    "market/appmgr/workflow_ui.py", "market/appmgr/workflow_models.py",
    "market/appmgr/store_tasks.py", "market/appmgr/result_hub.py",
    "market/appmgr/output_tools.py", "market/appmgr/visualization.py",
    "market/appmgr/workflow_model_contract.py", "market/appmgr/operations.py",
    "sdk/LICENSE", "sdk/NOTICE",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def documentation(value: str) -> str:
    """Keep reusable contracts, omit upstream test-device account/address."""
    return re.sub(
        r"\b(?:[A-Za-z][\w.-]*@)?(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))"
        r"\.\d{1,3}\.\d{1,3}\b", "<device-host>", value)


def discover(root: Path) -> list[Path]:
    """Include new source modules, exclude generated wheels/builds and tests."""
    found = []
    for directory in (root / "kit", root / "sdk/python/recamera_ext"):
        for path in directory.rglob("*.py"):
            relative = path.relative_to(directory)
            if any(p in {"tests", "build", "dist", "__pycache__"} or p.startswith(".")
                   for p in relative.parts[:-1]):
                continue
            if path.name in EXCLUDED or path.name.startswith("test_"):
                continue
            if path.name.startswith("_") and path.name != "__init__.py":
                continue
            found.append(path)
    return sorted(found)


def module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix().removeprefix("sdk/python/")
    return relative.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def page_name(module: str) -> str:
    return module.replace(".", "-") + ".md"


def public(name: str) -> bool:
    return not name.startswith("_") or name in SPECIAL


def declaration(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        returns = " -> " + ast.unparse(node.returns) if node.returns else ""
        decorators = "".join("@" + ast.unparse(d) + "\n" for d in node.decorator_list)
        return f"{decorators}{prefix}def {node.name}({ast.unparse(node.args)}){returns}"
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        decorators = "".join("@" + ast.unparse(d) + "\n" for d in node.decorator_list)
        return decorators + f"class {node.name}" + (f"({bases})" if bases else "")
    return ast.unparse(node)


def assignments(body):
    for i, node in enumerate(body):
        names = []
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        for name in names:
            if not public(name):
                continue
            following = body[i + 1] if i + 1 < len(body) else None
            doc = (following.value.value if isinstance(following, ast.Expr)
                   and isinstance(following.value, ast.Constant)
                   and isinstance(following.value.value, str) else "")
            yield name, node, doc


def module_statements(node):
    """Descend through import guards, never into a class/function scope."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            yield from module_statements(child)


def imports(tree, module, is_package):
    result = {}
    for node in module_statements(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = module.split(".") if is_package else module.split(".")[:-1]
                base = base[:len(base) - node.level + 1]
                source = ".".join(base + ([node.module] if node.module else []))
            else:
                source = node.module or ""
            for entry in node.names:
                if (source in {"kit", "recamera_ext"} or source.startswith(("kit.", "recamera_ext."))) and entry.name != "*":
                    result[entry.asname or entry.name] = source + "." + entry.name
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "_EXPORTS" for t in node.targets):
                for name, source in ast.literal_eval(node.value).items():
                    result[name] = module + "." + source + "." + name
    return result


def exported(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                return set(ast.literal_eval(node.value))
    return None


def http_routes(source: str) -> set[str]:
    """Read literal and regex routes from BaseHTTPRequestHandler methods."""
    tree = ast.parse(source)
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for name in node.targets:
                if isinstance(name, ast.Name):
                    constants[name.id] = node.value.value
    routes = set()
    for method in ast.walk(tree):
        if not isinstance(method, ast.FunctionDef) or method.name not in {
                "do_GET", "do_POST", "do_PUT", "do_DELETE", "do_PATCH", "do_HEAD"}:
            continue
        for node in ast.walk(method):
            values = []
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "path":
                for value in node.comparators:
                    if isinstance(value, ast.Constant):
                        values.append(value.value)
                    elif isinstance(value, ast.Name) and value.id in constants:
                        values.append(constants[value.id])
                    else:
                        raise ValueError("Review dynamic HTTP route: " + ast.unparse(node))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (isinstance(node.func.value, ast.Name) and node.func.value.id == "re"
                        and node.func.attr in {"fullmatch", "match"} and node.args
                        and len(node.args) > 1 and isinstance(node.args[1], ast.Name)
                        and node.args[1].id == "path"):
                    values.append(ast.literal_eval(node.args[0]))
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "path" and node.func.attr in {"startswith", "endswith"}:
                    raise ValueError("Review prefix/suffix HTTP route: " + ast.unparse(node))
            for value in values:
                if isinstance(value, str) and value.startswith("/"):
                    routes.add(method.name[3:] + " " + value)
    return routes


def summary(node, key, notes):
    extra = notes["symbols"].get(key, "")
    original = documentation(ast.get_docstring(node) or "")
    if extra:
        return extra + ("\n\n源码补充：\n\n" + original if original else "")
    if original:
        return original
    name = node.name
    common = notes["common"].get(name)
    if common:
        return common
    raise ValueError(f"Missing API explanation: {key}")


def link_for(target, modules):
    parts = target.split(".")
    for end in range(len(parts), 0, -1):
        module = ".".join(parts[:end])
        if module in modules:
            return f"[{target}]({page_name(module)})"
    return f"`{target}` (外部/基类接口)"


def build(root: Path, revision: str, notes: dict, http_notes: dict) -> tuple[dict[str, str], dict]:
    paths = discover(root)
    modules = {module_name(p, root): (p, ast.parse(p.read_text())) for p in paths}
    unknown = set(modules) - set(notes["modules"])
    stale = set(notes["modules"]) - set(modules)
    if unknown or stale:
        raise ValueError(f"Review module descriptions: new={sorted(unknown)}, removed={sorted(stale)}")
    alias_maps = {module: imports(tree, module, path.name == "__init__.py")
                  for module, (path, tree) in modules.items()}

    def canonical(target):
        seen = set()
        while target not in seen:
            seen.add(target)
            owner, _, name = target.rpartition(".")
            next_target = alias_maps.get(owner, {}).get(name)
            if not next_target:
                return target
            target = next_target
        raise ValueError("Review circular public re-export: " + target)

    pages = {}
    inventory = {}
    files = {}
    for module, (path, tree) in modules.items():
        relative = path.relative_to(root).as_posix()
        files[relative] = digest(path.read_bytes())
        url = f"{REPOSITORY}/blob/{revision}/{relative}"
        rows = [f"# {module}", "", "[API 索引](../index.md) · [接口特性与边界](../features.md)", "",
                f"源码基线：[{relative}]({url})；签名由 AST 提取，不导入硬件依赖。", "",
                notes["modules"][module], "",
                "源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；"
                "应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。", ""]
        module_doc = documentation(ast.get_docstring(tree) or "")
        if module_doc:
            rows += ["## 模块契约（源码）", "", module_doc, ""]
        aliases = imports(tree, module, path.name == "__init__.py")
        exports = exported(tree)
        defined = {n.name for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
        # Local imports in ordinary modules are implementation dependencies;
        # package __init__ and explicit __all__ define re-export surfaces.
        if path.name == "__init__.py" or exports is not None:
            chosen = {k: v for k, v in aliases.items() if not k.startswith("_")
                      and k not in defined and (exports is None or k in exports)}
            if chosen:
                rows += ["## 公开导出 / 别名", "", "| 导入名称 | 定义与完整说明 |", "| --- | --- |"]
                for name, target in sorted(chosen.items()):
                    target = canonical(target)
                    rows.append(f"| `{name}` | {link_for(target, modules)} |")
                    inventory[module + "." + name] = {"kind": "export", "target": target}
                rows.append("")
        constants = [(n, item, doc) for n, item, doc in assignments(tree.body)
                     if n not in {"log", "logger"}]
        if constants:
            rows += ["## 常量与类型别名", "", "以下为该版本源码值；默认地址不等于所有固件都开放该服务。", ""]
            for name, item, doc in constants:
                rows += ["```python", declaration(item), "```", doc, ""]
                inventory[module + "." + name] = {"kind": "value", "declaration": declaration(item)}
        local_classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}

        def methods(cls, seen=()):
            found = {}
            for base in cls.bases:
                if isinstance(base, ast.Name) and base.id in local_classes and base.id not in seen:
                    found.update(methods(local_classes[base.id], (*seen, cls.name)))
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and public(item.name):
                    suffix = ".setter" if any(isinstance(d, ast.Attribute) and d.attr == "setter" for d in item.decorator_list) else ""
                    found[item.name + suffix] = (item, cls.name)
            return found

        def render_function(item, key, level, inherited=None):
            rows.extend(["#" * level + " " + key, "", "```python", declaration(item), "```", ""])
            explanation_key = module + "." + inherited + "." + item.name if inherited else key
            rows.extend([summary(item, explanation_key, notes), ""])
            if inherited:
                rows.extend([f"此方法定义于基类 `{inherited}`。", ""])
            rows.extend([f"[实现与参数校验]({url}#L{item.lineno})", ""])
            inventory[key] = {"kind": "callable", "declaration": declaration(item)}

        for node in tree.body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) or not public(node.name):
                continue
            key = module + "." + node.name
            if not isinstance(node, ast.ClassDef):
                render_function(node, key, 2)
                continue
            rows += [f"## {key}", "", "```python", declaration(node), "```", "", summary(node, key, notes), ""]
            inventory[key] = {"kind": "class", "declaration": declaration(node)}
            fields = list(assignments(node.body))
            ctypes_fields = next((n.value for n in node.body if isinstance(n, ast.Assign)
                                  and any(isinstance(t, ast.Name) and t.id == "_fields_" for t in n.targets)), None)
            if ctypes_fields:
                rows += ["### C 结构字段", "", "```python", "_fields_ = " + ast.unparse(ctypes_fields), "```", ""]
                inventory[key]["ctypes_fields"] = ast.unparse(ctypes_fields)
            if fields:
                rows += ["### 字段 / 默认值", "", "`@dataclass` 未显式定义构造器时，由下列字段生成；无默认值的字段必填。", "", "```python"]
                rows += [declaration(item) for _, item, _ in fields]
                rows += ["```", ""]
                for name, item, doc in fields:
                    inventory[key + "." + name] = {"kind": "field", "declaration": declaration(item)}
                    if doc:
                        rows += [f"`{name}`：{doc}", ""]
            for base in node.bases:
                base_name = ast.unparse(base)
                if base_name in aliases:
                    rows += ["继承接口：" + link_for(canonical(aliases[base_name]), modules) + "。", ""]
            for name, (item, owner) in methods(node).items():
                render_function(item, key + "." + name, 3, owner if owner != node.name else None)
        pages["python/" + page_name(module)] = "\n".join(rows).rstrip() + "\n"
        if exports is not None:
            missing = sorted(name for name in exports if module + "." + name not in inventory)
            if missing:
                raise ValueError(f"Review undocumented exports in {module}: {missing}")
    for name, spec in inventory.items():
        if spec["kind"] == "export" and spec["target"] not in inventory and spec["target"] not in modules:
            raise ValueError(f"Review unresolved public export: {name} -> {spec['target']}")
    for relative in EXTRA_SOURCES:
        path = root / relative
        files[relative] = digest(path.read_bytes())
    header = (root / "sdk/include/recamera_ext.h").read_text()
    pages["c-abi.md"] = ("# Native C ABI\n\n[API 索引](index.md) · [调用边界](features.md#native-and-platform)\n\n"
        "本文件保留该版本完整公开头文件及原始参数、错误码、字段、生命周期说明。\n"
        "Python 应用优先使用绑定；`osd`/`record` 入口仅限 AppMgr，不能以导出符号的存在推断应用调用权限。\n\n"
        f"源码基线：`{revision}`，`sdk/include/recamera_ext.h`。\n\n"
        "原始授权声明：[SDK NOTICE 与 BSD-3-Clause 许可证](sdk-license.txt)。\n\n```c\n" + header.rstrip() + "\n```\n")
    pages["sdk-license.txt"] = ((root / "sdk/NOTICE").read_text().rstrip() + "\n\n"
                                 + (root / "sdk/LICENSE").read_text())
    routes = http_routes((root / "market/appmgr/server.py").read_text())
    if routes != set(http_notes):
        raise ValueError(f"Review HTTP routes: new={sorted(routes - set(http_notes))}, removed={sorted(set(http_notes) - routes)}")
    http_rows = ["# App Center HTTP routes", "", "[API 索引](index.md) · [鉴权、响应和 WS 契约](http.md)", "",
                 "覆盖 server.py 的所有 HTTP 路由分支；带可选后缀/多个 action 的分支在说明中逐项展开。",
                 "`app_id` 为 `[a-z0-9-]{1,64}`，task/upload id 为 32 位小写十六进制；builtin 仅在明确允许的接口适用。", ""]
    for route in sorted(routes):
        spec = http_notes[route]
        if not all(isinstance(spec.get(k), str) and spec[k].strip() for k in ("path", "request", "response", "features")):
            raise ValueError("Incomplete HTTP explanation: " + route)
        http_rows += [f"## {route.split(' ', 1)[0]} {spec['path']}", "",
                      f"- 请求：{spec['request']}", f"- 返回：{spec['response']}",
                      f"- 特性：{spec['features']}", "", f"源码路由：`{route}`", ""]
    pages["http-routes.md"] = "\n".join(http_rows)
    index = ["# Public API reference", "", "按任务只读需要的模块。以下文件随 skill 安装，可离线查阅。", "",
             f"源码基线：`{revision}`；打包器版本仍由 `sdk-contract-lock.json` 单独固定。", "",
             "先读 [接口特性、权限、生命周期及示例](features.md)。", "",
             "| Python 模块 | 说明 |", "| --- | --- |"]
    for module in modules:
        short = notes["modules"][module].split("\n", 1)[0].replace("|", "\\|")
        index.append(f"| [{module}](python/{page_name(module)}) | {short} |")
    index += ["", "其他公开边界：", "", "- [完整 C ABI](c-abi.md)",
              "- [HTTP、SSE、WebSocket 与平台接口](http.md)", "",
              "## 覆盖定义", "",
              "覆盖 `recamera_ext` 和 `kit` 非私有源码模块的公开定义、公开重导出、构造器、属性、类字段、枚举和本模块继承的方法；跨模块继承链接到基类。",
              "包括兼容/平台实现供诊断，但不把它们视作普通应用可任意调用的接口。下划线私有模块、测试、构建缓存、示例 CLI 不作为公开开发契约。",
              "参数签名、类型注解、默认值来自 AST；数据类的构造字段和原始文档一并保留。没有类型注解并不表示返回 None，返回语义以逐项说明为准。",
              "源码中的旧示例仅用于理解该模块；托管应用的入口、NPU 调度和结果路由以 `features.md`、`managed-runtime.md` 为准。",
              "接口存在不代表目标固件已部署，也不代表模型精度、吞吐或外设已通过真机验收。", "",
              "## 维护检查", "", "```text",
              "python scripts/api_reference.py --sdk-root <checkout> --check", "```", "",
              "检查模块集合、源码散列、导出/签名/字段和生成文档。新模块或无说明的定义会失败。",
              "审阅源码变化及 `api-notes.json` 后，使用 `--write --revision <full-commit>` 更新；命令不会更改 SDK 或打包器版本。", ""]
    pages["index.md"] = "\n".join(index)
    snapshot = {"source_revision": revision, "repository": REPOSITORY,
                "source_files": files, "symbols": inventory,
                "http_routes": sorted(routes),
                "generated_files": {name: digest(text.encode()) for name, text in sorted(pages.items())}}
    return pages, snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-root", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--write", action="store_true")
    parser.add_argument("--revision")
    args = parser.parse_args()
    try:
        previous = json.loads(LOCK.read_text()) if LOCK.exists() else {}
        revision = args.revision or previous.get("source_revision")
        if not revision or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Provide an explicit full source commit with --revision")
        pages, snapshot = build(args.sdk_root.resolve(), revision, json.loads(NOTES.read_text()),
                                json.loads(HTTP_NOTES.read_text()))
        if args.write:
            # Remove only obsolete generated files named by our previous lock.
            for name in set(previous.get("generated_files", {})) - set(pages):
                (DEST / name).unlink(missing_ok=True)
            for name, content in pages.items():
                target = DEST / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            LOCK.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
        else:
            changed = [name for name, content in pages.items()
                       if not (DEST / name).is_file() or (DEST / name).read_text() != content]
            if previous != snapshot or changed:
                print(json.dumps({"ok": False, "changed_pages": changed,
                    "source_changes": sorted(k for k in set(previous.get("source_files", {})) | set(snapshot["source_files"])
                        if previous.get("source_files", {}).get(k) != snapshot["source_files"].get(k))}, indent=2))
                return 1
        print(json.dumps({"ok": True, "modules": len(discover(args.sdk_root.resolve())),
                          "symbols": len(snapshot["symbols"]), "pages": len(pages)}, indent=2))
        return 0
    except (OSError, ValueError, SyntaxError) as exc:
        print(f"API reference error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
