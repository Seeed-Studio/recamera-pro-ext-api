// Copyright 2025 reCamera Pro Extension API
// librecamera_ext C ABI v1 -- M2 frame-source receiver (spec §2.5).
//
// Client side of the frame proxy: connect + Hello/HelloAck + FrameSubscribe,
// then receive frames (96-byte header + one dma-buf fd via SCM_RIGHTS), mmap
// with dma-buf cache sync, and release by seq. This is the reference
// implementation of the "receive discipline" in spec §2.3 so that solution
// vendors never touch cmsg / ioctl plumbing.
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "recamera_ext.h"

#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "ext_api.pb-c.h"
#include "ext_client_common.h"
#include "rc_ext_errno.h"

#define RC_EXT_FRAME_SOCK "/run/recamera/frame.sock"
#define RC_EXT_ACK_MAX    4096

// dma-buf cache sync ioctl: _IOW('b', 0, __u64). Same encoding on this arch as
// the on-device Python reference (0x40086200).
#ifndef DMA_BUF_IOCTL_SYNC
#define DMA_BUF_IOCTL_SYNC 0x40086200u
#endif
#define DMA_BUF_SYNC_READ  (1u << 0)
#define DMA_BUF_SYNC_START (0u << 2)
#define DMA_BUF_SYNC_END   (1u << 2)

// Wire header (spec §2.3), must match src/rv1126b_ipc/video/frame_export.h.
struct fe_wire_plane {
	uint32_t offset;
	uint32_t stride;
	uint32_t vstride;
} __attribute__((packed));

struct fe_wire_hdr {
	uint32_t magic;
	uint16_t ver;
	uint16_t flags;
	uint64_t seq;
	uint64_t pts_us;
	uint32_t width, height;
	uint32_t fourcc;
	uint32_t buf_size;
	uint8_t chn_id;
	uint8_t n_planes;
	uint16_t reserved0;
	struct fe_wire_plane plane[3];
	uint8_t reserved[16];
} __attribute__((packed));

_Static_assert(sizeof(struct fe_wire_hdr) == 96, "frame_hdr ABI");

struct rc_ext_frame {
	int fd;
	uint32_t api_version;
	uint32_t width, height, fourcc, pool_depth, max_outstanding;
};

static void dma_sync(int fd, uint64_t flags) {
	// Best-effort: some allocators return ENOTTY; access still works but may be
	// stale on non-coherent buffers. Mirrors the on-device reference behaviour.
	(void)ioctl(fd, DMA_BUF_IOCTL_SYNC, &flags);
}

rc_ext_frame_t *rc_ext_frame_open(const rc_ext_frame_cfg_t *cfg, int *err) {
	uint32_t api_version = 0;
	int fd = rc_ext_connect_hello(RC_EXT_FRAME_SOCK, "frame-src", &api_version, err);
	if (fd < 0)
		return NULL;

	// --- FrameSubscribe -> FrameSubscribeAck ---
	// fps_divisor defaults to 1 (== "every frame", spec §2.2). Keeping it
	// non-zero also guarantees the packed message is never empty: a 0-byte
	// SOCK_SEQPACKET datagram is indistinguishable from EOF at the server's
	// recv() and would be treated as a disconnect.
	FrameSubscribe sub = FRAME_SUBSCRIBE__INIT;
	sub.fps_divisor = 1;
	if (cfg) {
		sub.width = cfg->width;
		sub.height = cfg->height;
		sub.fourcc = cfg->fourcc;
		if (cfg->fps_divisor)
			sub.fps_divisor = cfg->fps_divisor;
	}
	size_t ssz = frame_subscribe__get_packed_size(&sub);
	uint8_t sbuf[RC_EXT_ACK_MAX];
	uint8_t ackbuf[RC_EXT_ACK_MAX];
	if (ssz > sizeof(sbuf)) {
		close(fd);
		rc_ext_set_err(err, RC_EXT_EINTERNAL);
		return NULL;
	}
	frame_subscribe__pack(&sub, sbuf);
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
	FrameSubscribeAck *sack = frame_subscribe_ack__unpack(NULL, (size_t)sar, ackbuf);
	if (!sack) {
		close(fd);
		rc_ext_set_err(err, RC_EXT_EFORMAT);
		return NULL;
	}
	if (sack->error != 0) {
		int e = sack->error;
		frame_subscribe_ack__free_unpacked(sack, NULL);
		close(fd);
		rc_ext_set_err(err, (rc_ext_err_t)e);
		return NULL;
	}

	rc_ext_frame_t *h = (rc_ext_frame_t *)calloc(1, sizeof(*h));
	if (!h) {
		frame_subscribe_ack__free_unpacked(sack, NULL);
		close(fd);
		rc_ext_set_err(err, RC_EXT_EINTERNAL);
		return NULL;
	}
	h->fd = fd;
	h->api_version = api_version;
	h->width = sack->width;
	h->height = sack->height;
	h->fourcc = sack->fourcc;
	h->pool_depth = sack->pool_depth;
	h->max_outstanding = sack->max_outstanding;
	frame_subscribe_ack__free_unpacked(sack, NULL);

	if (err)
		*err = RC_EXT_OK;
	return h;
}

