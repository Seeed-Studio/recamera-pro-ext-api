// Copyright 2025 reCamera Pro Extension API
// librecamera_ext C ABI v1 implementation -- result-injection sink.
#include "recamera_ext.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "ext_api.pb-c.h"
#include "ext_client_common.h"
#include "inference.pb-c.h"
#include "rc_ext_errno.h"

#define RC_EXT_RESULT_SOCK "/run/recamera/result-in.sock"
#define RC_EXT_OSD_SOCK    "/run/recamera/osd-in.sock"
#define RC_EXT_RECORD_SOCK "/run/recamera/record-in.sock"
#define RC_EXT_OSD_MAX_BOXES 64u

// RecordSink calls run on appmgr's ordered recording worker. Keep every
// record@1 socket operation finite so lifecycle invalidation and close cannot
// be held indefinitely by a stalled or backpressured server. This does not
// alter the historical blocking behaviour of ResultSink or OsdSink.
#ifndef RC_EXT_RECORD_IO_TIMEOUT_MS
#define RC_EXT_RECORD_IO_TIMEOUT_MS 1000u
#endif

struct rc_ext_result {
	int fd;
	uint32_t api_version;
	char source_id[64];
};

struct rc_ext_osd {
	int fd;
	uint32_t api_version;
};

struct rc_ext_record {
	int fd;
	uint32_t api_version;
};

// Allocate the entries[]/eptrs[]/boxobjs[] triple shared by the detection,
// classification, segmentation and tracking builders. Left-to-right sequencing
// of the comma operator means all three callocs run; the expression is true
// only when all succeeded. On failure the caller frees with RC_FREE3.
#define RC_ALLOC3(entries, eptrs, boxobjs, n)                                  \
	((entries) = calloc((n), sizeof(*(entries))),                          \
	 (eptrs) = calloc((n), sizeof(*(eptrs))),                              \
	 (boxobjs) = calloc((n), sizeof(*(boxobjs))),                          \
	 ((entries) && (eptrs) && (boxobjs)))

#define RC_FREE3(entries, eptrs, boxobjs)                                      \
	do {                                                                   \
		free(entries);                                                 \
		free(eptrs);                                                   \
		free(boxobjs);                                                 \
	} while (0)

// Fill the top-level fields common to every task entry struct (box pointer,
// score, class_id, class_name). Field names match across the generated entry
// types. `boxp` may be NULL (e.g. box-less classification). `lbl` NULL -> "".
#define RC_FILL_ENTRY(e, boxp, sc, cid, lbl)                                   \
	do {                                                                   \
		(e).box = (boxp);                                              \
		(e).score = (sc);                                              \
		(e).class_id = (cid);                                          \
		(e).class_name = (char *)((lbl) ? (lbl) : "");                 \
	} while (0)

// Initialise a protobuf InferenceBox from (x1,y1,x2,y2) corner coordinates.
// Shared by every task type that carries a bounding box.
static void fill_box(InferenceBox *b, float x1, float y1, float x2, float y2) {
	InferenceBox init = INFERENCE_BOX__INIT;
	*b = init;
	b->left = x1;
	b->top = y1;
	b->right = x2;
	b->bottom = y2;
}

// Packs a fully-built InferenceResult and sends it as one datagram. Fills the
// common top-level fields first. Returns 0 on success or -rc_ext_err_t.
static int rc_send_result_fd(int fd, const char *source_id,
			     InferenceResult *res) {
	res->model_id = 0;
	res->source_id = (char *)(source_id ? source_id : "");
	size_t psz = inference_result__get_packed_size(res);
	uint8_t *pbuf = (uint8_t *)malloc(psz ? psz : 1);
	if (!pbuf)
		return -RC_EXT_EINTERNAL;
	inference_result__pack(res, pbuf);
	ssize_t s = send(fd, pbuf, psz, MSG_NOSIGNAL);
	free(pbuf);
	return (s < 0) ? -RC_EXT_EINTERNAL : 0;
}

