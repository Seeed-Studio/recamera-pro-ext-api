# Local observation task playbook (S01–S07)

Read this playbook whenever the user asks to inspect the current scene, watch for something, receive a later alert, query observation history, or modify or cancel a watch. It is a set of design/configuration templates, **not a bundled persistent watcher**.

`recamera-local` exposes local capture, model, recording-rule, event, file, and notification primitives. It does not itself provide observation-task CRUD, a relative-time scheduler, durable task history, or business-delivery retries.

## 1. Keep the main loop out of the chat

Use a deterministic external program as the system skeleton. The device should own supported inference and evidence recording; a Python runner should own scheduling, state transitions, checkpoints, task IDs, dedupe, persistence, cancellation, and retries. An LLM/VLM, when approved, should only interpret a bounded input and return a validated result.

| Layer | Appropriate responsibility | Boundary |
| --- | --- | --- |
| Device rules | Positive class/ROI matches and evidence files | No native absence, entry/exit edge, exact dwell, or task CRUD |
| External runner | Main loop, task state, timers, history, notification decisions | Cannot claim observations it did not obtain |
| LLM/VLM | One image/question with a strict output schema | Never owns timing, persistence, or delivery success |

Do not create an open-ended conversation polling loop. If no persistent runner or agent scheduler exists, offer a fresh one-shot inspection or a bounded check while the agent is demonstrably active. Persistent recording rules can collect evidence after user approval, but without a runner/notification path they do not justify “I will alert you later”; the local event buffer is volatile.

## 2. Discover and route before writes

### Classify the request

- **Look now/again (S01):** call `capture_image`; do not alter detection rules.
- **Watch/alert for a period (S02–S06):** create an explicit external task contract, then compile only the device-supported evidence portion.
- **Business integration (S07):** additionally require durable delivery state and idempotent retry behavior.
- **Query/modify/cancel:** resolve the external task ID and generation; a recording-rule name alone is not a task manager.

### Confirm target and ROI

Capture a fresh reference and open its returned local path with the available image/vision tool. Resolve “this parcel”, “that sofa”, and the region boundary against that image. If several targets or boundaries remain plausible, ask a short question. Save the confirmed normalized polygon; never copy an example polygon without confirming it on the user's image.

### Inspect actual capabilities

Start with `rcl get_status`. For observation tasks, inspect only the relevant additional state:

```bash
rcl get_detection_models_info
rcl get_detection_model
rcl get_record_sources
rcl list_apps
rcl get_record_trigger
rcl get_record_config
rcl get_detection_schedule
rcl get_storage_status
rcl get_resource_info
```

Read current configuration before replacing it. Only one record trigger and one built-in vision model are active at a time. A new task can therefore break an existing one; disclose the impact and obtain permission before changing shared model, rules, schedule, writer, storage, or notify configuration. `export_device_config` can snapshot configuration before approved changes.

Use exact, case-sensitive labels reported by the active model/source. Never assume support for a generic parcel, cardboard box, specific identity, or plate OCR. Vehicle detection is not OCR; an ROI match is not an entry event; a class match is not identity recognition.

### Missing model/capability: ask in non-technical terms

Do not silently upload images or turn the request into model training. Ask:

> 现有模型不能直接识别“这个目标”。您更希望：1）允许上传快照并尝试在线视觉服务（较快，但需联网，图片会离开设备）；2）查找现成的兼容模型或应用（可能离线运行，但需验证）；3）把需求简化为现有模型能识别的目标；还是 4）先暂停？

Recommend according to privacy, offline, provider, cost, and latency constraints. Online VLM use requires explicit image-upload approval and a real provider. Otherwise search the App Center/model store or, with approval, sources such as <https://github.com/airockchip/rknn_model_zoo> and <https://github.com/airockchip/rknn-llm>. A search hit is not compatibility: verify RV1126B/RKNN support, runtime, labels, ROI, resources, and license before any authorized download/install. `rknn-llm` does not imply a ready detector or OCR app. Prefer existing models; do not propose an impromptu training pipeline.

For a new on-device state/zone/tracking/OCR pipeline, hand off to `recamera-pysdk` for a separate capability assessment and validated app implementation with an existing compatible model.

## 3. Persist an external task contract

Before activation, show the user: target, ROI/reference, observation window, requested and actual cadence, trigger and reset conditions, notification channel, device changes, and known limitations. Obtain permission for shared configuration writes.

The external runner should persist:

| Field group | Required content |
| --- | --- |
| Task | ID, generation, owner, `pending/running/paused/failed/completed/cancelled` |
| Goal | Target, exact labels or approved VLM route, ROI and reference evidence |
| Timing | Absolute start/end or recurring schedule, requested/actual cadence |
| Condition | Confirmation count/duration, unknown policy, re-arm/reset rule |
| Delivery | Real channel, stable event ID, attachments, dedupe and retry policy |
| Health | Last successful observation, last event, error, observation gaps |
| Evidence | Local image/file paths, event/observation history, capture timestamps |
| Device diff | Previous and replacement model/trigger/rules/schedule/writer/storage |

