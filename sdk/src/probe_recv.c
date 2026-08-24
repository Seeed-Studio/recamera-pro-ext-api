// Copyright 2025 reCamera Pro Extension API
// librecamera_ext C ABI v1 -- M3 probe observability receiver (spec §4).
//
// Client side of the probe tap: connect + Hello/HelloAck + ProbeSubscribe,
// then receive the server's one-way ProbeData stream. Small samples
// (metrics / postproc.out) travel inline in ProbeData.payload; large tensors
// (preproc.out / npu.raw) arrive as an ordinary memfd carried by SCM_RIGHTS
// with ProbeData.fd_size holding its byte length. Unlike the frame proxy the
// memfd is a plain anonymous file, not a dma-buf: a plain mmap(PROT_READ)
// suffices, no DMA_BUF_IOCTL_SYNC.
//
// Mirrors frame_recv.c's receive discipline; the one structural difference is
// that a ProbeData datagram carries 0 or 1 fd (inline vs memfd), whereas a
// frame datagram always carries exactly 1.
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "recamera_ext.h"

#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "ext_api.pb-c.h"
#include "ext_client_common.h"
#include "rc_ext_errno.h"

#define RC_EXT_PROBE_SOCK "/run/recamera/probe.sock"
#define RC_EXT_ACK_MAX    4096
// Matches the server-side RC_EXT_MSG_MAX (ext_socket.h): the largest single
// SEQPACKET datagram. Sized so an inline ProbeData never trips MSG_TRUNC.
#define RC_EXT_MSG_MAX    (64 * 1024)

struct rc_ext_probe {
	int fd;
	uint32_t api_version;
	uint32_t sample_every;    // effective value the server honours
	uint32_t subscribed_mask; // bit0 preproc, bit1 npu, bit2 post, bit3 metrics
	uint8_t rxbuf[RC_EXT_MSG_MAX];
};

rc_ext_probe_t *rc_ext_probe_open(const char *const *stage_ids, size_t n_stages,
                                  uint32_t sample_every, int *err) {
	if (!stage_ids || n_stages == 0) {
		rc_ext_set_err(err, RC_EXT_EFORMAT);
		return NULL;
	}

	uint32_t api_version = 0;
	int fd = rc_ext_connect_hello(RC_EXT_PROBE_SOCK, "probe-src", &api_version, err);
	if (fd < 0)
		return NULL;

	// --- ProbeSubscribe -> ProbeSubscribeAck ---
	ProbeSubscribe sub = PROBE_SUBSCRIBE__INIT;
	char **ids = (char **)calloc(n_stages, sizeof(char *));
	if (!ids) {
		close(fd);
		rc_ext_set_err(err, RC_EXT_EINTERNAL);
		return NULL;
	}
	for (size_t i = 0; i < n_stages; i++)
		ids[i] = (char *)(stage_ids[i] ? stage_ids[i] : "");
	sub.n_stage_ids = n_stages;
	sub.stage_ids = ids;
	sub.sample_every = sample_every ? sample_every : 1;

	size_t ssz = probe_subscribe__get_packed_size(&sub);
	uint8_t sbuf[RC_EXT_ACK_MAX];
	uint8_t ackbuf[RC_EXT_ACK_MAX];
	if (ssz > sizeof(sbuf)) {
		free(ids);
		close(fd);
		rc_ext_set_err(err, RC_EXT_EINTERNAL);
		return NULL;
	}
	probe_subscribe__pack(&sub, sbuf);
	free(ids);
	if (send(fd, sbuf, ssz, MSG_NOSIGNAL) < 0) {
		close(fd);
		rc_ext_set_err(err, RC_EXT_EINTERNAL);
		return NULL;
	}

	ssize_t sar = recv(fd, ackbuf, sizeof(ackbuf), 0);
	if (sar <= 0) {
		close(fd);
		rc_ext_set_err(err, RC_EXT_EINTERNAL);
		return NULL;
	}
	ProbeSubscribeAck *sack = probe_subscribe_ack__unpack(NULL, (size_t)sar, ackbuf);
	if (!sack) {
		close(fd);
		rc_ext_set_err(err, RC_EXT_EFORMAT);
		return NULL;
	}
	if (sack->error != 0 || sack->subscribed_mask == 0) {
		int e = sack->error ? sack->error : RC_EXT_EFORMAT;
		probe_subscribe_ack__free_unpacked(sack, NULL);
		close(fd);
		rc_ext_set_err(err, (rc_ext_err_t)e);
		return NULL;
	}

	rc_ext_probe_t *h = (rc_ext_probe_t *)calloc(1, sizeof(*h));
	if (!h) {
		probe_subscribe_ack__free_unpacked(sack, NULL);
		close(fd);
		rc_ext_set_err(err, RC_EXT_EINTERNAL);
		return NULL;
	}
	h->fd = fd;
	h->api_version = api_version;
	h->sample_every = sack->sample_every;
	h->subscribed_mask = sack->subscribed_mask;
	probe_subscribe_ack__free_unpacked(sack, NULL);

	if (err)
		*err = RC_EXT_OK;
	return h;
}