static int rc_send_record_result_mode(int fd, const char *source_id,
				      InferenceResult *res, int32_t model_id,
				      int require_ack) {
	res->model_id = model_id;
	res->source_id = (char *)(source_id ? source_id : "");
	size_t packed_size = inference_result__get_packed_size(res);
	uint8_t *buffer = (uint8_t *)malloc(packed_size ? packed_size : 1);
	if (!buffer)
		return -RC_EXT_EINTERNAL;
	inference_result__pack(res, buffer);
	int sent = require_ack ?
	    rc_ext_send_packet_ack_bounded(fd, buffer, packed_size,
	                                   RC_EXT_RECORD_IO_TIMEOUT_MS) :
	    rc_ext_send_packet_bounded(fd, buffer, packed_size,
	                               RC_EXT_RECORD_IO_TIMEOUT_MS);
	free(buffer);
	return require_ack ? sent : (sent < 0 ? -RC_EXT_EINTERNAL : 0);
}

static int rc_send_record_result_fd(int fd, const char *source_id,
				    InferenceResult *res) {
	return rc_send_record_result_mode(fd, source_id, res, 0, 0);
}

static int rc_send_record_event_fd(int fd, const char *source_id,
				   InferenceResult *res) {
	return rc_send_record_result_mode(
	    fd, source_id, res, RC_EXT_RECORD_EVENT_MODEL_ID, 0);
}

static int rc_send_record_reset_fd(int fd, const char *source_id,
				   InferenceResult *res) {
	return rc_send_record_result_mode(fd, source_id, res, 0, 1);
}

typedef int (*send_result_fn)(int fd, const char *source_id,
			      InferenceResult *result);

static int rc_send_result(rc_ext_result_t *handle, InferenceResult *result) {
	return rc_send_result_fd(handle->fd, handle->source_id, result);
}

static int rc_record_app_id_valid(const char *app_id) {
	size_t length = 0;

	if (!app_id)
		return 0;
	while (length <= 64 && app_id[length] != '\0')
		length++;
	if (length == 0 || length > 64 || strcmp(app_id, "builtin") == 0)
		return 0;
	for (size_t i = 0; i < length; i++) {
		unsigned char ch = (unsigned char)app_id[i];
		if (!((ch >= 'a' && ch <= 'z') ||
		      (ch >= '0' && ch <= '9') || ch == '-'))
			return 0;
	}
	return 1;
}

rc_ext_record_t *rc_ext_record_open(int *err) {
	uint32_t api_version = 0;
	int fd = rc_ext_connect_hello_bounded(
	    RC_EXT_RECORD_SOCK, "appmgr-record", &api_version, err,
	    RC_EXT_RECORD_IO_TIMEOUT_MS);
	if (fd < 0)
		return NULL;

	rc_ext_record_t *handle = (rc_ext_record_t *)calloc(1, sizeof(*handle));
	if (!handle) {
		close(fd);
		rc_ext_set_err(err, RC_EXT_EINTERNAL);
		return NULL;
	}
	handle->fd = fd;
	handle->api_version = api_version;
	if (err)
		*err = RC_EXT_OK;
	return handle;
}

rc_ext_result_t *rc_ext_result_open(const char *source_id, int *err) {
	uint32_t api_version = 0;
	int fd = rc_ext_connect_hello(RC_EXT_RESULT_SOCK,
	                              source_id ? source_id : "ext", &api_version, err);
	if (fd < 0)
		return NULL;

	rc_ext_result_t *h = (rc_ext_result_t *)calloc(1, sizeof(*h));
	if (!h) {
		close(fd);
		rc_ext_set_err(err, RC_EXT_EINTERNAL);
		return NULL;
	}
	h->fd = fd;
	h->api_version = api_version;
	snprintf(h->source_id, sizeof(h->source_id), "%s", source_id ? source_id : "ext");

	if (err)
		*err = RC_EXT_OK;
	return h;
}

