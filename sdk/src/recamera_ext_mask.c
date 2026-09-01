// Copyright 2025 reCamera Pro Extension API
// librecamera_ext C ABI -- M4 hardware privacy-mask control.
//
// Unlike the result/frame/probe facilities (which use the SEQPACKET protobuf
// sockets under /run/recamera/), mask control is a request/response RPC that
// talks to rkipc's control socket /var/tmp/rkipc using its FunMap wire
// protocol:
//   server -> client: [int handshake]
//   client -> server: [int name_len][name bytes incl. trailing NUL]
//   <handler-specific request/response bytes>
//   server -> client: [int ret]   (appended by the dispatch loop; connection
//                                   is reused, so this trailing int MUST be
//                                   drained after every call)
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "recamera_ext.h"

#include <errno.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "rc_ext_errno.h"

// rkipc control socket (mirror of CS_PATH in the rkipc socket layer).
#define RC_EXT_RKIPC_SOCK "/var/tmp/rkipc"
#define RC_EXT_MASK_MAX   6

struct rc_ext_mask {
	int fd;
};

static double rc_clamp01(double v) {
	if (v < 0.0)
		return 0.0;
	if (v > 1.0)
		return 1.0;
	return v;
}

// Full read/write helpers (the rkipc server speaks plain byte streams).
static int rc_readn(int fd, void *buf, size_t n) {
	uint8_t *p = (uint8_t *)buf;
	size_t got = 0;
	while (got < n) {
		ssize_t r = read(fd, p + got, n - got);
		if (r > 0)
			got += (size_t)r;
		else if (r == 0)
			return -1; // EOF
		else if (errno == EINTR)
			continue;
		else
			return -1;
	}
	return 0;
}

static int rc_writen(int fd, const void *buf, size_t n) {
	const uint8_t *p = (const uint8_t *)buf;
	size_t sent = 0;
	while (sent < n) {
		ssize_t w = send(fd, p + sent, n - sent, MSG_NOSIGNAL);
		if (w > 0)
			sent += (size_t)w;
		else if (w < 0 && errno == EINTR)
			continue;
		else
			return -1;
	}
	return 0;
}

// Sends the RPC verb name (length-prefixed, NUL-terminated so the server's
// strcmp matches).
static int rc_send_verb(int fd, const char *name) {
	int len = (int)strlen(name) + 1;
	if (rc_writen(fd, &len, sizeof(len)) != 0)
		return -1;
	if (rc_writen(fd, name, (size_t)len) != 0)
		return -1;
	return 0;
}

rc_ext_mask_t *rc_ext_mask_open(int *err) {
	int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0) {
		if (err)
			*err = RC_EXT_EINTERNAL;
		return NULL;
	}
	struct sockaddr_un addr;
	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	strncpy(addr.sun_path, RC_EXT_RKIPC_SOCK, sizeof(addr.sun_path) - 1);
	if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		close(fd);
		if (err)
			*err = RC_EXT_EINTERNAL;
		return NULL;
	}
	int handshake = 0;
	if (rc_readn(fd, &handshake, sizeof(handshake)) != 0) {
		close(fd);
		if (err)
			*err = RC_EXT_EINTERNAL;
		return NULL;
	}
	rc_ext_mask_t *h = (rc_ext_mask_t *)calloc(1, sizeof(*h));
	if (!h) {
		close(fd);
		if (err)
			*err = RC_EXT_EINTERNAL;
		return NULL;
	}
	h->fd = fd;
	if (err)
		*err = RC_EXT_OK;
	return h;
}

int rc_ext_mask_update(rc_ext_mask_t *h, const rc_ext_mask_rect_t *rect) {
	if (!h || h->fd < 0 || !rect)
		return -RC_EXT_EFORMAT;
	if (rect->id < 0 || rect->id >= RC_EXT_MASK_MAX)
		return -RC_EXT_EFORMAT;
	int mask_id = rect->id;
	double nx = rc_clamp01(rect->x), ny = rc_clamp01(rect->y);
	double nw = rc_clamp01(rect->w), nh = rc_clamp01(rect->h);

	if (rc_send_verb(h->fd, "osd_manager_update_mask_rect") != 0)
		return -RC_EXT_EINTERNAL;
	if (rc_writen(h->fd, &mask_id, sizeof(mask_id)) != 0 ||
	    rc_writen(h->fd, &nx, sizeof(nx)) != 0 ||
	    rc_writen(h->fd, &ny, sizeof(ny)) != 0 ||
	    rc_writen(h->fd, &nw, sizeof(nw)) != 0 ||
	    rc_writen(h->fd, &nh, sizeof(nh)) != 0)
		return -RC_EXT_EINTERNAL;

	int e_err = 0, ret = 0;
	if (rc_readn(h->fd, &e_err, sizeof(e_err)) != 0)
		return -RC_EXT_EINTERNAL;
	if (rc_readn(h->fd, &ret, sizeof(ret)) != 0)
		return -RC_EXT_EINTERNAL;
	if (e_err != 0)
		return (e_err == -EIO) ? -RC_EXT_EINTERNAL : -RC_EXT_EBUSY;
	return 0;
}

