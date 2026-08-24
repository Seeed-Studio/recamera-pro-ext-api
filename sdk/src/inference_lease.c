// Copyright 2025 reCamera Pro Extension API
// Connection-lifetime client for rkipc's external NPU ownership broker.
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "recamera_ext.h"

#include <errno.h>
#include <poll.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "ext_api.pb-c.h"
#include "ext_client_common.h"

#define RC_EXT_INFERENCE_SOCK "/run/recamera/inference-control.sock"
#define RC_EXT_INFERENCE_RESPONSE_MAX 4096u
#define RC_EXT_INFERENCE_DEFAULT_TIMEOUT_MS 10000u
#define RC_EXT_INFERENCE_MAX_TIMEOUT_MS 30000u
#define RC_EXT_INFERENCE_RPC_TIMEOUT_MS 2000u

struct rc_ext_inference_lease {
	int fd;
	uint64_t lease_id;
	uint64_t epoch;
	uint64_t generation;
	uint64_t next_request_id;
	int fallback_builtin;
	char app_id[RC_EXT_INFERENCE_APP_ID_MAX + 1u];
	char instance_id[RC_EXT_INFERENCE_INSTANCE_ID_MAX + 1u];
	pthread_mutex_t lock;
};

static int valid_text(const char *value, size_t max_len) {
	return !value || strnlen(value, max_len + 1u) <= max_len;
}

static int wait_readable(int fd, uint32_t timeout_ms) {
	struct pollfd p = {.fd = fd, .events = POLLIN | POLLHUP | POLLERR, .revents = 0};
	int rc;
	do {
		rc = poll(&p, 1, (int)timeout_ms);
	} while (rc < 0 && errno == EINTR);
	if (rc == 0)
		return -RC_EXT_EBUSY;
	if (rc < 0 || (p.revents & (POLLERR | POLLNVAL)))
		return -RC_EXT_EINTERNAL;
	return 0;
}

// Control replies must never carry descriptors. recvmsg is used instead of
// recv so an accidental or malicious SCM_RIGHTS attachment is closed before
// the response is rejected.
static ssize_t recv_control(int fd, uint8_t *buf, size_t cap) {
	struct iovec iov = {.iov_base = buf, .iov_len = cap};
	union {
		char bytes[CMSG_SPACE(sizeof(int) * 4)];
		struct cmsghdr align;
	} control;
	memset(&control, 0, sizeof(control));
	struct msghdr msg;
	memset(&msg, 0, sizeof(msg));
	msg.msg_iov = &iov;
	msg.msg_iovlen = 1;
	msg.msg_control = control.bytes;
	msg.msg_controllen = sizeof(control.bytes);

	ssize_t n;
	do {
		n = recvmsg(fd, &msg, MSG_CMSG_CLOEXEC);
	} while (n < 0 && errno == EINTR);
	if (n <= 0)
		return n;

	int bad = (msg.msg_flags & (MSG_TRUNC | MSG_CTRUNC)) != 0;
	for (struct cmsghdr *c = CMSG_FIRSTHDR(&msg); c; c = CMSG_NXTHDR(&msg, c)) {
		if (c->cmsg_level != SOL_SOCKET || c->cmsg_type != SCM_RIGHTS)
			continue;
		size_t bytes = c->cmsg_len >= CMSG_LEN(0) ? c->cmsg_len - CMSG_LEN(0) : 0;
		size_t count = bytes / sizeof(int);
		for (size_t i = 0; i < count; ++i) {
			int received_fd = -1;
			memcpy(&received_fd, CMSG_DATA(c) + i * sizeof(int), sizeof(received_fd));
			if (received_fd >= 0)
				close(received_fd);
		}
		bad = 1;
	}
	return bad ? -2 : n;
}

static int send_request(int fd, const InferenceControlRequest *request) {
	size_t size = inference_control_request__get_packed_size(request);
	if (size == 0 || size > RC_EXT_INFERENCE_RESPONSE_MAX)
		return -RC_EXT_EFORMAT;
	uint8_t buf[RC_EXT_INFERENCE_RESPONSE_MAX];
	inference_control_request__pack(request, buf);
	ssize_t sent;
	do {
		sent = send(fd, buf, size, MSG_NOSIGNAL);
	} while (sent < 0 && errno == EINTR);
	return sent == (ssize_t)size ? 0 : -RC_EXT_EINTERNAL;
}

static void fill_status(const InferenceControlResponse *response,
			 rc_ext_inference_status_t *out) {
	if (!out)
		return;
	rc_ext_inference_status_t snapshot;
	memset(&snapshot, 0, sizeof(snapshot));
	snapshot.struct_size = sizeof(snapshot);
	snapshot.state = (uint32_t)response->state;
	snapshot.lease_id = response->lease_id;
	snapshot.epoch = response->epoch;
	snapshot.generation = response->generation;
	snapshot.actual_fps = response->actual_fps;
	snapshot.peer_pid = response->peer_pid;
	snapshot.builtin_enabled = response->builtin_enabled ? 1u : 0u;
	snapshot.handle_present = response->handle_present ? 1u : 0u;
	snapshot.fallback_builtin = response->fallback_builtin ? 1u : 0u;
	snprintf(snapshot.builtin_state, sizeof(snapshot.builtin_state), "%s",
		 response->builtin_state ? response->builtin_state : "");
	snprintf(snapshot.source_id, sizeof(snapshot.source_id), "%s",
		 response->source_id ? response->source_id : "");

	size_t capacity = out->struct_size ? out->struct_size : sizeof(*out);
	if (capacity > sizeof(snapshot))
		capacity = sizeof(snapshot);
	memcpy(out, &snapshot, capacity);
}