int rc_ext_probe_info(rc_ext_probe_t *h, uint32_t *sample_every,
                      uint32_t *subscribed_mask) {
	if (!h)
		return -RC_EXT_EINTERNAL;
	if (sample_every)
		*sample_every = h->sample_every;
	if (subscribed_mask)
		*subscribed_mask = h->subscribed_mask;
	return 0;
}

int rc_ext_probe_next(rc_ext_probe_t *h, rc_ext_probe_sample_t *out, int timeout_ms) {
	if (!h || !out)
		return -RC_EXT_EINTERNAL;

	struct pollfd pfd = {h->fd, POLLIN, 0};
	int pr = poll(&pfd, 1, timeout_ms);
	if (pr < 0)
		return (errno == EINTR) ? 1 : -RC_EXT_EINTERNAL;
	if (pr == 0)
		return 1; // timeout
	if (pfd.revents & (POLLHUP | POLLERR) && !(pfd.revents & POLLIN))
		return -RC_EXT_EINTERNAL;

	int fd = -1;
	ssize_t r = rc_ext_recv_msg_fds(h->fd, h->rxbuf, sizeof(h->rxbuf), 0, &fd);
	if (r == 0)
		return -RC_EXT_EINTERNAL; // orderly EOF (server closed / backpressure)
	if (r < 0)
		return (r == -2) ? -RC_EXT_EFORMAT : -RC_EXT_EINTERNAL;

	ProbeData *pd = probe_data__unpack(NULL, (size_t)r, h->rxbuf);
	if (!pd) {
		if (fd >= 0)
			close(fd);
		return -RC_EXT_EFORMAT;
	}

	memset(out, 0, sizeof(*out));
	out->_fd = -1;
	// probe_data__unpack() gives pd its own copies of stage_id and the inline
	// payload bytes (independent of rxbuf once unpacked). Those must stay valid
	// until the caller releases the sample, so the scalars are copied out now
	// and pd itself is stashed in out->_pb and freed in rc_ext_probe_release.
	out->stage_id = pd->stage_id ? pd->stage_id : "";
	out->seq = pd->seq;
	out->pts_us = pd->pts_us;
	out->flags = pd->flags;

	if (pd->meta) {
		out->has_meta = 1;
		TensorMeta *m = pd->meta;
		out->n_shape = (uint32_t)(m->n_shape < 8 ? m->n_shape : 8);
		for (uint32_t i = 0; i < out->n_shape; i++)
			out->shape[i] = m->shape[i];
		out->dtype = m->dtype;
		out->scale = m->scale;
		out->zero_point = m->zero_point;
		out->layout = m->layout;
		out->fourcc = m->fourcc;
		out->width = m->width;
		out->height = m->height;
		out->stride = m->stride;
	}

	if (pd->fd_size > 0) {
		// Large tensor: payload lives in the memfd carried by SCM_RIGHTS.
		if (fd < 0) {
			probe_data__free_unpacked(pd, NULL);
			return -RC_EXT_EFORMAT;
		}
		size_t len = pd->fd_size;
		void *base = mmap(NULL, len, PROT_READ, MAP_SHARED, fd, 0);
		if (base == MAP_FAILED) {
			close(fd);
			probe_data__free_unpacked(pd, NULL);
			return -RC_EXT_EINTERNAL;
		}
		out->payload = (const uint8_t *)base;
		out->payload_len = len;
		out->_fd = fd;
		out->_base = base;
		out->_map_len = len;
		// stage_id came from pd, which we are about to free: it must remain
		// valid after return, so stash pd on the sample and free it in release.
		out->_pb = pd;
	} else {
		// Inline sample: payload bytes live inside pd (owned by the unpack).
		// Keep pd alive until release; a stray fd here is a protocol violation.
		if (fd >= 0)
			close(fd);
		out->payload = pd->payload.len ? pd->payload.data : (const uint8_t *)"";
		out->payload_len = pd->payload.len;
		out->_pb = pd;
	}
	return 0;
}

void rc_ext_probe_release(rc_ext_probe_t *h, rc_ext_probe_sample_t *s) {
	(void)h;
	if (!s)
		return;
	if (s->_base) {
		munmap(s->_base, s->_map_len);
		s->_base = NULL;
		s->_map_len = 0;
	}
	if (s->_fd >= 0) {
		close(s->_fd);
		s->_fd = -1;
	}
	if (s->_pb) {
		probe_data__free_unpacked((ProbeData *)s->_pb, NULL);
		s->_pb = NULL;
	}
	s->payload = NULL;
	s->payload_len = 0;
	s->stage_id = NULL;
}

void rc_ext_probe_close(rc_ext_probe_t *h) {
	if (!h)
		return;
	if (h->fd >= 0)
		close(h->fd);
	free(h);
}
