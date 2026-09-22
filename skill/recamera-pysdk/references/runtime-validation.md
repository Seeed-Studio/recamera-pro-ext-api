# Artifact validation and device delivery

Use this after writing the App and building its final archive. Keep these
stages separate: package integrity; host loader; controlled loop; model
numerical comparison; upload; install; lifecycle; result delivery; task quality.
Only report stages actually observed. None alone proves all the others.

## Host smoke test of the final archive

Run in a disposable host venv with the compatible SDK/Kit and its host
dependencies, using a complete pinned checkout as `--sdk-root`. This executes
trusted App source; a subprocess timeout is not a security sandbox. Do not run
unknown uploaded third-party code just to inspect its package.

```text
python scripts/smoke_app.py --archive <dist/app.tar.gz> --sdk-root <checkout> \
  --smoke-test <tests/app-smoke.py> --timeout 15 --report <dist/host-smoke.json>
```

The helper verifies the exact archive, extracts to a temporary directory,
loads it with real `kit.run.resolve_entry/load_app_module/find_app`, and calls
the external test hook `smoke(app)`. It terminates the subprocess group at the
deadline and removes temporary files. It never modifies the source or archive.
Without `--smoke-test`, only module import/entry selection/construction run;
`mock_loop` stays `not_run` and no camera/NPU lifecycle is started.

A detector hook should supply one controlled frame and prepared input,
replace model inference with **known valid raw output tensors**, intercept
`emit`, and run the actual App loop. Assert expected classes, scores and
source-image coordinates, including non-640 input, custom class count,
letterbox/padding and empty results. Do not replace the postprocessor being
tested with a stub that always returns the expected answer. CPU-only Apps may
call a naturally bounded `run()` directly. Keep hooks outside the App payload.

The helper extracts portable `*-none-any.whl` dependencies for the host test.
If any wheel is native to the target, it returns `not_run` (exit 2): import
validation must run on a compatible target. Host dependency/constructor
failures are reported, not disguised as device failures. The helper does not
install host substitutes or silently fall back to the developer's app source.
Dynamic imports/API calls require tests covering those branches; static AST
checks cannot prove all Python behavior.

## Device access and upload

For device delivery use the user's IP, SSH username and either a key or
password. Reuse session authorization and credentials; never place a password
in App source, a manifest, command-line arguments, docs or a report. Hosts
need OpenSSH; password mode additionally needs `sshpass` (Linux/POSIX host).
The device needs Python 3 and writable `/userdata/appstage` space.

```text
python scripts/deploy_app.py --archive <dist/app.tar.gz> \
  --host <device-ip> --user <ssh-user> --ask-password \
  --report <dist/device-upload.json>
```

For agent/noninteractive use, securely populate an environment variable and
pass only its **name** with `--password-env RECAMERA_SSH_PASSWORD`. The helper
removes that variable from the child environment and sends the password via
an anonymous `sshpass -d` pipe. Do not paste the password into an exported
shell command that is logged. Omit both options for SSH key/agent auth.

SSH strictly verifies existing host keys. If the host is unknown or its key
changed, verify the fingerprint through a trusted channel first; a changed
key is not automatically trusted because a device was reflashed. Supply a
separate verified file via `--known-hosts <trusted-file>` if appropriate. Do
not disable host verification or overwrite unrelated global SSH records.

Upload is the default action. It snapshots and validates the local archive,
streams to a new private `/userdata/appstage/recamera-skill-*/app.tar.gz`, and
checks size and SHA-256 on the device. The report contains the remote path and
digest. Interrupted/incomplete or hash-mismatched receives remove their own
staging directory. A successful upload leaves the package there for the user;
it does **not** unpack, install or launch anything. Remove that exact staging
directory when no longer needed. Abrupt device power loss can leave a partial
staging directory; never clean unrelated paths automatically.

## Optional AppMgr installation and verification

Choose an action matching the user's request; existing session authorization
is enough. Installation grants the exact permissions of the reviewed manifest.
Check they match the requested App behavior before selecting install/verify.

