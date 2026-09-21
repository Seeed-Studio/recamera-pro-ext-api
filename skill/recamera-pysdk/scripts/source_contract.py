"""Conservative AST checks for Kit entry points; never execute app code.

Resolve local imports, re-exports, aliases and inheritance. Dynamic factories or
external base classes remain explicitly unverified rather than being guessed.
"""
from __future__ import annotations

import ast
from pathlib import PurePosixPath

KIT = ("external", "kit.app.App")
UNKNOWN = ("unknown",)


def issue(severity, code, location, message):
    return dict(severity=severity, code=code, location=location, message=message)


def validate_managed_result_channel(tree: ast.AST, relative: str, issues: list[dict[str, str]]) -> None:
    """Reject child-owned result listeners and hand-written AppMgr identity."""
    sink_classes = {"WsResultSink", "GatewayResultSink"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function_name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if function_name in sink_classes:
                issues.append(issue(
                    "error", "direct_result_sink_construction", f"{relative}:{node.lineno}",
                    "AppMgr-managed Apps must publish through kit.App.emit(); do not construct WsResultSink or GatewayResultSink",
                    ))
            port_keyword = next((keyword.value for keyword in node.keywords if keyword.arg == "port"), None)
            if isinstance(port_keyword, ast.Constant) and port_keyword.value == 8124:
                issues.append(issue(
                    "error", "fixed_result_sink_port", f"{relative}:{node.lineno}",
                    "AppMgr-managed Apps must not hard-code the reserved result port 8124",
                ))
            if function_name in {"open_result_sink", "select_result_sink"}:
                kind = node.args[0] if node.args else None
                if isinstance(kind, ast.Constant) and kind.value in {"ws", "osd"}:
                    issues.append(issue(
                        "error", "direct_result_sink_construction", f"{relative}:{node.lineno}",
                        "AppMgr-managed Apps must not select a child-owned WebSocket/OSD sink; use kit.App.emit()",
                    ))
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if node.func.value.id == "os" and node.func.attr in {"putenv"}:
                    first = node.args[0] if node.args else None
                    if isinstance(first, ast.Constant) and first.value in {"RECAMERA_RESULT_GATEWAY_SOCK", "RECAMERA_RESULT_GATEWAY_REQUIRED", "RECAMERA_APP_ID", "RECAMERA_APP_INSTANCE", "RECAMERA_APP_GENERATION"}:
                        issues.append(issue(
                            "error", "manual_managed_gateway_override", f"{relative}:{node.lineno}",
                            "Gateway and application identity are minted by AppMgr and must not be set by the App",
                        ))
        target = None
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) and node.targets else node.target
        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Attribute):
            if isinstance(target.value.value, ast.Name) and target.value.value.id == "os" and target.value.attr == "environ":
                key = target.slice
                if isinstance(key, ast.Constant) and key.value in {"RECAMERA_RESULT_GATEWAY_SOCK", "RECAMERA_RESULT_GATEWAY_REQUIRED", "RECAMERA_APP_ID", "RECAMERA_APP_INSTANCE", "RECAMERA_APP_GENERATION"}:
                    issues.append(issue(
                        "error", "manual_managed_gateway_override", f"{relative}:{node.lineno}",
                        "Gateway and application identity are minted by AppMgr and must not be assigned by the App",
                    ))
        # Plain documentation strings and diagnostic messages are harmless. The
        # validator only rejects 8124 when it is used as a socket/CLI argument,
        # which is handled by the call-site checks above.