static int send_detections_fd(int fd, const char *source_id, uint64_t pts_us,
			      const rc_ext_box_t *boxes, size_t n,
			      send_result_fn send_result) {
	if (fd < 0 || (n && !boxes))
		return -RC_EXT_EINTERNAL;

	InferenceResult res = INFERENCE_RESULT__INIT;
	res.task_type = TASK_TYPE__TASK_TYPE_DETECTION;
	res.pts_us = pts_us;

	InferenceDetectionResult det = INFERENCE_DETECTION_RESULT__INIT;
	InferenceDetectionEntry *entries = NULL;
	InferenceDetectionEntry **eptrs = NULL;
	InferenceBox *boxobjs = NULL;

	if (n) {
		if (!RC_ALLOC3(entries, eptrs, boxobjs, n)) {
			RC_FREE3(entries, eptrs, boxobjs);
			return -RC_EXT_EINTERNAL;
		}
		for (size_t i = 0; i < n; i++) {
			fill_box(&boxobjs[i], boxes[i].x1, boxes[i].y1, boxes[i].x2, boxes[i].y2);

			InferenceDetectionEntry e = INFERENCE_DETECTION_ENTRY__INIT;
			entries[i] = e;
			RC_FILL_ENTRY(entries[i], &boxobjs[i], boxes[i].score,
				      boxes[i].class_id, boxes[i].label);
			eptrs[i] = &entries[i];
		}
	}

	det.n_entries = n;
	det.entries = n ? eptrs : NULL;
	res.data_case = INFERENCE_RESULT__DATA_DETECTION;
	res.detection = &det;

	int ret = send_result(fd, source_id, &res);

	RC_FREE3(entries, eptrs, boxobjs);
	return ret;
}

int rc_ext_result_send_detections(rc_ext_result_t *h, uint64_t pts_us,
				  const rc_ext_box_t *boxes, size_t n) {
	if (!h)
		return -RC_EXT_EINTERNAL;
	return send_detections_fd(h->fd, h->source_id, pts_us, boxes, n,
	                          rc_send_result_fd);
}

int rc_ext_record_send_detections(rc_ext_record_t *h, const char *app_id,
				  uint64_t pts_us, const rc_ext_box_t *boxes,
				  size_t n) {
	if (!h || h->fd < 0)
		return -RC_EXT_EINTERNAL;
	if (!rc_record_app_id_valid(app_id))
		return -RC_EXT_EFORMAT;
	return send_detections_fd(h->fd, app_id, pts_us, boxes, n,
	                          rc_send_record_result_fd);
}

rc_ext_osd_t *rc_ext_osd_open(int *err) {
	uint32_t api_version = 0;
	int fd = rc_ext_connect_hello(RC_EXT_OSD_SOCK, "appmgr-osd",
				      &api_version, err);
	if (fd < 0)
		return NULL;
	rc_ext_osd_t *handle = (rc_ext_osd_t *)calloc(1, sizeof(*handle));
	if (!handle) {
		close(fd);
		rc_ext_set_err(err, RC_EXT_EINTERNAL);
		return NULL;
	}
	handle->fd = fd;
	handle->api_version = api_version;
	if (err)
		*err = RC_EXT_OK;
	return handle;
}

int rc_ext_osd_send_detections(rc_ext_osd_t *handle, uint64_t pts_us,
			       const rc_ext_box_t *boxes, size_t n) {
	if (!handle)
		return -RC_EXT_EINTERNAL;
	if (n > RC_EXT_OSD_MAX_BOXES || (n && !boxes))
		return -RC_EXT_EFORMAT;
	return send_detections_fd(handle->fd, "appmgr-osd", pts_us, boxes, n,
	                          rc_send_result_fd);
}

void rc_ext_osd_close(rc_ext_osd_t *handle) {
	if (!handle)
		return;
	if (handle->fd >= 0)
		close(handle->fd);
	free(handle);
}

static int send_classification_fd(int fd, const char *source_id,
				  uint64_t pts_us,
				  const rc_ext_class_t *items, size_t n,
				  send_result_fn send_result) {
	if (fd < 0 || (n && !items))
		return -RC_EXT_EINTERNAL;

	InferenceResult res = INFERENCE_RESULT__INIT;
	res.task_type = TASK_TYPE__TASK_TYPE_CLASSIFICATION;
	res.pts_us = pts_us;

	InferenceClassificationResult cls = INFERENCE_CLASSIFICATION_RESULT__INIT;
	InferenceClassificationEntry *entries = NULL;
	InferenceClassificationEntry **eptrs = NULL;
	InferenceBox *boxobjs = NULL;

	if (n) {
		if (!RC_ALLOC3(entries, eptrs, boxobjs, n)) {
			RC_FREE3(entries, eptrs, boxobjs);
			return -RC_EXT_EINTERNAL;
		}
		for (size_t i = 0; i < n; i++) {
			InferenceClassificationEntry e = INFERENCE_CLASSIFICATION_ENTRY__INIT;
			entries[i] = e;
			InferenceBox *boxp = NULL;
			if (items[i].has_box) {
				fill_box(&boxobjs[i], items[i].x1, items[i].y1,
					 items[i].x2, items[i].y2);
				boxp = &boxobjs[i];
			}
			RC_FILL_ENTRY(entries[i], boxp, items[i].score,
				      items[i].class_id, items[i].label);
			eptrs[i] = &entries[i];
		}
	}

	cls.n_entries = n;
	cls.entries = n ? eptrs : NULL;
	res.data_case = INFERENCE_RESULT__DATA_CLASSIFICATION;
	res.classification = &cls;

	int ret = send_result(fd, source_id, &res);

	RC_FREE3(entries, eptrs, boxobjs);
	return ret;
}

