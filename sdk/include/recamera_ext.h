// Copyright 2025 reCamera Pro Extension API
// librecamera_ext C ABI v1 (spec §3.5, §2.5) -- result-injection sink +
// frame-source receiver. soname: librecamera_ext.so.1
#ifndef RECAMERA_EXT_H
#define RECAMERA_EXT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// ===========================================================================
// M1 result injection (spec §3.5)
// ===========================================================================

// Opaque result-sink handle.
typedef struct rc_ext_result rc_ext_result_t;

// A single detection box. Coordinates are normalized [0,1] (top-left x1/y1 /
// bottom-right x2/y2, as a fraction of frame width/height; e.g. 0.5 = center).
// The OSD renderer clamps to [0,1] and multiplies by frame width/height, so
// pixel-valued coordinates collapse to a 1px box -- always send fractions.
typedef struct {
	float x1;
	float y1;
	float x2;
	float y2;
	float score;       // confidence, 0..1
	const char *label; // class name; NULL -> ""
	int class_id;
} rc_ext_box_t;

// Opens a connection to /run/recamera/result-in.sock and performs the
// Hello/HelloAck handshake. source_id is advisory (the server overrides it with
// the peercred-derived identity). On failure returns NULL and, if err != NULL,
// sets *err to an rc_ext_err_t code; on success sets *err to 0.
rc_ext_result_t *rc_ext_result_open(const char *source_id, int *err);

// Sends one detection result (task_type = DETECTION) with n boxes at pts_us
// (CLOCK_MONOTONIC microseconds; 0 = no frame association). Returns 0 on
// success or a negative rc_ext_err_t on failure.
int rc_ext_result_send_detections(rc_ext_result_t *h, uint64_t pts_us,
                                  const rc_ext_box_t *boxes, size_t n);

// --- other task types (spec §3.5) -------------------------------------------
// Each send_* below packs the matching InferenceResult oneof and sends one
// datagram, mirroring rc_ext_result_send_detections: pts_us is CLOCK_MONOTONIC
// microseconds (0 = no frame association); returns 0 on success or a negative
// rc_ext_err_t. All const char* labels accept NULL (treated as "").

// One classification entry (top-k). label may be NULL. Optionally carries a
// source ROI box (set has_box=1 to attach x1/y1/x2/y2, e.g. per-face
// attributes); has_box=0 omits the box on the wire, preserving the original
// box-less behaviour. When present, x1/y1/x2/y2 are normalized [0,1] (fraction
// of frame width/height), same contract as rc_ext_box_t.
typedef struct {
	float score;       // confidence, 0..1
	int class_id;
	const char *label; // class name; NULL -> ""
	int has_box;       // 0 -> no box (default); 1 -> x1/y1/x2/y2 valid
	float x1;          // normalized [0,1]
	float y1;          // normalized [0,1]
	float x2;          // normalized [0,1]
	float y2;          // normalized [0,1]
} rc_ext_class_t;

// task_type = CLASSIFICATION.
int rc_ext_result_send_classification(rc_ext_result_t *h, uint64_t pts_us,
                                      const rc_ext_class_t *items, size_t n);

// One segmentation entry: an optional ROI box plus a row-major mask buffer.
// The ROI box x1/y1/x2/y2 is normalized [0,1] (fraction of frame width/height),
// same contract as rc_ext_box_t. The mask is raw row-major bytes (NOT
// coordinates): mask points to mask_w*mask_h bytes (may be NULL with
// mask_w=mask_h=0).
typedef struct {
	float x1;            // normalized [0,1]
	float y1;            // normalized [0,1]
	float x2;            // normalized [0,1]
	float y2;            // normalized [0,1]
	float score;
	int class_id;
	const char *label;
	const uint8_t *mask; // row-major mask, mask_w*mask_h bytes; NULL -> empty
	int mask_w;
	int mask_h;
} rc_ext_seg_t;