int rc_ext_frame_geometry(rc_ext_frame_t *h, uint32_t *width, uint32_t *height,
                          uint32_t *fourcc, uint32_t *pool_depth, uint32_t *max_outstanding) {
	if (!h)
		return -RC_EXT_EINTERNAL;
	if (width)
		*width = h->width;
	if (height)
		*height = h->height;
	if (fourcc)
		*fourcc = h->fourcc;
	if (pool_depth)
		*pool_depth = h->pool_depth;
	if (max_outstanding)
		*max_outstanding = h->max_outstanding;
	return 0;
}

int rc_ext_frame_next(rc_ext_frame_t *h, rc_ext_frame_buf_t *out, int timeout_ms) {
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

	struct fe_wire_hdr wire;
	int fd = -1;
	ssize_t r = rc_ext_recv_msg_fds(h->fd, &wire, sizeof(wire), 1, &fd);
	if (r == 0)
		return -RC_EXT_EINTERNAL; // orderly EOF (server closed / backpressure)
	if (r < 0)
		return (r == -2) ? -RC_EXT_EFORMAT : -RC_EXT_EINTERNAL;

	if (r != (ssize_t)sizeof(wire) || wire.magic != RC_EXT_FRAME_MAGIC ||
	    wire.ver != RC_EXT_FRAME_VER) {
		if (fd >= 0)
			close(fd);
		return -RC_EXT_EFORMAT;
	}

	memset(out, 0, sizeof(*out));
	out->seq = wire.seq;
	out->pts_us = wire.pts_us;
	out->width = wire.width;
	out->height = wire.height;
	out->fourcc = wire.fourcc;
	out->buf_size = wire.buf_size;
	out->flags = wire.flags;
	out->chn_id = wire.chn_id;
	out->n_planes = wire.n_planes;
	for (int i = 0; i < 3; i++) {
		out->plane[i].offset = wire.plane[i].offset;
		out->plane[i].stride = wire.plane[i].stride;
		out->plane[i].vstride = wire.plane[i].vstride;
	}
	out->fd = fd;
	out->_base = NULL;
	out->_map_len = 0;
	return 0;
}

void *rc_ext_frame_map(rc_ext_frame_t *h, rc_ext_frame_buf_t *f) {
	(void)h;
	if (!f || f->fd < 0)
		return NULL;
	if (f->_base)
		return (uint8_t *)f->_base + f->plane[0].offset; // already mapped
	size_t len = f->buf_size ? f->buf_size : 0;
	if (len == 0)
		return NULL;
	void *base = mmap(NULL, len, PROT_READ, MAP_SHARED, f->fd, 0);
	if (base == MAP_FAILED)
		return NULL;
	f->_base = base;
	f->_map_len = len;
	dma_sync(f->fd, DMA_BUF_SYNC_START | DMA_BUF_SYNC_READ);
	return (uint8_t *)base + f->plane[0].offset;
}

void rc_ext_frame_release(rc_ext_frame_t *h, rc_ext_frame_buf_t *f) {
	if (!f)
		return;
	if (f->_base) {
		dma_sync(f->fd, DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ);
		munmap(f->_base, f->_map_len);
		f->_base = NULL;
		f->_map_len = 0;
	}
	if (h && h->fd >= 0) {
		uint64_t seq = f->seq;
		(void)send(h->fd, &seq, sizeof(seq), MSG_NOSIGNAL); // release
	}
	if (f->fd >= 0) {
		close(f->fd);
		f->fd = -1;
	}
}

void rc_ext_frame_close(rc_ext_frame_t *h) {
	if (!h)
		return;
	if (h->fd >= 0)
		close(h->fd);
	free(h);
}