int rc_ext_result_send_classification(rc_ext_result_t *h, uint64_t pts_us,
				      const rc_ext_class_t *items, size_t n) {
	if (!h)
		return -RC_EXT_EINTERNAL;
	return send_classification_fd(h->fd, h->source_id, pts_us, items, n,
	                              rc_send_result_fd);
}

int rc_ext_record_send_classification(rc_ext_record_t *h, const char *app_id,
				      uint64_t pts_us,
				      const rc_ext_class_t *items, size_t n) {
	if (!h || h->fd < 0)
		return -RC_EXT_EINTERNAL;
	if (!rc_record_app_id_valid(app_id))
		return -RC_EXT_EFORMAT;
	return send_classification_fd(h->fd, app_id, pts_us, items, n,
	                              rc_send_record_result_fd);
}

int rc_ext_record_send_events(rc_ext_record_t *h, const char *app_id,
			      uint64_t pts_us,
			      const rc_ext_class_t *items, size_t n) {
	if (!h || h->fd < 0)
		return -RC_EXT_EINTERNAL;
	if (!rc_record_app_id_valid(app_id))
		return -RC_EXT_EFORMAT;
	return send_classification_fd(h->fd, app_id, pts_us, items, n,
	                              rc_send_record_event_fd);
}

int rc_ext_result_send_segmentation(rc_ext_result_t *h, uint64_t pts_us,
                                    const rc_ext_seg_t *items, size_t n) {
	if (!h)
		return -RC_EXT_EINTERNAL;

	InferenceResult res = INFERENCE_RESULT__INIT;
	res.task_type = TASK_TYPE__TASK_TYPE_SEGMENTATION;
	res.pts_us = pts_us;

	InferenceSegmentationResult seg = INFERENCE_SEGMENTATION_RESULT__INIT;
	InferenceSegmentationEntry *entries = NULL;
	InferenceSegmentationEntry **eptrs = NULL;
	InferenceBox *boxobjs = NULL;

	if (n) {
		if (!RC_ALLOC3(entries, eptrs, boxobjs, n)) {
			RC_FREE3(entries, eptrs, boxobjs);
			return -RC_EXT_EINTERNAL;
		}
		for (size_t i = 0; i < n; i++) {
			fill_box(&boxobjs[i], items[i].x1, items[i].y1, items[i].x2, items[i].y2);

			InferenceSegmentationEntry e = INFERENCE_SEGMENTATION_ENTRY__INIT;
			entries[i] = e;
			RC_FILL_ENTRY(entries[i], &boxobjs[i], items[i].score,
				      items[i].class_id, items[i].label);
			if (items[i].mask && items[i].mask_w > 0 && items[i].mask_h > 0) {
				entries[i].mask.data = (uint8_t *)items[i].mask;
				entries[i].mask.len = (size_t)items[i].mask_w * (size_t)items[i].mask_h;
			}
			entries[i].mask_width = items[i].mask_w;
			entries[i].mask_height = items[i].mask_h;
			eptrs[i] = &entries[i];
		}
	}

	seg.n_entries = n;
	seg.entries = n ? eptrs : NULL;
	res.data_case = INFERENCE_RESULT__DATA_SEGMENTATION;
	res.segmentation = &seg;

	int ret = rc_send_result(h, &res);

	RC_FREE3(entries, eptrs, boxobjs);
	return ret;
}