// task_type = SEGMENTATION.
int rc_ext_result_send_segmentation(rc_ext_result_t *h, uint64_t pts_us,
                                    const rc_ext_seg_t *items, size_t n);

// One tracking entry: a detection box plus a persistent track_id. The box
// x1/y1/x2/y2 is normalized [0,1] (fraction of frame width/height), same
// contract as rc_ext_box_t.
typedef struct {
	float x1;          // normalized [0,1]
	float y1;          // normalized [0,1]
	float x2;          // normalized [0,1]
	float y2;          // normalized [0,1]
	float score;
	int class_id;
	const char *label;
	int track_id;
} rc_ext_track_t;

// task_type = TRACKING.
int rc_ext_result_send_tracking(rc_ext_result_t *h, uint64_t pts_us,
                                const rc_ext_track_t *items, size_t n);

// One keypoint within an instance. x/y are normalized [0,1] (fraction of frame
// width/height), same contract as the boxes; the OSD renderer clamps to [0,1]
// and multiplies by frame width/height.
typedef struct {
	float x;          // normalized [0,1]
	float y;          // normalized [0,1]
	float score;      // keypoint confidence (not object confidence)
	int keypoint_id;  // index in the caller-owned keypoint schema
} rc_ext_point_t;

// One detected instance: optional object box/label (set has_box=0 to omit the
// whole object_info group, per proto) plus its keypoints. When present, the
// object box x1/y1/x2/y2 is normalized [0,1], same contract as rc_ext_box_t.
typedef struct {
	int has_box;         // 0 -> omit object box/score/class/label entirely
	float x1;            // normalized [0,1]
	float y1;            // normalized [0,1]
	float x2;            // normalized [0,1]
	float y2;            // normalized [0,1]
	float score;         // object confidence
	int class_id;
	const char *label;
	const rc_ext_point_t *points;
	size_t n_points;
} rc_ext_kpinstance_t;

// task_type = KEYPOINTS.
int rc_ext_result_send_keypoints(rc_ext_result_t *h, uint64_t pts_us,
                                 const rc_ext_kpinstance_t *instances, size_t n);

// Closes the connection and frees the handle. NULL-safe.
void rc_ext_result_close(rc_ext_result_t *h);

// ===========================================================================
// Platform OSD-only sink (additive ABI)
// ===========================================================================
// This narrowly-scoped sink is reserved for the appmgr visualization bridge.
// Its server endpoint authenticates the connecting process with SO_PEERCRED,
// the root-owned appmgr pidfile and /proc identity.  Client-provided names are
// not authorization.  Accepted messages update only the device OSD compositor;
// they never enter recording, notification, rule or legacy WebSocket paths.

typedef struct rc_ext_osd rc_ext_osd_t;

// Connects to /run/recamera/osd-in.sock and performs the osd@1 handshake.
// On failure returns NULL and writes rc_ext_err_t to err when non-NULL.
rc_ext_osd_t *rc_ext_osd_open(int *err);

// Sends a DETECTION snapshot containing at most 64 normalized boxes.  n == 0
// is an explicit OSD clear.  Other task types are intentionally absent from
// this API and rejected by the server.
int rc_ext_osd_send_detections(rc_ext_osd_t *h, uint64_t pts_us,
			       const rc_ext_box_t *boxes, size_t n);

// Closes the OSD-only connection and frees the handle. NULL-safe.
void rc_ext_osd_close(rc_ext_osd_t *h);

// ===========================================================================
// Platform recording-only sink (additive ABI)
// ===========================================================================
// Reserved for the authenticated appmgr Result Hub bridge. Accepted messages
// enter only source-aware Vigil recording rules; they do not reach OSD,
// notifications, or public result streams. app_id must be the stable manifest
// id (`[a-z0-9-]{1,64}`, excluding reserved id `builtin`) and is supplied for
// every ordered datagram. Open/Hello/ACK and each send/reset use an internal
// one-second socket-I/O timeout so a stalled server cannot indefinitely hold
// the appmgr recording worker. Callers must serialize send/reset/close on a handle.