static int exchange_locked(rc_ext_inference_lease_t *lease,
			   InferenceControlOp op, uint32_t timeout_ms,
			   rc_ext_inference_status_t *status) {
	if (!lease || lease->fd < 0)
		return -RC_EXT_EINTERNAL;

	InferenceControlRequest request = INFERENCE_CONTROL_REQUEST__INIT;
	request.request_id = ++lease->next_request_id;
	request.op = op;
	request.app_id = lease->app_id;
	request.instance_id = lease->instance_id;
	request.lease_id = lease->lease_id;
	request.epoch = lease->epoch;
	request.timeout_ms = timeout_ms;
	request.fallback_builtin = lease->fallback_builtin;

	int rc = send_request(lease->fd, &request);
	if (rc != 0)
		return rc;
	uint32_t response_timeout = timeout_ms ? timeout_ms : RC_EXT_INFERENCE_RPC_TIMEOUT_MS;
	if (op == INFERENCE_CONTROL_OP__INFERENCE_CONTROL_ACQUIRE)
		response_timeout += 1000u; // allow one server tick after its drain deadline
	rc = wait_readable(lease->fd, response_timeout);
	if (rc != 0)
		return rc;

	uint8_t buf[RC_EXT_INFERENCE_RESPONSE_MAX];
	ssize_t n = recv_control(lease->fd, buf, sizeof(buf));
	if (n == -2)
		return -RC_EXT_EFORMAT;
	if (n <= 0)
		return -RC_EXT_EINTERNAL;
	InferenceControlResponse *response =
		inference_control_response__unpack(NULL, (size_t)n, buf);
	if (!response)
		return -RC_EXT_EFORMAT;
	if (response->request_id != request.request_id || response->op != op) {
		inference_control_response__free_unpacked(response, NULL);
		return -RC_EXT_EFORMAT;
	}
	if (response->error != RC_EXT_OK) {
		rc = response->error > 0 ? -response->error : -RC_EXT_EINTERNAL;
		inference_control_response__free_unpacked(response, NULL);
		return rc;
	}
	if (op == INFERENCE_CONTROL_OP__INFERENCE_CONTROL_ACQUIRE) {
		if (!response->lease_id || !response->epoch) {
			inference_control_response__free_unpacked(response, NULL);
			return -RC_EXT_EFORMAT;
		}
		lease->lease_id = response->lease_id;
		lease->epoch = response->epoch;
		lease->generation = response->generation;
	}
	fill_status(response, status);
	inference_control_response__free_unpacked(response, NULL);
	return 0;
}

rc_ext_inference_lease_t *rc_ext_inference_lease_open(
	const char *app_id, const char *instance_id, uint32_t timeout_ms,
	int fallback_builtin, int *err) {
	if (!valid_text(app_id, RC_EXT_INFERENCE_APP_ID_MAX) ||
	    !valid_text(instance_id, RC_EXT_INFERENCE_INSTANCE_ID_MAX) ||
	    timeout_ms > RC_EXT_INFERENCE_MAX_TIMEOUT_MS) {
		rc_ext_set_err(err, RC_EXT_EFORMAT);
		return NULL;
	}

	const char *socket_path = getenv("RECAMERA_INFERENCE_CONTROL_SOCK");
	if (!socket_path || !socket_path[0])
		socket_path = RC_EXT_INFERENCE_SOCK;
	int connect_err = RC_EXT_OK;
	int fd = rc_ext_connect_hello(socket_path, "inference-lease", NULL,
				      &connect_err);
	if (fd < 0) {
		rc_ext_set_err(err, (rc_ext_err_t)connect_err);
		return NULL;
	}

	rc_ext_inference_lease_t *lease = calloc(1, sizeof(*lease));
	if (!lease) {
		close(fd);
		rc_ext_set_err(err, RC_EXT_EINTERNAL);
		return NULL;
	}
	lease->fd = fd;
	lease->fallback_builtin = fallback_builtin ? 1 : 0;
	lease->next_request_id = 0;
	snprintf(lease->app_id, sizeof(lease->app_id), "%s", app_id ? app_id : "external");
	snprintf(lease->instance_id, sizeof(lease->instance_id), "%s",
		 instance_id ? instance_id : "unspecified");
	if (pthread_mutex_init(&lease->lock, NULL) != 0) {
		close(fd);
		free(lease);
		rc_ext_set_err(err, RC_EXT_EINTERNAL);
		return NULL;
	}

	uint32_t effective = timeout_ms ? timeout_ms : RC_EXT_INFERENCE_DEFAULT_TIMEOUT_MS;
	pthread_mutex_lock(&lease->lock);
	int rc = exchange_locked(lease, INFERENCE_CONTROL_OP__INFERENCE_CONTROL_ACQUIRE,
				 effective, NULL);
	pthread_mutex_unlock(&lease->lock);
	if (rc != 0) {
		pthread_mutex_destroy(&lease->lock);
		close(lease->fd);
		free(lease);
		rc_ext_set_err(err, (rc_ext_err_t)(-rc));
		return NULL;
	}
	if (err)
		*err = RC_EXT_OK;
	return lease;
}