static int send_tracking_fd(int fd, const char *source_id, uint64_t pts_us,
			    const rc_ext_track_t *items, size_t n,
			    send_result_fn send_result) {
	if (fd < 0 || (n && !items))
		return -RC_EXT_EINTERNAL;

	InferenceResult res = INFERENCE_RESULT__INIT;
	res.task_type = TASK_TYPE__TASK_TYPE_TRACKING;
	res.pts_us = pts_us;

	InferenceTrackingResult trk = INFERENCE_TRACKING_RESULT__INIT;
	InferenceTrackingEntry *entries = NULL;
	InferenceTrackingEntry **eptrs = NULL;
	InferenceBox *boxobjs = NULL;

	if (n) {
		if (!RC_ALLOC3(entries, eptrs, boxobjs, n)) {
			RC_FREE3(entries, eptrs, boxobjs);
			return -RC_EXT_EINTERNAL;
		}
		for (size_t i = 0; i < n; i++) {
			fill_box(&boxobjs[i], items[i].x1, items[i].y1, items[i].x2, items[i].y2);

			InferenceTrackingEntry e = INFERENCE_TRACKING_ENTRY__INIT;
			entries[i] = e;
			RC_FILL_ENTRY(entries[i], &boxobjs[i], items[i].score,
				      items[i].class_id, items[i].label);
			entries[i].track_id = items[i].track_id;
			eptrs[i] = &entries[i];
		}
	}

	trk.n_entries = n;
	trk.entries = n ? eptrs : NULL;
	res.data_case = INFERENCE_RESULT__DATA_TRACKING;
	res.tracking = &trk;

	int ret = send_result(fd, source_id, &res);

	RC_FREE3(entries, eptrs, boxobjs);
	return ret;
}

int rc_ext_result_send_tracking(rc_ext_result_t *h, uint64_t pts_us,
				const rc_ext_track_t *items, size_t n) {
	if (!h)
		return -RC_EXT_EINTERNAL;
	return send_tracking_fd(h->fd, h->source_id, pts_us, items, n,
	                        rc_send_result_fd);
}

int rc_ext_record_send_tracking(rc_ext_record_t *h, const char *app_id,
				uint64_t pts_us,
				const rc_ext_track_t *items, size_t n) {
	if (!h || h->fd < 0)
		return -RC_EXT_EINTERNAL;
	if (!rc_record_app_id_valid(app_id))
		return -RC_EXT_EFORMAT;
	return send_tracking_fd(h->fd, app_id, pts_us, items, n,
	                        rc_send_record_result_fd);
}