typedef struct rc_ext_record rc_ext_record_t;

// record@1 wire discriminator for application events. Events reuse the
// classification oneof but cannot be confused with frame classifications.
#define RC_EXT_RECORD_EVENT_MODEL_ID INT32_MIN

// Connects to /run/recamera/record-in.sock and performs a bounded record@1
// handshake. Transport/timeout failures report RC_EXT_EINTERNAL via err.
rc_ext_record_t *rc_ext_record_open(int *err);

int rc_ext_record_send_detections(rc_ext_record_t *h, const char *app_id,
                                  uint64_t pts_us, const rc_ext_box_t *boxes, size_t n);
int rc_ext_record_send_classification(rc_ext_record_t *h, const char *app_id,
                                      uint64_t pts_us, const rc_ext_class_t *items, size_t n);
int rc_ext_record_send_events(rc_ext_record_t *h, const char *app_id,
                              uint64_t pts_us, const rc_ext_class_t *items, size_t n);
int rc_ext_record_send_tracking(rc_ext_record_t *h, const char *app_id,
                                uint64_t pts_us, const rc_ext_track_t *items, size_t n);
int rc_ext_record_send_keypoints(rc_ext_record_t *h, const char *app_id,
                                 uint64_t pts_us, const rc_ext_kpinstance_t *instances,
                                 size_t n);

// Ordered lifecycle invalidation. Clears debounce state for app_id and returns
// success only after receiving a bounded HelloAck(error=0) from the server.
// appmgr calls this when an app stops/upgrades; the server also resets all
// sources observed on a connection if that connection is lost. Any reset
// error invalidates the handle; close it and open a new one before retrying.
int rc_ext_record_reset(rc_ext_record_t *h, const char *app_id);
void rc_ext_record_close(rc_ext_record_t *h);

// ===========================================================================
// M2 frame proxy -- zero-copy frame receiver (spec §2.5)
// ===========================================================================
//
//   rc_ext_frame_t *h = rc_ext_frame_open(NULL, &err);
//   rc_ext_frame_buf_t f;
//   while (rc_ext_frame_next(h, &f, 1000) == 0) {
//       void *y = rc_ext_frame_map(h, &f);   // mmap + DMA_BUF_IOCTL_SYNC START
//       // ... read f.plane[i].offset/stride/vstride, f.pts_us ...
//       rc_ext_frame_release(h, &f);          // SYNC END + release + close fd
//   }
//   rc_ext_frame_close(h);

#define RC_EXT_FRAME_MAGIC  0x52434652u // "RCFR"
#define RC_EXT_FRAME_VER    1u
#define RC_EXT_FOURCC_NV12  0x3231564Eu // 'N','V','1','2' little-endian

// Opaque frame-source connection handle.
typedef struct rc_ext_frame rc_ext_frame_t;

// One image plane's layout within the shared dma-buf (spec §2.3). Clients must
// use these values verbatim and never derive layout from width/height.
typedef struct {
	uint32_t offset;  // byte offset from the dma-buf start
	uint32_t stride;  // row stride in bytes
	uint32_t vstride; // rows incl. alignment padding
} rc_ext_plane_t;

// A borrowed frame: the header fields carried in the 96-byte wire header plus
// the dma-buf fd. Owned by the caller between rc_ext_frame_next() and
// rc_ext_frame_release(). Do NOT memcpy or reuse across frames -- pass the same
// object to map()/release().
typedef struct {
	uint64_t seq;     // monotonic; a gap means the server dropped frames
	uint64_t pts_us;  // VI PTS (CLOCK_MONOTONIC microseconds)
	uint32_t width;   // valid pixels
	uint32_t height;
	uint32_t fourcc;  // RC_EXT_FOURCC_NV12
	uint32_t buf_size;// total valid dma-buf length
	uint16_t flags;   // bit0: a frame was dropped before this one
	uint8_t chn_id;
	uint8_t n_planes; // NV12 = 2
	rc_ext_plane_t plane[3]; // NV12: plane[0]=Y, plane[1]=UV
	int fd;           // dma-buf fd (>= 0 while borrowed; -1 once released)
	void *_base;      // internal: mmap base (NULL until mapped)
	size_t _map_len;  // internal: mmap length
} rc_ext_frame_buf_t;