A weekly detection schedule is not a one-shot “next 20 minutes” timer; the runner enforces relative expiration. “Running” requires a live runner and a successful initial observation/read-back, not merely a successful rule write.

## 4. Preserve primitive semantics

### Rules and events

`set_detection_rules` installs positive-match `inference_set` recording rules and normally enables JPG/storage. It does not directly express disappeared, returned, entered, exited, exact dwell, recovered, identity, or cancelled.

- `region_filter` filters supported source results by normalized ROI. It does not detect a crossing by itself.
- `debounce_times` counts matching frames/hops, not seconds. Do not turn it into an exact dwell threshold without measured timing and scene validation.
- `wait_event` is the low-latency local evidence wait. Feed each returned `watermark_unix_ms` back as `since_unix_ms`; never recompute it. Persist the resulting observations externally. This does not make history durable across buffer expiry, restart, or a stalled consumer.
- The daemon normally retains only a bounded recent view (default 500 visible results for 100 seconds, with separate recording-association limits). Query promptly and drain/persist what the task needs. Empty output means no retained matching row, not proof that nothing happened.
- `snapshot_path` is a local path: open it directly or use `read_file`. Do not treat it as a permanent historical archive after storage rotation.

Keep **positive**, **negative**, **unknown**, **capture failure**, and **no retained event** distinct. Occlusion, blur, and ambiguous model output are unknown; device/capture failure creates no observation. Neither a gap nor an empty event query proves departure, absence, recovery, or continuing dwell.

For an approved VLM call, supply one fresh relevant image and require a small result such as:

```json
{
  "region_visible": true,
  "target": "visible",
  "reason": "short factual description"
}
```

Allowed `target` values are `visible`, `not_visible`, and `uncertain`. Code attaches image ID/time and validates this schema. Code, not the VLM, owns state transitions, durations, dedupe, and notification. `not_visible` counts as an absence candidate only when the region is visible and evaluable. Do not treat a generated confidence value as calibrated accuracy.

### Notifications

A chat response is not a background notification channel. `set_notify_config` controls the shared MQTT/HTTP/UART inference-result pipeline; it does not add task state, durable event IDs, business acknowledgement, or retries. Applying it briefly restarts the result pipeline. Preserve redacted secrets and never write `***` back.

## 5. Scenario recipes

| Scenario | Local evidence route | External deterministic work |
| --- | --- | --- |
| S01 current view | Fresh `capture_image`, open local path | Bind response to new capture/time; admit uncertainty |
| S02 parcel removed | Timed fresh images + approved detector/VLM | Absence/return state, 30-minute expiry, one alert |
| S03 person in region | Person model + ROI rule + `wait_event` | Entry/dwell/exit state if promised; event persistence |
| S04 pet in forbidden region | Cat/dog model + ROI rule + events | Inside/outside state and valid 30-second reset |
| S05 vehicle + plate | Approved vehicle and separate OCR route | Exact match, arrival dedupe, absolute time window |
| S06 sustained obstruction | Timed fresh images + approved detector/VLM | Occupancy/recovery state over valid observations |
| S07 business record | Persisted observation + test HTTP integration | Idempotency, delivery ledger, retry/reconciliation |

### S01 — Current view and follow-up

Run `rcl capture_image` for every “now”, “again”, or “I moved it” request and open the newly returned path. Include capture time and resolve references against that image. If a label is unreadable, state the visual limitation; if capture fails, report failure and never present an older path as current. No recording-rule changes are needed.

### S02 — Parcel removed/returned

Initial experimental contract: one unique parcel and ROI, 30 minutes, one fresh check every 5 seconds, two consecutive evaluable absence observations, one notification. Report the real achieved cadence.

Use `PRESENT -> ABSENCE_CANDIDATE -> ABSENT`, optionally followed by `ABSENT -> PRESENT` after the user modifies the request. Unknown frames follow a declared pause/reset policy and never directly confirm removal. Attach before/after evidence, last-seen time, and first-confirmed-absent time; the exact removal instant is unknown. Notify once per transition/generation.

Do not substitute a person rule for parcel detection and do not infer removal from missing positive events. Without a suitable installed model, obtain the user's choice of approved VLM, compatible model/app search, or a narrower goal.

### S03 — Person enters/stays in ROI

Prefer the existing evidence path when inventory reports a model with the exact `person` label and a source with ROI support. The following is an illustrative configuration template only; replace both model and polygon with verified, user-approved values.