class Sources:
    def __init__(self, trees, entry):
        self.modules = {}
        self.paths = {}
        self.packages = set()
        self.classes = {}
        self.dynamic = set()
        self.entry = self.module_name(entry)
        self.entry_parent = str(PurePosixPath(entry).parent)
        for path, tree in trees:
            module = self.module_name(path)
            self.modules[module] = tree
            self.paths[module] = path
            if path.endswith("/__init__.py"):
                self.packages.add(module)
        self.bindings = {}

    @staticmethod
    def module_name(path):
        name = str(PurePosixPath(path).with_suffix("")).replace("/", ".")
        return name.removesuffix(".__init__")

    def import_name(self, module, node):
        name = node.module or ""
        if node.level:
            package = module.split(".") if module in self.packages else module.split(".")[:-1]
            package = package[:len(package) - node.level + 1]
            return ".".join(package + ([name] if name else []))
        # kit.run inserts the entry directory into sys.path.
        if self.entry_parent != ".":
            local = self.entry_parent.replace("/", ".") + "." + name
            if local in self.modules:
                return local
        return name

    def attribute(self, owner, name):
        if owner[0] == "module":
            target = owner[1]
            if target in self.modules:
                if target not in self.bindings:
                    self.scan(target)
                return self.bindings[target].get(name, UNKNOWN)
            return ("external", target + "." + name)
        if owner[0] == "external":
            return ("external", owner[1] + "." + name)
        return UNKNOWN

    def resolve(self, module, node):
        if isinstance(node, ast.Name):
            return self.bindings[module].get(node.id, UNKNOWN)
        if isinstance(node, ast.Attribute):
            return self.attribute(self.resolve(module, node.value), node.attr)
        if isinstance(node, ast.Call):
            cls = self.resolve(module, node.func)
            return ("instance", *cls[1:]) if cls[0] == "class" else UNKNOWN
        if isinstance(node, ast.Constant):
            return ("literal", node.value)
        return UNKNOWN

    def scan(self, module):
        if module in self.bindings:
            return
        env = self.bindings[module] = {}
        for node in self.modules[module].body:
            if isinstance(node, ast.ImportFrom):
                target = self.import_name(module, node)
                if target in self.modules:
                    self.scan(target)
                for alias in node.names:
                    if alias.name == "*":
                        # __all__ can be computed dynamically; do not assume all exports.
                        self.dynamic.add(module)
                    else:
                        value = self.attribute(("module", target), alias.name)
                        if value == UNKNOWN and target + "." + alias.name in self.modules:
                            value = ("module", target + "." + alias.name)
                        env[alias.asname or alias.name] = value
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    env[alias.asname or alias.name.split(".")[0]] = (
                        "module", alias.name if alias.asname else alias.name.split(".")[0])
            elif isinstance(node, ast.ClassDef):
                key = ("class", module, node.name)
                self.classes[key] = (node, [self.resolve(module, b) for b in node.bases])
                env[node.name] = key
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = self.resolve(module, node.value)
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    if isinstance(target, ast.Name):
                        env[target.id] = value
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                # Definitions inside control flow cannot be resolved without executing it.
                if any(isinstance(n, (ast.ClassDef, ast.ImportFrom, ast.Import, ast.Assign)) for n in ast.walk(node)):
                    self.dynamic.add(module)

    def lineage(self, cls, seen=()):
        if cls in seen or cls not in self.classes:
            return []
        bases = self.classes[cls][1]
        return bases + [a for b in bases for a in self.lineage(b, (*seen, cls))]

    def is_app(self, cls):
        return KIT in self.lineage(cls)

    def member(self, cls, name, seen=()):
        if cls in seen or cls not in self.classes:
            return None
        node, bases = self.classes[cls]
        for child in reversed(node.body):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name:
                return child
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                    return child.value
        for base in bases:
            value = self.member(base, name, (*seen, cls))
            if value is not None:
                return value
        return None

    def select_entry(self):
        if self.entry not in self.modules:
            return "invalid", None, "Entry Python source is missing from the payload."
        self.scan(self.entry)
        env = self.bindings[self.entry]
        explicit = env.get("APP")
        if explicit is not None and explicit != ("literal", None):
            candidate = ("class", *explicit[1:]) if explicit[0] == "instance" else explicit
            if self.is_app(candidate):
                return "resolved", candidate, "Explicit APP resolves to a Kit App."
            if explicit == UNKNOWN or explicit[0] == "external" or any(
                b[0] in {"unknown", "external"} for b in self.lineage(candidate)
            ):
                return "unverified", None, "APP or its base is dynamic/external; verify kit.run.find_app on the target runtime."
            return "invalid", None, "APP must be a kit.app.App subclass or instance."
        visible = {v for v in env.values() if v[0] == "class" and self.is_app(v)}
        own = {v for v in visible if v[1] == self.entry}
        candidates = own or visible
        leaves = [c for c in candidates if not any(c in self.lineage(o) for o in candidates if c != o)]
        if len(leaves) > 1:
            return "invalid", None, "Several Kit App subclasses are visible; set APP to select one."
        if self.entry in self.dynamic:
            return "unverified", None, "Entry exports depend on runtime control flow; verify the loader with the target runtime."
        if len(leaves) == 1:
            return "resolved", leaves[0], "Single Kit App leaf class resolves statically."
        uncertain = any(v == UNKNOWN or v[0] == "external" and v != KIT for v in env.values())
        uncertain |= any(b != KIT and b[0] in {"unknown", "external"} for c in env.values()
                         if c[0] == "class" for b in self.classes.get(c, (None, []))[1])
        # An ordinary SDK import is not evidence of a possible App export.
        uncertain = uncertain and (any(v == UNKNOWN for v in env.values()) or
            any(v[0] == "class" for v in env.values()) or any(
                v[0] == "external" and not v[1].startswith(("recamera_ext.", "kit.app.", "time.", "os.", "sys.", "pathlib."))
                for v in env.values()))
        return ("unverified" if uncertain else "invalid"), None, (
            "No statically resolved Kit entry; external/dynamic exports require loader verification." if uncertain else
            "No kit.app.App subclass found. A standalone SDK script is a demo; wrap its loop in a Kit App for App Center.")