// Optional subscription config; pass NULL for the NPU-matched defaults.
typedef struct {
	uint32_t width;       // 0 = default
	uint32_t height;      // 0 = default
	uint32_t fourcc;      // 0 = NV12
	uint32_t fps_divisor; // 0/1 = every frame, 2 = every other, ...
} rc_ext_frame_cfg_t;

// Connects to /run/recamera/frame.sock, performs the Hello/HelloAck handshake
// and sends a FrameSubscribe, blocking for the FrameSubscribeAck. On failure
// returns NULL and, if err != NULL, sets *err to an rc_ext_err_t; on success
// sets *err to 0.
rc_ext_frame_t *rc_ext_frame_open(const rc_ext_frame_cfg_t *cfg, int *err);

// Effective subscription geometry from the FrameSubscribeAck (valid after
// open). Any out pointer may be NULL. Returns 0.
int rc_ext_frame_geometry(rc_ext_frame_t *h, uint32_t *width, uint32_t *height,
                          uint32_t *fourcc, uint32_t *pool_depth,
                          uint32_t *max_outstanding);

// Waits up to timeout_ms for the next frame. Receives one SEQPACKET datagram
// (96-byte header + exactly one dma-buf fd via SCM_RIGHTS, MSG_CMSG_CLOEXEC)
// and fills *out. Returns:
//   0  -> a frame is in *out (fd valid, not yet mapped)
//   1  -> timeout, no frame available (retry)
//  <0  -> -rc_ext_err_t: EOF/transport error (-EINTERNAL) or a protocol
//         violation (-EFORMAT; all received fds already closed).
// A negative return means the caller should stop and close the handle.
int rc_ext_frame_next(rc_ext_frame_t *h, rc_ext_frame_buf_t *out, int timeout_ms);

// mmap()s the frame's dma-buf (PROT_READ, MAP_SHARED) and issues
// DMA_BUF_IOCTL_SYNC(START|READ) so the CPU sees coherent data. Returns a
// pointer to the start of plane[0] (Y for NV12), or NULL on failure. Idempotent
// per frame. The mapping stays valid until rc_ext_frame_release().
void *rc_ext_frame_map(rc_ext_frame_t *h, rc_ext_frame_buf_t *f);

// Ends CPU access: DMA_BUF_IOCTL_SYNC(END|READ) if mapped, munmap, sends the
// 8-byte release seq back to the server, and closes the dma-buf fd. NULL-safe;
// idempotent (a released frame has f->fd == -1).
void rc_ext_frame_release(rc_ext_frame_t *h, rc_ext_frame_buf_t *f);

// Closes the connection and frees the handle. NULL-safe.
void rc_ext_frame_close(rc_ext_frame_t *h);

// ===========================================================================
// M3 observability -- probe tap receiver (spec §4)
// ===========================================================================
//
//   const char *stages[] = {"metrics", "preproc.out"};
//   rc_ext_probe_t *h = rc_ext_probe_open(stages, 2, 1, &err);
//   rc_ext_probe_sample_t s;
//   while (rc_ext_probe_next(h, &s, 1000) == 0) {
//       // s.stage_id, s.seq, s.pts_us, s.payload[0..s.payload_len)
//       // s.has_meta -> s.width/s.height/s.shape/... for tensor stages
//       rc_ext_probe_release(h, &s);   // munmap/close memfd; inline = no-op
//   }
//   rc_ext_probe_close(h);
//
// After the shared Hello/HelloAck the client sends one ProbeSubscribe (stage
// ids + sample_every) and receives a ProbeSubscribeAck; the server then PUSHES
// ProbeData one-way. Valid stage ids: "preproc.out", "npu.raw",
// "postproc.out", "metrics". Small samples (metrics / postproc.out) travel
// inline in the payload (fd_size == 0); large tensors (preproc.out / npu.raw)
// arrive as an ordinary memfd via SCM_RIGHTS -- a plain mmap(PROT_READ), no
// dma-buf cache sync.

