// Copyright 2025 reCamera Pro Extension API
// Shared client-side plumbing for the librecamera_ext receivers/sink.
// See ext_client_common.h. Extracted verbatim from the connect+Hello handshake
// and recvmsg discipline that frame_recv.c / probe_recv.c / recamera_ext.c
// previously duplicated -- behaviour is identical; only the location changed.
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "ext_client_common.h"

#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "ext_api.pb-c.h"

// Largest Hello / HelloAck datagram we send or expect (spec §handshake).
#define RC_EXT_CLIENT_ACK_MAX 4096

int rc_ext_set_err(int *err, rc_ext_err_t code) {
	if (err)
		*err = (int)code;
	return -1;
}

static int open_seqpacket_cloexec(void) {
	int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
	if (fd < 0 && errno == EINVAL) {
		fd = socket(AF_UNIX, SOCK_SEQPACKET, 0);
		if (fd >= 0)
			(void)fcntl(fd, F_SETFD, FD_CLOEXEC);
	}
	return fd;
}

int rc_ext_connect_hello(const char *path, const char *client_name,
                         uint32_t *api_version, int *err) {
	int fd = open_seqpacket_cloexec();
	if (fd < 0)
		return rc_ext_set_err(err, RC_EXT_EINTERNAL);

	struct sockaddr_un a;
	memset(&a, 0, sizeof(a));
	a.sun_family = AF_UNIX;
	strncpy(a.sun_path, path, sizeof(a.sun_path) - 1);
	if (connect(fd, (struct sockaddr *)&a, sizeof(a)) < 0) {
		close(fd);
		return rc_ext_set_err(err, RC_EXT_EINTERNAL);
	}

	// --- Hello -> HelloAck ---
	Hello hello = HELLO__INIT;
	hello.version_min = 1;
	hello.version_max = 1;
	hello.client_name = (char *)(client_name ? client_name : "ext");
	hello.auth = (char *)"";
	size_t hsz = hello__get_packed_size(&hello);
	uint8_t hbuf[RC_EXT_CLIENT_ACK_MAX];
	if (hsz > sizeof(hbuf)) {
		close(fd);
		return rc_ext_set_err(err, RC_EXT_EINTERNAL);
	}
	hello__pack(&hello, hbuf);
	if (send(fd, hbuf, hsz, MSG_NOSIGNAL) < 0) {
		close(fd);
		return rc_ext_set_err(err, RC_EXT_EINTERNAL);
	}

	uint8_t ackbuf[RC_EXT_CLIENT_ACK_MAX];
	ssize_t ar = recv(fd, ackbuf, sizeof(ackbuf), 0);
	if (ar <= 0) {
		close(fd);
		return rc_ext_set_err(err, RC_EXT_EVERSION);
	}
	HelloAck *hack = hello_ack__unpack(NULL, (size_t)ar, ackbuf);
	if (!hack) {
		close(fd);
		return rc_ext_set_err(err, RC_EXT_EFORMAT);
	}
	if (hack->error != 0) {
		int e = hack->error;
		hello_ack__free_unpacked(hack, NULL);
		close(fd);
		return rc_ext_set_err(err, (rc_ext_err_t)e);
	}
	if (api_version)
		*api_version = hack->api_version;
	hello_ack__free_unpacked(hack, NULL);
	return fd;
}

ssize_t rc_ext_recv_msg_fds(int sock, void *buf, size_t buflen,
                            int require_exactly_one, int *out_fd) {
	*out_fd = -1;
	struct iovec iov = {buf, buflen};
	struct msghdr msg;
	memset(&msg, 0, sizeof(msg));
	msg.msg_iov = &iov;
	msg.msg_iovlen = 1;
	union {
		char buf[CMSG_SPACE(sizeof(int) * 4)];
		struct cmsghdr align;
	} u;
	memset(&u, 0, sizeof(u));
	msg.msg_control = u.buf;
	msg.msg_controllen = sizeof(u.buf);

	ssize_t r = recvmsg(sock, &msg, MSG_CMSG_CLOEXEC);
	if (r <= 0)
		return r;

	int fds[8];
	int nfds = 0;
	for (struct cmsghdr *c = CMSG_FIRSTHDR(&msg); c; c = CMSG_NXTHDR(&msg, c)) {
		if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) {
			int cnt = (int)((c->cmsg_len - CMSG_LEN(0)) / sizeof(int));
			for (int i = 0; i < cnt && nfds < (int)(sizeof(fds) / sizeof(fds[0])); i++) {
				int got;
				memcpy(&got, CMSG_DATA(c) + i * sizeof(int), sizeof(int));
				fds[nfds++] = got;
			}
		}
	}
	// frame proxy requires exactly one fd; probe tap allows 0 or 1. Either way
	// a truncated control message is a protocol violation.
	int bad = (msg.msg_flags & MSG_CTRUNC) ||
	          (require_exactly_one ? (nfds != 1) : (nfds > 1));
	if (bad) {
		for (int i = 0; i < nfds; i++)
			close(fds[i]);
		return -2;
	}
	if (nfds == 1)
		*out_fd = fds[0];
	return r;
}