int rc_ext_mask_set(rc_ext_mask_t *h, const rc_ext_mask_rect_t *rects, size_t n, int *applied) {
	if (!h || h->fd < 0)
		return -RC_EXT_EFORMAT;
	int count = (int)n;
	if (count < 0)
		count = 0;
	if (count > RC_EXT_MASK_MAX)
		count = RC_EXT_MASK_MAX;
	int enabled = (count > 0) ? 1 : 0;

	if (rc_send_verb(h->fd, "osd_manager_set_mask_cfg") != 0)
		return -RC_EXT_EINTERNAL;
	if (rc_writen(h->fd, &enabled, sizeof(enabled)) != 0 ||
	    rc_writen(h->fd, &count, sizeof(count)) != 0)
		return -RC_EXT_EINTERNAL;
	for (int i = 0; i < count; i++) {
		int id = rects[i].id;
		double x = rc_clamp01(rects[i].x), y = rc_clamp01(rects[i].y);
		double w = rc_clamp01(rects[i].w), hh = rc_clamp01(rects[i].h);
		if (rc_writen(h->fd, &id, sizeof(id)) != 0 ||
		    rc_writen(h->fd, &x, sizeof(x)) != 0 ||
		    rc_writen(h->fd, &y, sizeof(y)) != 0 ||
		    rc_writen(h->fd, &w, sizeof(w)) != 0 ||
		    rc_writen(h->fd, &hh, sizeof(hh)) != 0)
			return -RC_EXT_EINTERNAL;
	}

	int e_err = 0, ret = 0;
	if (rc_readn(h->fd, &e_err, sizeof(e_err)) != 0)
		return -RC_EXT_EINTERNAL;
	if (rc_readn(h->fd, &ret, sizeof(ret)) != 0)
		return -RC_EXT_EINTERNAL;
	if (e_err < 0)
		return -RC_EXT_EBUSY;
	if (applied)
		*applied = e_err;
	return e_err;
}

int rc_ext_mask_clear(rc_ext_mask_t *h) {
	return rc_ext_mask_set(h, NULL, 0, NULL);
}

int rc_ext_mask_query(rc_ext_mask_t *h, rc_ext_mask_rect_t *out, size_t n) {
	if (!h || h->fd < 0)
		return -RC_EXT_EFORMAT;
	if (rc_send_verb(h->fd, "osd_manager_get_mask_cfg") != 0)
		return -RC_EXT_EINTERNAL;

	int e_err = 0, enabled = 0, total = 0, cnt = 0, ret = 0;
	if (rc_readn(h->fd, &e_err, sizeof(e_err)) != 0)
		return -RC_EXT_EINTERNAL;
	if (rc_readn(h->fd, &enabled, sizeof(enabled)) != 0)
		return -RC_EXT_EINTERNAL;
	if (rc_readn(h->fd, &total, sizeof(total)) != 0)
		return -RC_EXT_EINTERNAL;
	if (rc_readn(h->fd, &cnt, sizeof(cnt)) != 0)
		return -RC_EXT_EINTERNAL;
	for (int i = 0; i < cnt; i++) {
		int id = 0;
		double x = 0, y = 0, w = 0, hh = 0;
		if (rc_readn(h->fd, &id, sizeof(id)) != 0 ||
		    rc_readn(h->fd, &x, sizeof(x)) != 0 ||
		    rc_readn(h->fd, &y, sizeof(y)) != 0 ||
		    rc_readn(h->fd, &w, sizeof(w)) != 0 ||
		    rc_readn(h->fd, &hh, sizeof(hh)) != 0)
			return -RC_EXT_EINTERNAL;
		if (out && (size_t)i < n) {
			out[i].id = id;
			out[i].x = (float)x;
			out[i].y = (float)y;
			out[i].w = (float)w;
			out[i].h = (float)hh;
		}
	}
	if (rc_readn(h->fd, &ret, sizeof(ret)) != 0)
		return -RC_EXT_EINTERNAL;
	if (e_err < 0)
		return -RC_EXT_EBUSY;
	(void)enabled;
	return total;
}

void rc_ext_mask_close(rc_ext_mask_t *h) {
	if (!h)
		return;
	if (h->fd >= 0)
		close(h->fd);
	free(h);
}