static int send_keypoints_fd(int fd, const char *source_id, uint64_t pts_us,
			     const rc_ext_kpinstance_t *instances, size_t n,
			     send_result_fn send_result) {
	if (fd < 0 || (n && !instances))
		return -RC_EXT_EINTERNAL;

	InferenceResult res = INFERENCE_RESULT__INIT;
	res.task_type = TASK_TYPE__TASK_TYPE_KEYPOINTS;
	res.pts_us = pts_us;

	InferenceKeypointsResult kp = INFERENCE_KEYPOINTS_RESULT__INIT;
	InferenceKeypointInstance *insts = NULL;
	InferenceKeypointInstance **iptrs = NULL;
	InferenceObjectInfo *objs = NULL;
	InferenceBox *boxobjs = NULL;
	// One flat pool of point objects + pointer arrays, indexed per instance.
	InferencePoint *pts = NULL;
	InferencePoint **pptrs = NULL;
	size_t total_pts = 0;
	int ret;

	if (n) {
		for (size_t i = 0; i < n; i++)
			total_pts += instances[i].n_points;

		insts = (InferenceKeypointInstance *)calloc(n, sizeof(*insts));
		iptrs = (InferenceKeypointInstance **)calloc(n, sizeof(*iptrs));
		objs = (InferenceObjectInfo *)calloc(n, sizeof(*objs));
		boxobjs = (InferenceBox *)calloc(n, sizeof(*boxobjs));
		if (total_pts) {
			pts = (InferencePoint *)calloc(total_pts, sizeof(*pts));
			pptrs = (InferencePoint **)calloc(total_pts, sizeof(*pptrs));
		}
		if (!insts || !iptrs || !objs || !boxobjs ||
		    (total_pts && (!pts || !pptrs))) {
			ret = -RC_EXT_EINTERNAL;
			goto cleanup;
		}

		size_t pbase = 0;
		for (size_t i = 0; i < n; i++) {
			InferenceKeypointInstance inst = INFERENCE_KEYPOINT_INSTANCE__INIT;
			insts[i] = inst;

			// Points for this instance.
			size_t np = instances[i].n_points;
			for (size_t j = 0; j < np; j++) {
				InferencePoint p = INFERENCE_POINT__INIT;
				pts[pbase + j] = p;
				pts[pbase + j].x = instances[i].points[j].x;
				pts[pbase + j].y = instances[i].points[j].y;
				pts[pbase + j].score = instances[i].points[j].score;
				pts[pbase + j].keypoint_id = instances[i].points[j].keypoint_id;
				pptrs[pbase + j] = &pts[pbase + j];
			}
			insts[i].n_points = np;
			insts[i].points = np ? &pptrs[pbase] : NULL;
			pbase += np;

			// Optional object_info group.
			if (instances[i].has_box) {
				InferenceBox b = INFERENCE_BOX__INIT;
				boxobjs[i] = b;
				boxobjs[i].left = instances[i].x1;
				boxobjs[i].top = instances[i].y1;
				boxobjs[i].right = instances[i].x2;
				boxobjs[i].bottom = instances[i].y2;

				InferenceObjectInfo oi = INFERENCE_OBJECT_INFO__INIT;
				objs[i] = oi;
				objs[i].class_id = instances[i].class_id;
				objs[i].class_name =
				    (char *)(instances[i].label ? instances[i].label : "");
				objs[i].score = instances[i].score;
				objs[i].box = &boxobjs[i];

				insts[i].object_info_case =
				    INFERENCE_KEYPOINT_INSTANCE__OBJECT_INFO_OBJECT;
				insts[i].object = &objs[i];
			}
			iptrs[i] = &insts[i];
		}
	}

	kp.n_instances = n;
	kp.instances = n ? iptrs : NULL;
	res.data_case = INFERENCE_RESULT__DATA_KEYPOINTS;
	res.keypoints = &kp;

	ret = send_result(fd, source_id, &res);

cleanup:
	free(insts);
	free(iptrs);
	free(objs);
	free(boxobjs);
	free(pts);
	free(pptrs);
	return ret;
}

int rc_ext_result_send_keypoints(rc_ext_result_t *h, uint64_t pts_us,
				 const rc_ext_kpinstance_t *instances,
				 size_t n) {
	if (!h)
		return -RC_EXT_EINTERNAL;
	return send_keypoints_fd(h->fd, h->source_id, pts_us, instances, n,
	                         rc_send_result_fd);
}

int rc_ext_record_send_keypoints(rc_ext_record_t *h, const char *app_id,
				 uint64_t pts_us,
				 const rc_ext_kpinstance_t *instances,
				 size_t n) {
	if (!h || h->fd < 0)
		return -RC_EXT_EINTERNAL;
	if (!rc_record_app_id_valid(app_id))
		return -RC_EXT_EFORMAT;
	return send_keypoints_fd(h->fd, app_id, pts_us, instances, n,
	                         rc_send_record_result_fd);
}

void rc_ext_result_close(rc_ext_result_t *h) {
	if (!h)
		return;
	if (h->fd >= 0)
		close(h->fd);
	free(h);
}

int rc_ext_record_reset(rc_ext_record_t *h, const char *app_id) {
	if (!h || h->fd < 0)
		return -RC_EXT_EINTERNAL;
	if (!rc_record_app_id_valid(app_id))
		return -RC_EXT_EFORMAT;

	InferenceResult result = INFERENCE_RESULT__INIT;
	/* record@1 reserves DATA__NOT_SET for ordered source invalidation. */
	int ret = rc_send_record_reset_fd(h->fd, app_id, &result);
	if (ret != 0 && h->fd >= 0) {
		/* The request may have reached the server even when its ACK did not.
		 * Never let a delayed ACK confirm a later reset on this stream. */
		close(h->fd);
		h->fd = -1;
	}
	return ret;
}

void rc_ext_record_close(rc_ext_record_t *h) {
	if (!h)
		return;
	if (h->fd >= 0)
		close(h->fd);
	free(h);
}
