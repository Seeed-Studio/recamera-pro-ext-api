// Host-only record@1 client wire test. The production implementation is
// included directly so its private fd builders can be exercised through a
// socketpair without creating the privileged /run endpoint.
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "ext_client_common.h"
#include "inference.pb-c.h"

#define CHECK(expression)                                                      \
	do {                                                                     \
		if (!(expression)) {                                               \
			fprintf(stderr, "CHECK failed %s:%d: %s\n", __FILE__,       \
			        __LINE__, #expression);                               \
			return 1;                                                    \
		}                                                                \
	} while (0)

static int g_record_fd = -1;
static char g_open_path[128];
static char g_client_name[64];
static unsigned int g_open_timeout_ms;
static unsigned int g_send_timeout_ms;
static unsigned int g_ack_timeout_ms;
static int g_ack_result;

int rc_ext_set_err(int *err, rc_ext_err_t code) {
	if (err)
		*err = code;
	return -1;
}

int rc_ext_connect_hello(const char *path, const char *client_name,
			 uint32_t *api_version, int *err) {
	snprintf(g_open_path, sizeof(g_open_path), "%s", path ? path : "");
	snprintf(g_client_name, sizeof(g_client_name), "%s",
	         client_name ? client_name : "");
	if (g_record_fd < 0)
		return rc_ext_set_err(err, RC_EXT_EINTERNAL);
	if (api_version)
		*api_version = 1;
	if (err)
		*err = RC_EXT_OK;
	return dup(g_record_fd);
}

int rc_ext_connect_hello_bounded(const char *path, const char *client_name,
				 uint32_t *api_version, int *err,
				 unsigned int timeout_ms) {
	g_open_timeout_ms = timeout_ms;
	return rc_ext_connect_hello(path, client_name, api_version, err);
}

int rc_ext_send_packet_bounded(int fd, const void *buffer, size_t size,
			       unsigned int timeout_ms) {
	g_send_timeout_ms = timeout_ms;
	ssize_t sent = send(fd, buffer, size, MSG_NOSIGNAL);
	return sent == (ssize_t)size ? 0 : -1;
}

int rc_ext_send_packet_ack_bounded(int fd, const void *buffer, size_t size,
				   unsigned int timeout_ms) {
	g_ack_timeout_ms = timeout_ms;
	if (rc_ext_send_packet_bounded(fd, buffer, size, timeout_ms) < 0)
		return -RC_EXT_EINTERNAL;
	return g_ack_result;
}

#include "../src/recamera_ext.c"

static InferenceResult *receive_result(int fd) {
	uint8_t buffer[65536];
	ssize_t received = recv(fd, buffer, sizeof(buffer), 0);
	if (received <= 0)
		return NULL;
	return inference_result__unpack(NULL, (size_t)received, buffer);
}

static int check_common(const InferenceResult *result, const char *app_id,
			uint64_t pts_us, InferenceResult__DataCase data_case,
			int32_t model_id) {
	return result && result->source_id &&
	       strcmp(result->source_id, app_id) == 0 &&
	       result->model_id == model_id &&
	       result->pts_us == pts_us && result->data_case == data_case;
}

static int expect_no_message(int fd) {
	uint8_t byte;
	errno = 0;
	ssize_t received = recv(fd, &byte, sizeof(byte), MSG_DONTWAIT);
	return received < 0 && (errno == EAGAIN || errno == EWOULDBLOCK);
}

int main(void) {
	int pair[2];
	CHECK(socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, pair) == 0);
	g_record_fd = pair[0];
	int error = -1;
	rc_ext_record_t *record = rc_ext_record_open(&error);
	CHECK(record != NULL && error == RC_EXT_OK);
	CHECK(strcmp(g_open_path, "/run/recamera/record-in.sock") == 0);
	CHECK(strcmp(g_client_name, "appmgr-record") == 0);
	CHECK(g_open_timeout_ms == RC_EXT_RECORD_IO_TIMEOUT_MS);

	rc_ext_box_t box = {
		.x1 = 0.1f, .y1 = 0.2f, .x2 = 0.8f, .y2 = 0.9f,
		.score = 0.75f, .label = "person", .class_id = 3,
	};
	CHECK(rc_ext_record_send_detections(record, "fall-detection", 101,
	                                    &box, 1) == 0);
	InferenceResult *message = receive_result(pair[1]);
	CHECK(check_common(message, "fall-detection", 101,
	                   INFERENCE_RESULT__DATA_DETECTION, 0));
	CHECK(message->detection && message->detection->n_entries == 1);
	CHECK(message->detection->entries[0]->class_id == 3);
	CHECK(strcmp(message->detection->entries[0]->class_name, "person") == 0);
	inference_result__free_unpacked(message, NULL);

	rc_ext_class_t classification = {
		.score = 1.0f, .class_id = 7, .label = "fall", .has_box = 0,
	};
	CHECK(rc_ext_record_send_classification(record, "fall-detection", 102,
	                                        &classification, 1) == 0);
	message = receive_result(pair[1]);
	CHECK(check_common(message, "fall-detection", 102,
	                   INFERENCE_RESULT__DATA_CLASSIFICATION, 0));
	CHECK(message->classification && message->classification->n_entries == 1);
	CHECK(message->classification->entries[0]->class_id == 7);
	inference_result__free_unpacked(message, NULL);

	rc_ext_class_t event = {
		.score = 1.0f, .class_id = 0, .label = "motion", .has_box = 0,
	};
	CHECK(rc_ext_record_send_events(record, "event-app", 103, &event, 1) == 0);
	message = receive_result(pair[1]);
	CHECK(check_common(message, "event-app", 103,
	                   INFERENCE_RESULT__DATA_CLASSIFICATION,
	                   RC_EXT_RECORD_EVENT_MODEL_ID));
	CHECK(message->classification && message->classification->n_entries == 1);
	CHECK(strcmp(message->classification->entries[0]->class_name, "motion") == 0);
	inference_result__free_unpacked(message, NULL);

	rc_ext_track_t track = {
		.x1 = 0.1f, .y1 = 0.2f, .x2 = 0.8f, .y2 = 0.9f,
		.score = 0.8f, .class_id = 3, .label = "person", .track_id = 42,
	};
	CHECK(rc_ext_record_send_tracking(record, "tracker", 103, &track, 1) == 0);
	message = receive_result(pair[1]);
	CHECK(check_common(message, "tracker", 103,
	                   INFERENCE_RESULT__DATA_TRACKING, 0));
	CHECK(message->tracking && message->tracking->n_entries == 1);
	CHECK(message->tracking->entries[0]->track_id == 42);
	inference_result__free_unpacked(message, NULL);

	rc_ext_point_t point = {
		.x = 0.3f, .y = 0.4f, .score = 0.9f, .keypoint_id = 1,
	};
	rc_ext_kpinstance_t keypoints = {
		.has_box = 0, .points = &point, .n_points = 1,
	};
	CHECK(rc_ext_record_send_keypoints(record, "pose-app", 104,
	                                   &keypoints, 1) == 0);
	message = receive_result(pair[1]);
	CHECK(check_common(message, "pose-app", 104,
	                   INFERENCE_RESULT__DATA_KEYPOINTS, 0));
	CHECK(message->keypoints && message->keypoints->n_instances == 1);
	CHECK(message->keypoints->instances[0]->n_points == 1);
	inference_result__free_unpacked(message, NULL);

	CHECK(rc_ext_record_reset(record, "fall-detection") == 0);
	message = receive_result(pair[1]);
	CHECK(check_common(message, "fall-detection", 0,
	                   INFERENCE_RESULT__DATA__NOT_SET, 0));
	CHECK(g_ack_timeout_ms == RC_EXT_RECORD_IO_TIMEOUT_MS);
	inference_result__free_unpacked(message, NULL);

	char maximum_id[65];
	memset(maximum_id, 'a', sizeof(maximum_id) - 1);
	maximum_id[sizeof(maximum_id) - 1] = '\0';
	CHECK(rc_ext_record_send_detections(record, maximum_id, 0, NULL, 0) == 0);
	message = receive_result(pair[1]);
	CHECK(check_common(message, maximum_id, 0,
	                   INFERENCE_RESULT__DATA_DETECTION, 0));
	inference_result__free_unpacked(message, NULL);

	char oversized_id[66];
	memset(oversized_id, 'a', sizeof(oversized_id) - 1);
	oversized_id[sizeof(oversized_id) - 1] = '\0';
	const char *invalid_ids[] = {NULL, "", "builtin", "Bad-App", "bad_app",
	                             oversized_id};
	for (size_t i = 0; i < sizeof(invalid_ids) / sizeof(invalid_ids[0]); i++) {
		CHECK(rc_ext_record_send_detections(record, invalid_ids[i], 0,
		                                    NULL, 0) == -RC_EXT_EFORMAT);
		CHECK(rc_ext_record_reset(record, invalid_ids[i]) == -RC_EXT_EFORMAT);
	}
	CHECK(rc_ext_record_send_detections(NULL, "valid-app", 0, NULL, 0) ==
	      -RC_EXT_EINTERNAL);
	CHECK(rc_ext_record_reset(NULL, "valid-app") == -RC_EXT_EINTERNAL);
	CHECK(rc_ext_record_send_detections(record, "valid-app", 0, NULL, 1) ==
	      -RC_EXT_EINTERNAL);
	CHECK(rc_ext_record_send_classification(record, "valid-app", 0, NULL, 1) ==
	      -RC_EXT_EINTERNAL);
	CHECK(rc_ext_record_send_events(record, "valid-app", 0, NULL, 1) ==
	      -RC_EXT_EINTERNAL);
	CHECK(rc_ext_record_send_tracking(record, "valid-app", 0, NULL, 1) ==
	      -RC_EXT_EINTERNAL);
	CHECK(rc_ext_record_send_keypoints(record, "valid-app", 0, NULL, 1) ==
	      -RC_EXT_EINTERNAL);
	CHECK(expect_no_message(pair[1]));
	CHECK(g_send_timeout_ms == RC_EXT_RECORD_IO_TIMEOUT_MS);

	/* Any reset failure poisons the stream. A delayed ACK from an uncertain
	 * request can therefore never confirm a later reset. */
	g_ack_result = -RC_EXT_EAUTH;
	CHECK(rc_ext_record_reset(record, "denied-app") == -RC_EXT_EAUTH);
	message = receive_result(pair[1]);
	CHECK(check_common(message, "denied-app", 0,
	                   INFERENCE_RESULT__DATA__NOT_SET, 0));
	inference_result__free_unpacked(message, NULL);
	g_ack_result = 0;
	CHECK(rc_ext_record_reset(record, "valid-app") == -RC_EXT_EINTERNAL);
	CHECK(rc_ext_record_send_detections(record, "valid-app", 0, NULL, 0) ==
	      -RC_EXT_EINTERNAL);
	CHECK(expect_no_message(pair[1]));

	close(pair[0]);
	g_record_fd = -1;
	rc_ext_record_close(record);
	rc_ext_record_close(NULL);
	CHECK(recv(pair[1], &error, sizeof(error), 0) == 0);
	close(pair[1]);

	printf("PASS record@1 client ABI, validation and protobuf wire data\n");
	return 0;
}