def signature_error(call, spec):
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        return None
    positional = spec["positional"]
    keys = {k.arg for k in call.keywords}
    if len(call.args) > len(positional) and not spec["varargs"]:
        return "too many positional arguments"
    allowed = set(positional[spec["posonly"]:]) | set(spec["keyword_only"])
    if keys - allowed and not spec["kwargs"]:
        return "unexpected keyword(s): " + ", ".join(sorted(keys - allowed))
    if keys & set(positional[:len(call.args)]):
        return "argument supplied both positionally and by keyword"
    required = set(positional[len(call.args):spec["required"]]) | set(spec["required_keywords"])
    if required - keys:
        return "missing required argument(s): " + ", ".join(sorted(required - keys))
    return None


def inspect_sources(trees, entry, signatures, require_entry=True):
    sources = Sources(trees, entry)
    state, selected, explanation = sources.select_entry()
    if selected and any(len(sources.classes[c][1]) > 1 for c in [selected, *sources.lineage(selected)] if c in sources.classes):
        # Do not substitute a depth-first walk for Python's C3 MRO when mixins
        # may change inherited run/setup/owns_loop. Keep the entry reviewable.
        state, selected, explanation = "unverified", None, "Multiple-inheritance entry needs loader/MRO verification; no app code was executed."
    issues = []
    if require_entry and state != "resolved":
        issues.append(issue("error" if state == "invalid" else "warning", "managed_entry_" + state, entry, explanation))
    if selected is not None:
        owns = sources.member(selected, "owns_loop")
        run = sources.member(selected, "run")
        if not isinstance(owns, ast.Constant) or not bool(owns.value):
            issues.append(issue("error", "missing_owns_loop", entry, "Kit entry must inherit or declare owns_loop = True."))
        if not isinstance(run, ast.FunctionDef):
            issues.append(issue("error", "missing_run", entry, "Kit entry must implement or inherit synchronous run(self)."))
        else:
            args = run.args
            if len(args.posonlyargs + args.args) != 1 or args.vararg or any(d is None for d in args.kw_defaults):
                issues.append(issue("error", "invalid_run_signature", entry, "Kit invokes run() without arguments; use run(self)."))
        for name in ("on_results", "process_frame", "run_postproc"):
            if sources.member(selected, name) is not None:
                issues.append(issue("error", "removed_kit_callback", entry, f"{name} was removed from the Kit lifecycle."))
        setup = sources.member(selected, "setup")
        if isinstance(setup, ast.FunctionDef) and not any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "setup"
            and isinstance(n.func.value, ast.Call) and isinstance(n.func.value.func, ast.Name)
            and n.func.value.func.id == "super" for n in ast.walk(setup)
        ):
            issues.append(issue("warning", "review_super_setup", entry,
                                "setup override has no visible super().setup(config); verify it preserves Kit initialization."))
    # Load all source modules for API checks, but only resolve the entry as a launch candidate.
    for module in sources.modules:
        sources.scan(module)
    uses_gateway = False
    uses_direct = False
    recording_events = []
    for cls, (node, _) in sources.classes.items():
        if not sources.is_app(cls):
            continue
        relative = sources.paths[cls[1]]
        lineage = [cls, *sources.lineage(cls)]
        nodes = [sources.classes[c][0] for c in lineage if c in sources.classes]
        assigned = set()
        dynamic = any(c not in sources.classes and c != KIT for c in lineage)
        for owner in nodes:
            dynamic |= any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and
                           n.name in {"__getattr__", "__getattribute__"} for n in owner.body)
            for n in ast.walk(owner):
                if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name) and n.value.id == "self":
                    assigned.add(n.attr)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "setattr" and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == "self":
                    if len(n.args) > 1 and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str):
                        assigned.add(n.args[1].value)
                    else:
                        dynamic = True
                if isinstance(n, ast.Attribute) and n.attr == "__dict__":
                    dynamic = True
        for method in node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(method):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                    continue
                if not isinstance(call.func.value, ast.Name) or call.func.value.id != "self":
                    continue
                name = call.func.attr
                if sources.member(cls, name) is not None or name in assigned:
                    continue
                if name not in signatures:
                    issues.append(issue("warning" if dynamic else "error",
                        "unverified_app_method" if dynamic else "unknown_app_method",
                        f"{relative}:{call.lineno}",
                        f"self.{name} is not defined by the App or the pinned Kit API; verify dynamic dispatch."))
                    continue
                uses_gateway |= name in {"emit", "request_recording"}
                if name == "request_recording":
                    event = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "event_kind"), None)
                    if isinstance(event, ast.Constant) and isinstance(event.value, str):
                        recording_events.append(event.value)
                error = signature_error(call, signatures[name])
                if error:
                    issues.append(issue("error", "invalid_kit_api_call", f"{relative}:{call.lineno}", f"App.{name}: {error}"))
    for module, tree in sources.modules.items():
        validate_managed_result_channel(tree, sources.paths[module], issues)
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            func = sources.resolve(module, call.func)
            uses_direct |= func == ("external", "recamera_ext.ResultSink")
            if func in {("external", "recamera_ext.OsdSink"), ("external", "recamera_ext.RecordSink")}:
                issues.append(issue("error", "appmgr_only_api", f"{sources.paths[module]}:{call.lineno}",
                                    "OsdSink and RecordSink are AppMgr-only endpoints; regular apps cannot open them."))
    return {"state": state, "detail": explanation, "uses_gateway": uses_gateway,
            "uses_direct_result": uses_direct, "recording_events": recording_events, "issues": issues}