int rc_ext_inference_lease_ready(rc_ext_inference_lease_t *lease) {
	if (!lease)
		return -RC_EXT_EFORMAT;
	pthread_mutex_lock(&lease->lock);
	int rc = exchange_locked(lease, INFERENCE_CONTROL_OP__INFERENCE_CONTROL_READY,
				 RC_EXT_INFERENCE_RPC_TIMEOUT_MS, NULL);
	pthread_mutex_unlock(&lease->lock);
	return rc;
}

int rc_ext_inference_lease_set_fallback(rc_ext_inference_lease_t *lease,
					int fallback_builtin) {
	if (!lease)
		return -RC_EXT_EFORMAT;
	pthread_mutex_lock(&lease->lock);
	int previous = lease->fallback_builtin;
	lease->fallback_builtin = fallback_builtin ? 1 : 0;
	int rc = exchange_locked(lease,
				 INFERENCE_CONTROL_OP__INFERENCE_CONTROL_SET_FALLBACK,
				 RC_EXT_INFERENCE_RPC_TIMEOUT_MS, NULL);
	if (rc != 0)
		lease->fallback_builtin = previous;
	pthread_mutex_unlock(&lease->lock);
	return rc;
}

int rc_ext_inference_lease_status(rc_ext_inference_lease_t *lease,
				  rc_ext_inference_status_t *out) {
	if (!lease || !out || (out->struct_size && out->struct_size < sizeof(uint32_t)))
		return -RC_EXT_EFORMAT;
	pthread_mutex_lock(&lease->lock);
	int rc = exchange_locked(lease, INFERENCE_CONTROL_OP__INFERENCE_CONTROL_STATUS,
				 RC_EXT_INFERENCE_RPC_TIMEOUT_MS, out);
	pthread_mutex_unlock(&lease->lock);
	return rc;
}

int rc_ext_inference_lease_alive(rc_ext_inference_lease_t *lease) {
	if (!lease)
		return -RC_EXT_EFORMAT;
	pthread_mutex_lock(&lease->lock);
	if (lease->fd < 0) {
		pthread_mutex_unlock(&lease->lock);
		return 0;
	}
	struct pollfd p = {.fd = lease->fd, .events = POLLIN | POLLHUP | POLLERR,
			   .revents = 0};
	int rc;
	do {
		rc = poll(&p, 1, 0);
	} while (rc < 0 && errno == EINTR);
	int alive = 1;
	if (rc < 0)
		alive = -RC_EXT_EINTERNAL;
	else if (rc > 0 && (p.revents & (POLLHUP | POLLERR | POLLNVAL)))
		alive = 0;
	else if (rc > 0 && (p.revents & POLLIN)) {
		char byte;
		ssize_t n = recv(lease->fd, &byte, sizeof(byte), MSG_PEEK | MSG_DONTWAIT);
		if (n == 0)
			alive = 0;
		else if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK)
			alive = -RC_EXT_EINTERNAL;
	}
	pthread_mutex_unlock(&lease->lock);
	return alive;
}

void rc_ext_inference_lease_close(rc_ext_inference_lease_t *lease) {
	if (!lease)
		return;
	pthread_mutex_lock(&lease->lock);
	if (lease->fd >= 0) {
		InferenceControlRequest request = INFERENCE_CONTROL_REQUEST__INIT;
		request.request_id = ++lease->next_request_id;
		request.op = INFERENCE_CONTROL_OP__INFERENCE_CONTROL_RELEASE;
		request.lease_id = lease->lease_id;
		request.epoch = lease->epoch;
		request.fallback_builtin = lease->fallback_builtin;
		(void)send_request(lease->fd, &request);
		shutdown(lease->fd, SHUT_RDWR);
		close(lease->fd);
		lease->fd = -1;
	}
	pthread_mutex_unlock(&lease->lock);
	pthread_mutex_destroy(&lease->lock);
	free(lease);
}

void rc_ext_inference_lease_abandon_after_fork(rc_ext_inference_lease_t *lease) {
	if (!lease)
		return;
	// This function is called from pthread_atfork's child hook. Another thread
	// may have owned `lock` at the instant of fork, so taking or destroying that
	// copied mutex can deadlock. The child owns a private copy of this allocation;
	// close only its descriptor reference and discard the copy without emitting
	// RELEASE on the parent's shared socket.
	int fd = lease->fd;
	lease->fd = -1;
	if (fd >= 0)
		close(fd);
	free(lease);
}