```bash
# Verified preconditions: yolo11n is installed with label "person";
# the user approved the model/rule change and this example ROI.
rcl set_detection_model model_name=yolo11n
rcl set_detection_rules 'rules=[{"name":"s03-person-region","debounce_times":3,"confidence_range_filter":[0.25,1.0],"label_filter":["person"],"source_filter":["builtin"],"region_filter":[[[0.1,0.6],[0.9,0.6],[0.9,0.95],[0.1,0.95]]]}]'
rcl get_detection_model
rcl get_detection_rules
rcl get_record_trigger

# The external runner can wait for positive evidence and persist it.
rcl wait_event timeout_s=60
# Next call passes the returned watermark verbatim:
rcl wait_event timeout_s=60 since_unix_ms=<returned-watermark>
```

This device layer proves **person detected in ROI**, not entry/exit/dwell. For entry, require a valid outside/absent baseline followed by a valid inside observation. For dwell, use valid observation times and an explicit gap policy. For exit/re-entry, require valid negative observations; missing positive events alone cannot prove exit. Reset only under the confirmed outside condition to prevent continuous-presence notification spam.

### S04 — Cat/dog in a forbidden region

Reuse S03 only for an actually reported `cat`/`dog` label and a confirmed ROI. Appearance in the full frame is not presence on the sofa. For “a new event only after 30 seconds away”, require valid negative observations under the declared reset policy; an acquisition outage is not 30 seconds away. Do not claim to distinguish similar individual animals or infer health/emotion.

### S05 — Vehicle arrival and plate record

Do not activate a vehicle-only rule and call it plate recognition. First obtain approval for a compatible OCR route and validate optics, angle, lighting, and the static/low-speed scope. Keep original image, plate crop if available, raw OCR output, exact-match decision, region, and time. Blur, occlusion, conflicts, and similar characters yield `unknown`, never fuzzy success. Deduplicate an arrival and enforce the absolute window externally. Printed-plate and real-car results are separate; neither proves night/high-speed support or authorizes opening a gate.

### S06 — Passage occupied, then recovered

Initial experimental contract: inspect every 10 seconds, alert after 60 seconds of valid continuous occupancy, then one recovery alert after confirmed clear. First establish that the selected model can detect the target object; a passing person is not box occupancy.

Use `CLEAR -> OCCUPY_CANDIDATE -> OCCUPIED -> RECOVERY_CANDIDATE -> CLEAR` and merge repeated samples into one episode. Save start, first threshold confirmation, recovery, and evidence. Unknown frames cannot prove continued occupancy or recovery; follow the declared pause/reset policy and mark gaps. Cadence bounds time precision. This is inspection assistance, not a certified safety alarm.

### S07 — Business-system delivery

Persist the visual event separately from delivery. The observation needs a stable event ID, task ID/generation, judgment status, and evidence. Delivery needs `pending/succeeded/failed/unknown`, attempts, last error/response, and next retry. Retry with the same idempotency key and bounded backoff. A timeout is unknown, not success; reconcile where possible and never blindly create a duplicate. Use an approved test HTTP endpoint first. The shared notify pipeline can transport inference results but does not replace this ledger.

## 6. Lifecycle and truthfulness

- **Modify:** identify the task, increment generation, apply the new contract, and suppress late notifications from the old generation.
- **Cancel:** stop new checks, cancel/await in-flight work, and invalidate late delivery. Ask separately before disabling/restoring shared device rules; cancelling the runner does not change device configuration by itself.
- **Restart/outage:** report whether runner and rules resumed and mark the unobserved interval as a gap. Never backfill imagined events.
- **History:** use persisted observations/files only. Distinguish not observed, unknown, capture failed, and unavailable/expired history.
- **Capacity:** begin with one task per device; measure actual cadence, latency, CPU/NPU/memory/storage, and model calls before parallelism. Reject or reduce cadence visibly instead of silently dropping checks.

## 7. Acceptance gate

Read-back is not scene acceptance. Before saying the task works:

1. Read back model, trigger, rules, writer, schedule, and storage as applicable.
2. Confirm the runner is alive and one fresh observation succeeded.
3. With permission, run controlled positive and negative scenes: outside ROI, brief occlusion, continuous presence, and target absence as relevant.
4. Check evidence, one notification per episode, actual cadence/latency, timestamps, and reset/re-arm behavior.
5. Exercise query, modification, cancellation with an in-flight result, expiration, restart, and network/camera interruption.
6. For initial acceptance, record 10 normal and 10 interference/negative cases, plus a 30-minute soak for sustained tasks. Track hits, misses, false alerts, duplicates, event-to-notification latency, creation time, model calls, resource use, and failures. Do not market this small regression as an accuracy benchmark.

Report **configured**, **runner verified**, **controlled scene passed**, **delivery acknowledged**, and **not tested** separately. If deterministic local primitives cannot support the promised semantics, implement an approved Python runner or assess `recamera-pysdk`; otherwise narrow the request rather than letting the chat become an unreliable permanent main loop.
