"""Desired-state coordinator for independently managed application processes."""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Callable, Optional

from . import (
    config as appconfig,
    inference_auth,
    paths,
    resources,
    state,
    supervisor,
)


class CoordinatorError(RuntimeError):
    pass


class AppCoordinator:
    """Coordinate lifecycle state, reservations, identity and supervisor calls.

    ``launch`` is injectable because legacy activate/switch must cross their
    existing builtin/NPU barrier, while a v2 scheduled-NPU or CPU app launches
    directly.  The callback receives only supervisor keyword arguments; the
    coordinator still owns the transaction and exact instance generation.
    """

    def __init__(self, *, resource_manager: Optional[resources.ResourceManager] = None,
                 supervisor_module=supervisor, state_module=state,
                 dependency_probe: Optional[Callable[[str], bool]] = None,
                 result_gateway_sock: Optional[str] = None,
                 inference_service_sock: Optional[str] = None,
                 inference_registry=None):
        self.resources = resource_manager or resources.ResourceManager()
        self.supervisor = supervisor_module
        self.state = state_module
        self.dependency_probe = dependency_probe or resources.probe_inference_service
        self.result_gateway_sock = (result_gateway_sock
                                    if result_gateway_sock is not None
                                    else paths.RESULT_GATEWAY_SOCK)
        self.inference_service_sock = (inference_service_sock
                                       if inference_service_sock is not None
                                       else paths.INFERENCE_SERVICE_SOCK)
        self.inference_registry = (
            inference_registry
            if inference_registry is not None
            else inference_auth.InferenceAuthorizationRegistry()
        )

    @staticmethod
    def load_manifest(app_id: str) -> dict:
        if not paths.valid_app_id(app_id):
            raise ValueError("invalid app id %r" % app_id)
        directory = paths.app_dir(app_id)
        if not os.path.isdir(directory):
            raise ValueError("app not installed: %s" % app_id)
        with open(os.path.join(directory, "manifest.json")) as f:
            manifest = json.load(f)
        if not isinstance(manifest, dict):
            raise CoordinatorError("manifest must be an object")
        # The installer/manifest validator owns rejection; keeping the effective
        # id here prevents a missing id from producing app.instance:unknown.
        manifest = dict(manifest)
        manifest.setdefault("id", app_id)
        return manifest

    def _has_run_record(self, app_id: str) -> bool:
        """Treat an unreadable persisted process identity as an active fence."""
        check = getattr(self.supervisor, "has_run_record", None)
        if not callable(check):
            return False
        try:
            return bool(check(app_id))
        except Exception:
            # Failure to inspect the only teardown identity cannot authorize a
            # release/restart.  A later explicit stop can diagnose it safely.
            return True

    def _on_spawn(self, app_id: str, instance_id: str, generation: int,
                  allocations: list, pid: int,
                  inference_policy: Optional[dict] = None) -> None:
        self.state.transition(
            app_id, "starting", pid=int(pid), pgid=int(pid),
            instance_id=instance_id, generation=int(generation),
            allocations=[a.get("allocation_id") for a in allocations],
            reason=None)
        if inference_policy is not None:
            # Use the same canonical resolver as the result gateway before
            # materialising the cross-process record.  A registry entry can
            # therefore never be broader than appmgr's live generation view.
            if self.resolve_identity(pid, app_id, instance_id, generation) is None:
                raise CoordinatorError("spawn identity was not committed")
            self.inference_registry.publish(
                inference_policy,
                pid=int(pid),
                instance_id=instance_id,
                generation=int(generation),
            )

    def start(self, app_id: str, *, manifest: Optional[dict] = None,
              operation: str = "start", launch: Optional[Callable[..., int]] = None,
              require_dependencies: bool = True,
              launch_mode: str = "managed",
              reset_restart_history: bool = False) -> dict:
        manifest = manifest or self.load_manifest(app_id)
        running = self.supervisor.is_running(app_id)
        current = self.state.get_app(app_id)
        if running is None and current and current.get("teardown_pending"):
            # Do not mint a new identity or reserve a second set of resources
            # while the previous generation's inference fence is uncertain.
            # Retaining the old pid/instance/generation lets a later stop retry
            # the exact revocation instead of losing the only safe handle.
            raise CoordinatorError(
                "previous generation teardown is incomplete for %s: %s" % (
                    app_id, current.get("reason") or "retry stop"
                )
            )
        if running is not None:
            # Idempotent max_instances=1: never spawn a second process for the
            # same app.  Reconstruct a minimal observation if this process came
            # from a legacy appmgr version and has no v2 record.
            if current and current.get("instance_id"):
                self.state.set_desired(app_id, state.DESIRED_RUNNING,
                                       version=manifest.get("version"))
                current = self.state.get_app(app_id) or current
            else:
                instance = "adopted-%s-%s" % (running, uuid.uuid4().hex[:12])
                current = self.state.begin_start(
                    app_id, instance, version=manifest.get("version"),
                    launch_mode="legacy")
            running_plan = resources.plan_manifest(
                manifest, appconfig.effective_values(manifest, app_id))
            # This is also the daemon-crash adoption path.  start() commits the
            # root-owned run record before on_spawn; if appmgr dies between
            # those operations, the replacement daemon sees a valid live PID
            # plus the durable instance/generation and must finish the commit
            # instead of leaving the gateway identity permanently unbound.
            bound = self.resources.bind(
                current["instance_id"], int(current["generation"]))
            live_allocations = (bound or self.resources.allocations_for(
                current["instance_id"]))
            allocation_ids = [item["allocation_id"] for item in live_allocations]
            if not allocation_ids:
                allocation_ids = list(current.get("allocations") or [])
            current = self.state.transition(
                app_id,
                "running",
                pid=int(running),
                pgid=int(running),
                reason=None,
                started_at=current.get("started_at") or time.time(),
                allocations=allocation_ids,
                endpoints={
                    "result": (self.result_gateway_sock
                               if running_plan.result_gateway else None),
                    "inference": (self.inference_service_sock
                                  if running_plan.npu_mode == "scheduled" else None),
                },
                # Do not infer a route for an adopted process.  Only a launch
                # performed by the current supervisor mints this field.
                frame_stream_contract=
                    supervisor.normalise_managed_frame_stream_contract(
                        current.get("frame_stream_contract")),
                blocked_resource=None,
                resource_owners=[],
                dependency=None,
                runtime_guard=None,
            )
            if running_plan.npu_mode == "scheduled":
                policy = self.inference_registry.prepare(app_id, manifest)
                self.inference_registry.publish(
                    policy,
                    pid=int(running),
                    instance_id=current["instance_id"],
                    generation=int(current["generation"]),
                )
            return self._result(app_id, current, running, idempotent=True)

        instance_id = uuid.uuid4().hex
        rec = self.state.begin_start(app_id, instance_id,
                                     version=manifest.get("version"),
                                     launch_mode=launch_mode,
                                     reset_restart_history=reset_restart_history)
        generation = int(rec["generation"])
        plan = resources.plan_manifest(
            manifest, appconfig.effective_values(manifest, app_id))
        frame_stream_contract = supervisor.managed_frame_stream_contract(
            plan.as_dict())

        # Scheduled NPU is a service dependency, not a claim on the direct
        # inference-control owner.  Until inferenced exists, keep the desired
        # application visible as waiting_dependency rather than silently falling
        # back to direct RKNN ownership.
        dependency = None
        if plan.npu_mode == "scheduled" and require_dependencies:
            dependency = self.dependency_probe(self.inference_service_sock)
            available = (dependency.get("available") if isinstance(dependency, dict)
                         else bool(dependency))
        else:
            available = True
        if not available:
            detail = ((dependency or {}).get("error")
                      if isinstance(dependency, dict) else None)
            reason = "scheduled inference service unavailable: %s%s" % (
                self.inference_service_sock, ": " + detail if detail else "")
            rec = self.state.transition(app_id, "waiting_dependency",
                                        reason=reason, resource_plan=plan.as_dict(),
                                        frame_stream_contract=frame_stream_contract,
                                        dependency=dependency)
            return self._result(app_id, rec, None, accepted=True)

        inference_policy = None
        if plan.npu_mode == "scheduled":
            try:
                inference_policy = self.inference_registry.prepare(app_id, manifest)
            except Exception as exc:
                self.state.transition(
                    app_id,
                    "failed",
                    reason=f"inference authorization failed: {exc}",
                    resource_plan=plan.as_dict(),
                )
                raise

        self.state.transition(app_id, "waiting_resource", reason=None,
                              blocked_resource=None, resource_owners=[],
                              dependency=None, runtime_guard=None,
                              resource_plan=plan.as_dict(),
                              frame_stream_contract=frame_stream_contract)
        try:
            allocations = self.resources.reserve(app_id, instance_id,
                                                 generation, plan)
        except resources.ResourceBusy as exc:
            rec = self.state.transition(
                app_id, "waiting_resource", reason=str(exc),
                blocked_resource=exc.resource, resource_owners=exc.owners,
                resource_plan=plan.as_dict())
            return self._result(app_id, rec, None, accepted=True)

        allocation_ids = [a["allocation_id"] for a in allocations]
        self.state.transition(app_id, "starting", reason=None,
                              blocked_resource=None, resource_owners=[],
                              dependency=None, runtime_guard=None,
                              allocations=allocation_ids,
                              resource_plan=plan.as_dict(),
                              frame_stream_contract=frame_stream_contract)
        spawned_pid = {"value": None}
        startup_revoke = {"complete": inference_policy is None, "error": None}

        def on_spawn(pid: int) -> None:
            spawned_pid["value"] = int(pid)
            self._on_spawn(
                app_id,
                instance_id,
                generation,
                allocations,
                int(pid),
                inference_policy,
            )

        def revoke_spawned_generation() -> None:
            """Fence a half-started scheduled generation before forced kill."""
            if startup_revoke["complete"]:
                return
            try:
                self.inference_registry.revoke(
                    spawned_pid["value"],
                    app_id=app_id,
                    instance_id=instance_id,
                    generation=generation,
                )
                startup_revoke["complete"] = True
                startup_revoke["error"] = None
            except Exception as exc:
                startup_revoke["error"] = exc
                raise

        kwargs = {
            "instance_id": instance_id,
            "instance_generation": generation,
            "result_gateway_sock": (self.result_gateway_sock
                                    if plan.result_gateway else None),
            "frame_stream_contract": frame_stream_contract,
            "npu_mode": plan.npu_mode,
            "inference_service_sock": (self.inference_service_sock
                                       if plan.npu_mode == "scheduled" else None),
            "on_spawn": on_spawn,
            "before_force_kill": revoke_spawned_generation,
        }
        try:
            if launch is None:
                pid = self.supervisor.start(app_id, **kwargs)
            else:
                pid = launch(**kwargs)
            bound = self.resources.bind(instance_id, generation)
            self.state.transition(app_id, "ready", pid=pid, pgid=pid,
                                  blocked_resource=None, resource_owners=[],
                                  dependency=None, runtime_guard=None,
                                  allocations=[a["allocation_id"] for a in bound])
            rec = self.state.transition(
                app_id, "running", pid=pid, pgid=pid, reason=None,
                blocked_resource=None, resource_owners=[],
                dependency=None, runtime_guard=None,
                started_at=time.time(), next_retry_at=None,
                allocations=[a["allocation_id"] for a in bound],
                endpoints={
                    "result": (self.result_gateway_sock
                               if plan.result_gateway else None),
                    "inference": (self.inference_service_sock
                                  if plan.npu_mode == "scheduled" else None),
                })
            return self._result(app_id, rec, pid)
        except Exception as exc:
            # A cooperative startup failure reaches here after the child has
            # exited; a forced startup cleanup invokes the same callback before
            # SIGKILL.  Retry once here in case the pre-kill filesystem write
            # failed transiently.
            try:
                revoke_spawned_generation()
            except Exception:
                pass
            process_fenced = bool(
                getattr(exc, "process_fence_active", False)
                or self._has_run_record(app_id))
            if process_fenced:
                # The leader may have exited while an authenticated helper
                # remains in its group.  Preserve this exact generation and all
                # reservations; releasing either would allow an overlapping
                # replacement while the old native/camera work is still live.
                reason = "%s failed with process fence: %s" % (operation, exc)
                if startup_revoke["error"] is not None:
                    reason += "; inference authorization revoke failed: %s" % (
                        startup_revoke["error"])
                fence_pid = (spawned_pid["value"]
                             or getattr(exc, "pid", None))
                fence_pgid = getattr(exc, "pgid", None) or fence_pid
                fields = {
                    "reason": reason,
                    "teardown_pending": True,
                    "allocations": allocation_ids,
                }
                if isinstance(fence_pid, int) and not isinstance(fence_pid, bool):
                    fields["pid"] = fence_pid
                if isinstance(fence_pgid, int) and not isinstance(fence_pgid, bool):
                    fields["pgid"] = fence_pgid
                self.state.transition(app_id, "stopping", **fields)
                if startup_revoke["error"] is not None and hasattr(exc, "add_note"):
                    exc.add_note(
                        "inference authorization revoke also failed: %s" %
                        startup_revoke["error"])
                raise
            if startup_revoke["error"] is not None:
                reason = (
                    "%s failed: %s; inference authorization revoke failed: %s"
                    % (operation, exc, startup_revoke["error"])
                )
                self.state.transition(
                    app_id, "failed", reason=reason, teardown_pending=True,
                    frame_stream_contract={"id": "", "kind": "none"})
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        "inference authorization revoke failed; previous "
                        "generation resources retained: %s"
                        % startup_revoke["error"]
                    )
                raise
            self.resources.release(instance_id, generation)
            self.state.transition(
                app_id, "failed", pid=None, pgid=None, allocations=[],
                teardown_pending=False,
                frame_stream_contract={"id": "", "kind": "none"},
                reason="%s failed: %s" % (operation, exc))
            # Preserve the existing exception type/API contract for callers.
            raise

    def stop(self, app_id: str, *, desired: str = state.DESIRED_STOPPED) -> dict:
        if not paths.valid_app_id(app_id):
            raise ValueError("invalid app id %r" % app_id)
        rec = self.state.get_app(app_id)
        if rec is None:
            self.state.set_desired(app_id, desired)
            rec = self.state.get_app(app_id)
        else:
            self.state.set_desired(app_id, desired)
        self.state.transition(app_id, "stopping", reason=None)
        revoke_error = None
        revoke_complete = False

        def revoke_generation() -> None:
            """Fence exactly the generation captured before stop began.

            The supervisor invokes this immediately before a forced SIGKILL.
            On a cooperative TERM exit it is invoked only after stop() returns,
            so the app may finish its authenticated remote-model unloads first.
            Registry identity checks keep this callback from touching a newer
            PID/instance/generation if lifecycle state changes unexpectedly.
            """
            nonlocal revoke_complete, revoke_error
            if revoke_complete:
                return
            try:
                self.inference_registry.revoke(
                    rec.get("pid"),
                    app_id=app_id,
                    instance_id=rec.get("instance_id"),
                    generation=int(rec.get("generation", 0)),
                )
                revoke_complete = True
                revoke_error = None
            except Exception as exc:
                # A pre-KILL attempt must not prevent containment.  Leave the
                # operation incomplete so the post-stop call retries once.
                revoke_error = exc

        stop_error = None
        detail = None
        try:
            detail = self.supervisor.stop(
                app_id, before_force_kill=revoke_generation)
        except Exception as exc:
            # An unexpected supervisor failure is an abnormal-stop path: revoke
            # immediately, but retain allocations/state because the process may
            # still be alive and must not overlap a replacement generation.
            stop_error = exc
        finally:
            revoke_generation()
        if stop_error is not None:
            # The supervisor could not establish that the old process group is
            # gone.  Keep its allocation and identity even when authorization
            # revocation succeeded: a replacement must not overlap uncertain
            # native/camera resources.
            reason = "stop failed: %s" % stop_error
            if revoke_error is not None:
                reason += "; inference authorization revoke failed: %s" % revoke_error
            self.state.transition(
                app_id, "stopping", reason=reason, teardown_pending=True)
            if revoke_error is not None and hasattr(stop_error, "add_note"):
                stop_error.add_note(
                    "inference authorization revoke also failed: %s"
                    % revoke_error)
            raise stop_error
        if revoke_error is not None:
            # The process is gone, so /proc validation already fences its live
            # capability.  Nevertheless retain the exact old generation and
            # every reservation until registry cleanup succeeds; otherwise a
            # new generation could overlap a stale authorization record.
            self.state.transition(
                app_id, "stopping", teardown_pending=True,
                frame_stream_contract={"id": "", "kind": "none"},
                reason="inference authorization revoke failed: %s" % revoke_error)
            raise CoordinatorError(
                "application stopped but inference authorization revoke failed; "
                "previous generation retained for retry: %s" % revoke_error
            )
        if rec.get("instance_id"):
            self.resources.release(rec["instance_id"], int(rec.get("generation", 0)))
        stopped = self.state.transition(app_id, "stopped", pid=None, pgid=None,
                                        allocations=[], reason=None, endpoints={},
                                        blocked_resource=None,
                                        resource_owners=[], dependency=None,
                                        runtime_guard=None,
                                        frame_stream_contract={
                                            "id": "", "kind": "none"},
                                        teardown_pending=False,
                                        restart_history=[], next_retry_at=None,
                                        started_at=None)
        return {"stopped": app_id, "detail": detail,
                "desired_state": stopped["desired_state"],
                "observed_state": stopped["observed_state"]}

    def restart(self, app_id: str, *, manifest: Optional[dict] = None,
                launch: Optional[Callable[..., int]] = None,
                launch_mode: str = "managed") -> dict:
        manifest = manifest or self.load_manifest(app_id)
        self.stop(app_id)
        return self.start(app_id, manifest=manifest, operation="restart",
                          launch=launch, launch_mode=launch_mode)

    def observe(self, app_id: str, pid: Optional[int], last_exit=None) -> Optional[dict]:
        """Refresh one observed state from supervisor liveness without spawning."""
        rec = self.state.get_app(app_id)
        if rec is None:
            return None
        if pid is None and self._has_run_record(app_id):
            # A dead leader is not equivalent to an empty process group.  The
            # supervisor deliberately retains this identity while a trusted
            # same-boot helper survives containment; reads must not release its
            # reservations or erase the exact generation needed by stop retry.
            if (rec.get("observed_state") != "stopping"
                    or not rec.get("teardown_pending")):
                rec = self.state.transition(
                    app_id, "stopping", teardown_pending=True,
                    reason=(rec.get("reason")
                            or "persisted process fence remains active"))
            return rec
        if pid is not None:
            live_phases = ("starting", "ready", "running", "degraded")
            if rec.get("observed_state") in live_phases:
                if rec.get("pid") != pid or rec.get("pgid") != pid:
                    # is_running() authenticated the root-owned run.pid against
                    # /proc.  Backfill a commit interrupted after the run files
                    # but before on_spawn; keep the phase so an ordinary read
                    # does not pretend READY was reached.
                    rec = self.state.transition(
                        app_id, rec.get("observed_state"), pid=pid, pgid=pid)
            else:
                rec = self.state.transition(app_id, "running", pid=pid, pgid=pid,
                                            reason=None)
            return rec
        # ``starting`` is published before Popen so desired state and resource
        # admission are durable.  Until the on_spawn callback binds a concrete
        # PID, ``is_running() is None`` means "spawn identity not committed",
        # not "the process exited".  Treating this short transaction window as
        # a crash used to let a concurrent GET /apps release allocations and
        # clear the identity just before GatewayResultSink connected.
        if rec.get("observed_state") == "starting" and rec.get("pid") is None:
            return rec
        if rec.get("observed_state") in ("starting", "ready", "running", "degraded"):
            try:
                self.inference_registry.revoke(
                    rec.get("pid"),
                    app_id=app_id,
                    instance_id=rec.get("instance_id"),
                    generation=int(rec.get("generation", 0)),
                )
            except Exception as exc:
                self.state.transition(
                    app_id, "failed", teardown_pending=True,
                    frame_stream_contract={"id": "", "kind": "none"},
                    reason="process exited; inference authorization revoke failed: %s"
                    % exc,
                    exited_at=time.time(), last_exit=last_exit,
                    started_at=None)
                raise CoordinatorError(
                    "crashed generation authorization revoke failed: %s" % exc
                ) from exc
            if rec.get("instance_id"):
                self.resources.release(rec["instance_id"],
                                       int(rec.get("generation", 0)))
            target = ("failed" if rec.get("desired_state") == state.DESIRED_RUNNING
                      else "stopped")
            reason = "process exited"
            if isinstance(last_exit, dict):
                reason = "process exited: %s" % last_exit
            rec = self.state.transition(app_id, target, pid=None, pgid=None,
                                        allocations=[], reason=reason,
                                        blocked_resource=None,
                                        resource_owners=[], dependency=None,
                                        runtime_guard=None,
                                        frame_stream_contract={
                                            "id": "", "kind": "none"},
                                        teardown_pending=False,
                                        exited_at=time.time(), last_exit=last_exit,
                                        started_at=None)
        return rec

    @staticmethod
    def _restart_policy(manifest: dict) -> dict:
        health = manifest.get("health") if isinstance(manifest, dict) else None
        restart = (health or {}).get("restart") if isinstance(health, dict) else None
        if not isinstance(restart, dict):
            return {"policy": "never", "max_attempts": 0,
                    "window_sec": 300, "backoff_sec": []}
        return {
            "policy": restart.get("policy", "never"),
            "max_attempts": max(0, int(restart.get("max_attempts", 0))),
            "window_sec": max(1, int(restart.get("window_sec", 300))),
            "backoff_sec": [max(0, int(value)) for value in
                            (restart.get("backoff_sec") or [])],
        }

    def reconcile_one(self, app_id: str, *, manifest: dict,
                      launch: Callable[..., int], now: Optional[float] = None,
                      retry_interval: float = 1.0) -> dict:
        """Advance one desired-running managed app without relying on HTTP GET.

        A process crash releases only that generation's reservations.  Failed
        starts consume the manifest's bounded restart budget; dependency and
        resource waits do not.  A stable running interval clears old attempts.
        """
        now = time.time() if now is None else float(now)
        rec = self.state.get_app(app_id)
        if rec is None:
            return {"id": app_id, "action": "absent"}
        running = self.supervisor.is_running(app_id)
        if running is None and self._has_run_record(app_id):
            # sweep_stale/drain_exits deliberately retain a same-boot record
            # while its process group survives SIGKILL.  Do not reinterpret a
            # dead leader as a normal crash and release/start over its helpers.
            if (rec.get("observed_state") != "stopping"
                    or not rec.get("teardown_pending")):
                rec = self.state.transition(
                    app_id, "stopping", teardown_pending=True,
                    reason=(rec.get("reason")
                            or "persisted process fence remains active"))
            return {
                "id": app_id,
                "action": "teardown_pending",
                "observed_state": rec.get("observed_state"),
                "reason": rec.get("reason"),
            }
        exit_info = self.supervisor.last_exit(app_id)
        if (isinstance(exit_info, dict) and rec.get("pid") is not None
                and exit_info.get("pid") is not None
                and int(exit_info.get("pid")) != int(rec.get("pid"))):
            # last_exit describes an older generation, not this stale durable
            # run record (the file intentionally survives successful restarts).
            exit_info = None
        if running is not None:
            rec = self.observe(app_id, running, exit_info)
            violation = self.resources.runtime_guard()
            if violation is not None:
                # Preserve desired=running so the existing waiting-resource
                # reconciler can restore this app only after the lower start
                # threshold is satisfied.  Stop/release the exact generation
                # first; a hot process must not retain camera/NPU/model leases.
                self.stop(app_id, desired=state.DESIRED_RUNNING)
                rec = self.state.transition(
                    app_id, "waiting_resource",
                    pid=None, pgid=None, allocations=[],
                    frame_stream_contract={"id": "", "kind": "none"},
                    blocked_resource=violation["resource"],
                    resource_owners=[], reason=violation["message"],
                    runtime_guard=violation,
                )
                return {
                    "id": app_id,
                    "action": "safety_stop",
                    "observed_state": rec.get("observed_state"),
                    "reason": rec.get("reason"),
                    "runtime_guard": violation,
                }
            stabilization = max(0, int(
                ((manifest.get("health") or {}).get("stabilization_sec", 0))))
            started = rec.get("started_at") if rec else None
            history = list((rec or {}).get("restart_history") or [])
            if history and isinstance(started, (int, float)) \
                    and now - float(started) >= stabilization:
                rec = self.state.transition(
                    app_id, "running", restart_history=[],
                    next_retry_at=None, reason=None)
            return {"id": app_id, "action": "healthy", "pid": running,
                    "observed_state": (rec or {}).get("observed_state")}

        # Publish a dead leader and release its exact generation before deciding
        # whether policy permits another one.
        previous_observed = rec.get("observed_state")
        rec = self.observe(app_id, None, exit_info) or rec
        if rec.get("desired_state") != state.DESIRED_RUNNING:
            return {"id": app_id, "action": "stopped",
                    "observed_state": rec.get("observed_state")}
        if rec.get("launch_mode") != "managed":
            return {"id": app_id, "action": "legacy-unmanaged",
                    "observed_state": rec.get("observed_state")}

        observed = rec.get("observed_state")
        updated = float(rec.get("updated_at") or 0)
        if observed in ("waiting_dependency", "waiting_resource"):
            if now - updated < max(0.05, float(retry_interval)):
                return {"id": app_id, "action": "waiting",
                        "observed_state": observed}
            try:
                return self.start(app_id, manifest=manifest,
                                  operation="reconcile_wait", launch=launch)
            except Exception as exc:
                return {"id": app_id, "action": "failed",
                        "observed_state": "failed", "reason": str(exc)}

        # A daemon restart can leave durable desired=running with stopped or
        # stale observed state.  Boot recovery is not a crash attempt and starts
        # immediately; the same branch makes the reconciler independently safe.
        if (observed in ("installed", "stopped", "preparing_env")
                or (previous_observed in ("starting", "ready", "running")
                    and not exit_info)):
            try:
                return self.start(app_id, manifest=manifest,
                                  operation="reconcile_restore", launch=launch)
            except Exception as exc:
                return {"id": app_id, "action": "failed",
                        "observed_state": "failed", "reason": str(exc)}

        policy = self._restart_policy(manifest)
        if policy["policy"] != "on-failure":
            return {"id": app_id, "action": "restart-disabled",
                    "observed_state": observed}
        history = [float(value) for value in rec.get("restart_history") or []
                   if isinstance(value, (int, float))
                   and now - float(value) <= policy["window_sec"]]
        if len(history) >= policy["max_attempts"]:
            rec = self.state.transition(
                app_id, "crash_loop", restart_history=history,
                next_retry_at=None,
                reason="restart budget exhausted: %d attempts in %ds" %
                (len(history), policy["window_sec"]))
            return {"id": app_id, "action": "crash_loop",
                    "observed_state": rec["observed_state"],
                    "reason": rec.get("reason")}

        due = rec.get("next_retry_at")
        if not isinstance(due, (int, float)):
            delays = policy["backoff_sec"]
            delay = delays[min(len(history), len(delays) - 1)] if delays else 0
            due = now + delay
            rec = self.state.transition(
                app_id, "backoff", restart_history=history,
                next_retry_at=due,
                reason="restart attempt %d/%d in %ds" %
                (len(history) + 1, policy["max_attempts"], delay))
        if now < float(due):
            return {"id": app_id, "action": "backoff",
                    "observed_state": "backoff", "next_retry_at": due}

        attempt_history = history + [now]
        try:
            result = self.start(app_id, manifest=manifest,
                                operation="auto_restart", launch=launch)
        except Exception as exc:
            rec = self.state.get_app(app_id) or {}
            self.state.transition(
                app_id, "failed", restart_history=attempt_history,
                next_retry_at=None,
                reason=rec.get("reason") or "auto restart failed: %s" % exc)
            return {"id": app_id, "action": "restart_failed",
                    "observed_state": "failed", "reason": str(exc)}
        if result.get("pid") is not None:
            rec = self.state.transition(
                app_id, "running", restart_history=attempt_history,
                next_retry_at=None, reason=None)
            return self._result(app_id, rec, result.get("pid"),
                                action="restarted")
        # Resource/dependency admission did not spawn a process and therefore
        # must not consume the crash retry budget.
        return dict(result, action="waiting")

    def resolve_identity(self, peer_pid: Optional[int], claimed_app: str,
                         instance_id: str, generation: int) -> Optional[dict]:
        """Canonicalise a gateway publisher against the committed run identity."""
        if peer_pid is None or not paths.valid_app_id(claimed_app):
            return None
        rec = self.state.get_app(claimed_app)
        if not rec:
            return None
        try:
            # The hello tuple is only a selector.  Authentication comes from
            # Linux SO_PEERCRED (peer_pid), the coordinator's exact generation,
            # and supervisor.is_running(), which validates the root-owned
            # run.pid against /proc cwd/cmdline before returning it.  Requiring
            # both records also makes the gateway fail closed during either
            # half of the two-file startup commit.
            supervised_pid = self.supervisor.is_running(claimed_app)
            matches = (
                int(rec.get("pid") or -1) == int(peer_pid)
                and int(supervised_pid or -1) == int(peer_pid)
                and rec.get("instance_id") == instance_id
                and int(rec.get("generation", -1)) == int(generation)
                and rec.get("observed_state") in
                    ("starting", "ready", "running", "degraded")
            )
        except (TypeError, ValueError):
            return None
        if not matches:
            return None
        return {
            "app_id": claimed_app,
            "instance_id": instance_id,
            "generation": int(generation),
            "pid": int(peer_pid),
        }

    def reconcile_allocations(self) -> list:
        live = []
        for app_id, rec in self.state.app_states().items():
            if (rec.get("instance_id")
                    and (self.supervisor.is_running(app_id) is not None
                         or self._has_run_record(app_id))):
                live.append(rec["instance_id"])
        return self.resources.reconcile(live)

    def inference_status(self) -> dict:
        value = self.dependency_probe(self.inference_service_sock)
        if isinstance(value, dict):
            return value
        return {"available": bool(value), "socket": self.inference_service_sock}

    @staticmethod
    def _result(app_id: str, rec: dict, pid: Optional[int], **extra) -> dict:
        out = {
            "id": app_id,
            "pid": pid,
            "instance_id": rec.get("instance_id"),
            "generation": rec.get("generation"),
            "desired_state": rec.get("desired_state"),
            "observed_state": rec.get("observed_state"),
            "reason": rec.get("reason"),
            "allocations": list(rec.get("allocations") or []),
            "endpoints": dict(rec.get("endpoints") or {}),
            "frame_stream_contract":
                supervisor.normalise_managed_frame_stream_contract(
                    rec.get("frame_stream_contract")),
        }
        out.update(extra)
        return out