// subscribed_mask bits reported by rc_ext_probe_info().
#define RC_EXT_PROBE_MASK_PREPROC  0x1u
#define RC_EXT_PROBE_MASK_NPU      0x2u
#define RC_EXT_PROBE_MASK_POSTPROC 0x4u
#define RC_EXT_PROBE_MASK_METRICS  0x8u

// Opaque probe-source connection handle.
typedef struct rc_ext_probe rc_ext_probe_t;

// One borrowed probe sample. Owned by the caller between rc_ext_probe_next()
// and rc_ext_probe_release(). Do NOT memcpy or reuse across samples. For inline
// samples `payload` points into an internal buffer; for memfd samples it points
// into an mmap()ed region -- either way it is valid only until release().
typedef struct {
	const char *stage_id;   // "preproc.out"/"npu.raw"/"postproc.out"/"metrics"
	uint64_t seq;           // monotonic inference seq; a gap means dropped samples
	uint64_t pts_us;        // CLOCK_MONOTONIC microseconds (same clock as frames)
	const uint8_t *payload; // sample bytes (inline or memfd-backed)
	size_t payload_len;     // payload length in bytes
	uint32_t flags;         // bit0: samples were dropped before this one
	// Tensor descriptor (valid when has_meta != 0; preproc/npu stages).
	int has_meta;
	uint32_t shape[8];
	uint32_t n_shape;
	uint32_t dtype;    // 0=uint8 1=int8 2=uint16 3=int16 4=float32 5=float16
	uint32_t layout;   // 0=unknown 1=NCHW 2=NHWC
	uint32_t fourcc;   // V4L2 fourcc for image-like tensors
	uint32_t width;
	uint32_t height;
	uint32_t stride;
	float scale;       // quantization scale (0 if not quantized)
	int32_t zero_point;
	// Internal bookkeeping (opaque to callers).
	int _fd;           // memfd (>=0) or -1 for inline
	void *_base;       // mmap base (memfd samples)
	size_t _map_len;   // mmap length
	void *_pb;         // owned unpacked ProbeData (freed by release)
} rc_ext_probe_sample_t;

// Connects to /run/recamera/probe.sock, performs the Hello/HelloAck handshake
// and sends a ProbeSubscribe for the given stage ids (sample_every: sample 1 of
// every N inferences, 0 => 1), blocking for the ProbeSubscribeAck. On failure
// returns NULL and, if err != NULL, sets *err to an rc_ext_err_t (EFORMAT if no
// stage was accepted); on success sets *err to 0.
rc_ext_probe_t *rc_ext_probe_open(const char *const *stage_ids, size_t n_stages,
                                  uint32_t sample_every, int *err);

// Effective subscription info from the ProbeSubscribeAck (valid after open).
// Any out pointer may be NULL. Returns 0.
int rc_ext_probe_info(rc_ext_probe_t *h, uint32_t *sample_every,
                      uint32_t *subscribed_mask);

// Waits up to timeout_ms for the next pushed sample. Fills *out. Returns:
//   0  -> a sample is in *out (must be released)
//   1  -> timeout, no sample available (retry)
//  <0  -> -rc_ext_err_t: EOF/transport error (-EINTERNAL) or a protocol
//         violation (-EFORMAT; any received fd already closed).
int rc_ext_probe_next(rc_ext_probe_t *h, rc_ext_probe_sample_t *out, int timeout_ms);