```text
python scripts/deploy_app.py --archive <dist/app.tar.gz> \
  --host <device-ip> --user <ssh-user> --ask-password --action install \
  --report <dist/device-install.json>

python scripts/deploy_app.py --archive <dist/app.tar.gz> \
  --host <device-ip> --user <ssh-user> --ask-password --action verify \
  --observe-seconds 10 --timeout 180 --report <dist/device-verify.json>
```

Each invocation uploads its supplied archive; `install` and `verify` do not
resume a previous upload receipt. If this App ID is already installed, select
`--replace` only when replacement is in scope (including installation followed
by a later verify invocation). AppMgr preserves its normal upgrade/rollback
rules. The tool refuses built-in/system IDs and never stops other Apps to
resolve resource conflicts. The existing App can be stopped by an authorized
upgrade/reinstall; `install` leaves startup to AppMgr's policy (unsigned local
uploads normally remain stopped).

Installation goes through **device nginx**, with matching Host and Origin, to
`/api/app-center/v1/`. Before sending any upload or mutation, a read-only policy
request detects the firmware's HTTP-to-HTTPS redirect. Only a redirect to the
same loopback host and policy path on HTTPS port 443 is accepted; mutations
are never automatically replayed after a redirect. HTTPS and WSS verify the
certificate chain/validity using the device's public
`/userdata/config/system/ssl/server.crt`, read inside the authenticated SSH
session, and pin the exact peer certificate before sending application data.
The certificate's name can differ from `127.0.0.1`; the exact certificate pin
replaces hostname matching only for this local connection. Plain HTTP firmware
continues to use port 80. The documented integrated firmware supports
localhost control authentication, so the authenticated SSH worker can use this
edge without needing the user's Web password. SSH and Web credentials are not
assumed identical. If the device returns 401/403 or lacks these endpoints, keep
the uploaded package and use the authenticated Web App Center; report the
blocked stage. Never fall back to direct port 8130 uploads, forge the trusted
edge header, turn on developer mode or weaken signature policy.

The worker reads `/policy`, streams multipart `package` to `/uploads`, matches
preflight manifest/release identity with the verified archive, confirms exactly
that manifest's permissions and authorized replacement flags, then installs via
`POST /apps`. It polls the exact operation ID/type/App ID to a terminal state
and reads installed `manifest.json`/`release.lock.json` from the integrated
`/userdata/local/apps/<id>` layout. It does not compare UI-enriched manifest
fields from the app list with the original file. On success it removes only
its own SSH staging copy; AppMgr owns its upload/extraction/environment.

`verify` adds this sequence for the target App only:

1. Start and wait for actual `ready`/`running`; resource/dependency waits, a
   PID alone or `degraded` do not pass.
2. Observe health and canonical results for the requested interval.
3. Stop and confirm stopped; start again and observe a new instance.
4. Restart and observe another new instance/generation.
5. Stop by default, or keep running with `--leave-running` when desired.

Results use `/ws/ai/results/v2` with a raw subscription for this App. At least
two distinct result sequences from the exact current App/instance/generation
are required in **each** observation; an old result replay cannot pass. Only
message counts/types/identity are retained, not image/result payloads. For
Apps that intentionally emit rarely or use only GPIO/audio, this generic
result check may stay `unverified`; validate their intended output separately.
Missing the canonical endpoint also leaves result delivery unverified even if
lifecycle passes. Longer runs for leaks/performance require a separate soak
test; a 10-second observation cannot establish day-long stability.

Reports include preflight, operation IDs, final App state and bounded error
log tails. Keep reports private because app logs/configuration may be sensitive.
Timeout/lost SSH response can mean the server accepted a mutation: inspect
recorded operations and device state before retrying. The tool never blindly
retries mutations or issues conflicting cleanup after an uncertain operation.
A failed verification can leave the selected App installed/running; inspect
the final state and stop it through AppMgr when safe. It does not uninstall
the user's App or automatically roll back a successful installation.

Exit 0 means the requested automated stages passed; exit 1 means an error or
uncertain operation; exit 2 means runtime/result verification remains
unverified. `task_quality` stays `not_run`: separately use a known scene/sample
to verify real model loading, color, expected detections/coordinates and each
requested overlay/recording/audio/GPIO behavior. Record those observations in
the final delivery note rather than claiming the generic helper proved them.