def result_contract_issues(manifest, source):
    resources = manifest.get("resources")
    resources = resources if isinstance(resources, dict) else {}
    claims = resources.get("claims") or []
    claims = claims if isinstance(claims, list) else []
    publish = [c for c in claims if isinstance(c, dict) and c.get("name") == "result.publish"]
    result = []
    gateway = source["uses_gateway"] or bool(manifest.get("record_trigger"))
    if not gateway and not any(c.get("mode") == "brokered" for c in publish):
        result.append(issue("warning", "kit_default_sink_unverified", "resources.claims",
                            "Default kit.run opens a ws sink even without emit calls. Without an injected gateway it defaults to port 8124. "
                            "Direct SDK/compute-only integration needs a verified lifecycle/adapter route that avoids a child-owned listener; resource validity alone does not prove launchability."))
    if gateway and (len(publish) != 1 or publish[0].get("mode") != "brokered"):
        result.append(issue("error", "managed_result_claim_not_brokered", "resources.claims",
                            "Kit emit/request_recording requires result.publish mode=brokered for its authenticated gateway."))
    instances = manifest.get("instances")
    instances = instances if isinstance(instances, dict) else {}
    if gateway and instances.get("endpoint_mode") != "allocated":
        result.append(issue("error", "invalid_managed_endpoint_mode", "instances.endpoint_mode",
                            "Kit gateway publication requires endpoint_mode=allocated."))
    if source["uses_direct_result"] and (not publish or publish[0].get("mode") not in {"shared", "exclusive"}):
        result.append(issue("error", "missing_direct_result_ingress", "resources.claims",
                            "Direct recamera_ext.ResultSink requires result.publish shared/exclusive ingress; it is separate from Kit emit."))
    recording = manifest.get("record_trigger")
    recording = recording if isinstance(recording, dict) else {}
    signals = recording.get("signals")
    signals = signals if isinstance(signals, list) else []
    allowed = {s.get("event_kind") for s in signals if isinstance(s, dict) and s.get("type") == "event"}
    for event in sorted(set(source.get("recording_events", [])) - allowed):
        result.append(issue("error", "undeclared_recording_event", "record_trigger", f"Recording event {event!r} is not authorized by the manifest."))
    return result


def detector_contract_issues(trees, manifest):
    """The standard detector's defaults only describe 640/COCO80 models."""
    models = manifest.get("models") or []
    if not isinstance(models, list) or not models or not isinstance(models[0], dict):
        return []
    primary = models[0]
    shape = primary.get("input") or []
    custom_size = isinstance(shape, list) and len(shape) >= 3 and shape[1:3] != [640, 640]
    classes = primary.get("classes")
    custom_classes = classes is not None and classes != "coco80"
    sources = Sources(trees, manifest.get("entry", "app.py"))
    for module in sources.modules:
        sources.scan(module)
    issues = []
    for module, tree in sources.modules.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or sources.resolve(module, node.func) != (
                "external", "kit.runtime.postprocess.detect.postprocess"
            ):
                continue
            keys = {k.arg for k in node.keywords}
            if None in keys or any(isinstance(a, ast.Starred) for a in node.args):
                issues.append(issue("warning", "detector_parameters_unverified", sources.paths[module],
                                    "Dynamic postprocess arguments require a model-specific smoke test."))
                continue
            for needed, name, position in ((custom_size, "input_size", 4), (custom_classes, "class_names", 5)):
                if needed and name not in keys and len(node.args) <= position:
                    issues.append(issue("error", "missing_detector_model_parameter",
                                        f"{sources.paths[module]}:{node.lineno}",
                                        f"Custom detector must pass {name}; postprocess defaults to 640/COCO80."))
    return issues