// Ends access to one sample: munmap + close the memfd (memfd samples) and frees
// the internal message. NULL-safe; idempotent; a no-op mmap/close for inline
// samples. After this the sample's payload/stage_id pointers are invalid.
void rc_ext_probe_release(rc_ext_probe_t *h, rc_ext_probe_sample_t *s);

// Closes the connection and frees the handle. NULL-safe.
void rc_ext_probe_close(rc_ext_probe_t *h);

// ===========================================================================
// M5 external NPU lease -- crash-safe RKNN ownership
// ===========================================================================
//
// rkipc owns the system NPU arbitration decision. A workflow MUST acquire this
// lease before constructing an RKNN context and keep the returned handle alive
// for the entire inference session. The AF_UNIX connection is the lease: when a
// process exits, is SIGKILLed, or loses rkipc, socket HUP revokes the generation
// and releases all server-side external ownership.
//
// ACQUIRE is acknowledged only after rkipc has destroyed its built-in RKNN
// handle, reached state "stopped", and reported actual_fps == 0. This is the
// safety boundary; an HTTP 200 from the legacy CGI is not equivalent.

#define RC_EXT_INFERENCE_APP_ID_MAX      64u
#define RC_EXT_INFERENCE_INSTANCE_ID_MAX 64u
#define RC_EXT_INFERENCE_STATE_MAX       16u
#define RC_EXT_INFERENCE_SOURCE_MAX      64u

typedef struct rc_ext_inference_lease rc_ext_inference_lease_t;

typedef enum {
	RC_EXT_INFERENCE_BUILTIN = 0,
	RC_EXT_INFERENCE_PREPARING_EXTERNAL = 1,
	RC_EXT_INFERENCE_EXTERNAL_ACQUIRED = 2,
	RC_EXT_INFERENCE_EXTERNAL_READY = 3,
	RC_EXT_INFERENCE_NONE = 4,
	RC_EXT_INFERENCE_FAULT = 5,
} rc_ext_inference_state_t;

// Versionable returned snapshot. Callers should zero-initialize and set
// struct_size=sizeof(rc_ext_inference_status_t) before rc_ext_inference_status;
// current clients may also leave it zero, which selects the current layout.
typedef struct {
	uint32_t struct_size;
	uint32_t state; // rc_ext_inference_state_t
	uint64_t lease_id;
	uint64_t epoch;       // changes whenever rkipc restarts
	uint64_t generation;  // increases for every granted external owner
	uint32_t actual_fps;
	int32_t peer_pid;
	uint8_t builtin_enabled;
	uint8_t handle_present;
	uint8_t fallback_builtin;
	uint8_t reserved0;
	char builtin_state[RC_EXT_INFERENCE_STATE_MAX];
	char source_id[RC_EXT_INFERENCE_SOURCE_MAX];
} rc_ext_inference_status_t;

// Connects to /run/recamera/inference-control.sock, requests exclusive external
// ownership, and waits up to timeout_ms for the built-in RKNN context to drain
// (0 selects the server default). app_id/instance_id are diagnostic identifiers;
// SO_PEERCRED remains authoritative. fallback_builtin!=0 asks rkipc to restore
// built-in inference if this process crashes. Returns NULL + *err on failure.
rc_ext_inference_lease_t *rc_ext_inference_lease_open(
	const char *app_id, const char *instance_id, uint32_t timeout_ms,
	int fallback_builtin, int *err);

// Marks initialization complete after the caller has successfully loaded its
// model and opened its frame/result endpoints. Returns 0 or -rc_ext_err_t.
int rc_ext_inference_lease_ready(rc_ext_inference_lease_t *lease);

// Changes the disconnect/crash fallback policy for the live lease. This is
// useful before a deliberate application stop. Returns 0 or -rc_ext_err_t.
int rc_ext_inference_lease_set_fallback(rc_ext_inference_lease_t *lease,
                                        int fallback_builtin);

// Fetches an authoritative status snapshot over the live control connection.
int rc_ext_inference_lease_status(rc_ext_inference_lease_t *lease,
                                  rc_ext_inference_status_t *out);

// Non-blocking liveness check: 1 means the connection is still live, 0 means
// HUP/EOF/revocation, and a negative value is -rc_ext_err_t.
int rc_ext_inference_lease_alive(rc_ext_inference_lease_t *lease);

// Best-effort RELEASE followed by close. NULL-safe. Process death needs no call:
// rkipc observes HUP and performs the same server-side revocation.
void rc_ext_inference_lease_close(rc_ext_inference_lease_t *lease);

// Fork-child cleanup only. It closes the child's inherited descriptor without
// sending RELEASE, so the parent process keeps its lease. The handle must not
// be used afterwards. Do not call this in the owning process.
void rc_ext_inference_lease_abandon_after_fork(
	rc_ext_inference_lease_t *lease);


// ===========================================================================
// M4 privacy-mask control (hardware COVER incremental control)
// ===========================================================================
//
// Controls the VI-layer hardware COVER blocks (solid rectangles applied before
// the NPU/encoder). Talks to rkipc's RPC socket /var/tmp/rkipc. Up to 6 blocks;
// id selects the slot ([0,6), maps to hardware COVER handle 8+id).
//
//   rc_ext_mask_t *mc = rc_ext_mask_open(&err);
//   rc_ext_mask_rect_t r = {0, 0.1f, 0.1f, 0.3f, 0.2f};
//   rc_ext_mask_set(mc, &r, 1, NULL);          // create one block (persisted)
//   for (...) { r.x = drift(); rc_ext_mask_update(mc, &r); }  // move, no flicker
//   rc_ext_mask_close(mc);

// Opaque hardware-mask control handle.
typedef struct rc_ext_mask rc_ext_mask_t;

// One normalized mask rectangle. x/y/w/h are [0,1] fractions of frame
// width/height; the library clamps to [0,1] before sending.
typedef struct {
	int   id;   // slot id, [0,6); maps to hardware COVER handle 8+id
	float x;    // top-left x, [0,1]
	float y;    // top-left y, [0,1]
	float w;    // width,  (0,1]
	float h;    // height, (0,1]
} rc_ext_mask_rect_t;

// Connects to /var/tmp/rkipc and completes the handshake. On failure returns
// NULL and, if err != NULL, sets *err to an rc_ext_err_t; on success sets
// *err to 0.
rc_ext_mask_t *rc_ext_mask_open(int *err);

// Full set of the currently-active mask blocks (n <= 6, excess truncated).
// Goes through the full rkipc set path (persisted to flash). Returns the number
// of blocks actually applied (>=0) or a negative rc_ext_err_t. applied may be
// NULL.
int rc_ext_mask_set(rc_ext_mask_t *h, const rc_ext_mask_rect_t *rects, size_t n, int *applied);

// Incrementally move a single block (position/size only, no flicker, not
// persisted). id must already exist. Returns 0 or a negative rc_ext_err_t
// (block not created / out of range -> caller should fall back to
// rc_ext_mask_set).
int rc_ext_mask_update(rc_ext_mask_t *h, const rc_ext_mask_rect_t *rect);

// Clears all masks (equivalent to set n=0 / disable; persisted).
int rc_ext_mask_clear(rc_ext_mask_t *h);

// Queries the current active masks: fills out[0..min(n,actual)); returns the
// actual block count (may exceed n, meaning truncation). out may be NULL with
// n=0 to fetch only the count. Returns a negative rc_ext_err_t on error.
int rc_ext_mask_query(rc_ext_mask_t *h, rc_ext_mask_rect_t *out, size_t n);

// Closes the connection and frees the handle. NULL-safe.
void rc_ext_mask_close(rc_ext_mask_t *h);

#ifdef __cplusplus
}
#endif

#endif // RECAMERA_EXT_H
