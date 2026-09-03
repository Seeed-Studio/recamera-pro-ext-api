// Copyright 2025 reCamera Pro Extension API
// Shared client-side plumbing for the librecamera_ext receivers/sink.
// See ext_client_common.h. The ordinary connect+Hello and recvmsg discipline
// was extracted from frame_recv.c / probe_recv.c / recamera_ext.c without a
// behaviour change; record@1 additionally uses the bounded helpers below.
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "ext_client_common.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
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

static int64_t monotonic_ms(void) {
	struct timespec now;
	if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
		return -1;
	return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static int make_deadline(unsigned int timeout_ms, int64_t *deadline) {
	int64_t now = monotonic_ms();
	if (now < 0)
		return -1;
	*deadline = now + (int64_t)timeout_ms;
	return 0;
}

static int deadline_expired(int64_t deadline) {
	int64_t now = monotonic_ms();
	if (now < 0)
		return -1;
	if (now >= deadline) {
		errno = ETIMEDOUT;
		return 1;
	}
	return 0;
}

static int wait_fd_until(int fd, short events, int64_t deadline) {
	for (;;) {
		int64_t now = monotonic_ms();
		if (now < 0)
			return -1;
		int64_t remaining = deadline - now;
		if (remaining <= 0) {
			errno = ETIMEDOUT;
			return -1;
		}

		struct pollfd pfd = {
			.fd = fd,
			.events = events,
		};
		int timeout = remaining > INT_MAX ? INT_MAX : (int)remaining;
		int ready = poll(&pfd, 1, timeout);
		if (ready > 0) {
			if (pfd.revents & POLLNVAL) {
				errno = EBADF;
				return -1;
			}
			/* Let connect/send/recv report the concrete socket error. */
			if (pfd.revents & (events | POLLERR | POLLHUP))
				return 0;
			continue;
		}
		if (ready == 0) {
			errno = ETIMEDOUT;
			return -1;
		}
		if (errno != EINTR)
			return -1;
	}
}

static int set_nonblocking(int fd) {
	int flags = fcntl(fd, F_GETFL, 0);
	if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0)
		return -1;
	return 0;
}

static int connect_until(int fd, const struct sockaddr *address,
			 socklen_t address_len, int64_t deadline) {
	for (;;) {
		if (deadline_expired(deadline) != 0)
			return -1;
		if (connect(fd, address, address_len) == 0 || errno == EISCONN)
			return 0;

		int connect_error = errno;
		if (connect_error == EINTR) {
			int expired = deadline_expired(deadline);
			if (expired != 0)
				return -1;
			continue;
		}
		if (connect_error == EAGAIN) {
			/* Linux AF_UNIX uses EAGAIN when the listen backlog is full. */
			if (wait_fd_until(fd, POLLOUT, deadline) < 0)
				return -1;
			continue;
		}
		if (connect_error != EINPROGRESS && connect_error != EALREADY) {
			errno = connect_error;
			return -1;
		}
		if (wait_fd_until(fd, POLLOUT, deadline) < 0)
			return -1;

		int socket_error = 0;
		socklen_t error_len = sizeof(socket_error);
		if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &socket_error,
		               &error_len) < 0)
			return -1;
		if (socket_error == 0)
			return 0;
		if (socket_error == EINPROGRESS || socket_error == EALREADY ||
		    socket_error == EAGAIN)
			continue;
		errno = socket_error;
		return -1;
	}
}

static int send_packet_until(int fd, const void *buffer, size_t size,
			     int64_t deadline) {
	if ((!buffer && size != 0) || size > (size_t)SSIZE_MAX) {
		errno = EINVAL;
		return -1;
	}
	for (;;) {
		if (deadline_expired(deadline) != 0)
			return -1;
		ssize_t sent = send(fd, buffer, size, MSG_DONTWAIT | MSG_NOSIGNAL);
		if (sent == (ssize_t)size)
			return 0;
		if (sent >= 0) {
			errno = EIO; /* SEQPACKET must be all-or-nothing. */
			return -1;
		}
		if (errno == EAGAIN || errno == EWOULDBLOCK) {
			if (wait_fd_until(fd, POLLOUT, deadline) < 0)
				return -1;
			continue;
		}
		if (errno != EINTR)
			return -1;
		int expired = deadline_expired(deadline);
		if (expired != 0)
			return -1;
	}
}

static ssize_t recv_packet_until(int fd, void *buffer, size_t size,
				 int64_t deadline) {
	for (;;) {
		if (deadline_expired(deadline) != 0)
			return -1;
		ssize_t received = recv(fd, buffer, size,
		                        MSG_DONTWAIT | MSG_TRUNC);
		if (received >= 0)
			return received;
		if (errno == EAGAIN || errno == EWOULDBLOCK) {
			if (wait_fd_until(fd, POLLIN, deadline) < 0)
				return -1;
			continue;
		}
		if (errno != EINTR)
			return -1;
		int expired = deadline_expired(deadline);
		if (expired != 0)
			return -1;
	}
}

static int receive_hello_ack_until(int fd, uint32_t *api_version,
				   int64_t deadline) {
	uint8_t buffer[RC_EXT_CLIENT_ACK_MAX];
	ssize_t received = recv_packet_until(fd, buffer, sizeof(buffer), deadline);
	if (received < 0)
		return RC_EXT_EINTERNAL;
	if (received == 0)
		return RC_EXT_EVERSION;
	if ((size_t)received > sizeof(buffer))
		return RC_EXT_EFORMAT;
	HelloAck *ack = hello_ack__unpack(NULL, (size_t)received, buffer);
	if (!ack)
		return RC_EXT_EFORMAT;
	int result = RC_EXT_OK;
	if (ack->error != 0)
		result = ack->error > 0 ? ack->error : RC_EXT_EINTERNAL;
	else if (ack->api_version != 1)
		result = RC_EXT_EVERSION;
	else if (api_version)
		*api_version = ack->api_version;
	hello_ack__free_unpacked(ack, NULL);
	return result;
}

int rc_ext_send_packet_bounded(int fd, const void *buffer, size_t size,
			       unsigned int timeout_ms) {
	int64_t deadline;
	if (fd < 0 || make_deadline(timeout_ms, &deadline) < 0) {
		if (fd < 0)
			errno = EBADF;
		return -1;
	}
	return send_packet_until(fd, buffer, size, deadline);
}

int rc_ext_send_packet_ack_bounded(int fd, const void *buffer, size_t size,
				   unsigned int timeout_ms) {
	int64_t deadline;
	if (fd < 0 || make_deadline(timeout_ms, &deadline) < 0)
		return -RC_EXT_EINTERNAL;
	if (send_packet_until(fd, buffer, size, deadline) < 0)
		return -RC_EXT_EINTERNAL;
	int ack_error = receive_hello_ack_until(fd, NULL, deadline);
	return ack_error == RC_EXT_OK ? 0 : -ack_error;
}

int rc_ext_connect_hello_bounded(const char *path, const char *client_name,
				 uint32_t *api_version, int *err,
				 unsigned int timeout_ms) {
	int64_t deadline;
	if (!path || strlen(path) >= sizeof(((struct sockaddr_un *)0)->sun_path) ||
	    make_deadline(timeout_ms, &deadline) < 0)
		return rc_ext_set_err(err, RC_EXT_EINTERNAL);

	int fd = open_seqpacket_cloexec();
	if (fd < 0 || set_nonblocking(fd) < 0) {
		if (fd >= 0)
			close(fd);
		return rc_ext_set_err(err, RC_EXT_EINTERNAL);
	}

	struct sockaddr_un address;
	memset(&address, 0, sizeof(address));
	address.sun_family = AF_UNIX;
	memcpy(address.sun_path, path, strlen(path) + 1);
	if (connect_until(fd, (struct sockaddr *)&address, sizeof(address),
	                  deadline) < 0) {
		close(fd);
		return rc_ext_set_err(err, RC_EXT_EINTERNAL);
	}

	Hello hello = HELLO__INIT;
	hello.version_min = 1;
	hello.version_max = 1;
	hello.client_name = (char *)(client_name ? client_name : "ext");
	hello.auth = (char *)"";
	size_t hello_size = hello__get_packed_size(&hello);
	uint8_t hello_buffer[RC_EXT_CLIENT_ACK_MAX];
	if (hello_size > sizeof(hello_buffer)) {
		close(fd);
		return rc_ext_set_err(err, RC_EXT_EINTERNAL);
	}
	hello__pack(&hello, hello_buffer);
	if (send_packet_until(fd, hello_buffer, hello_size, deadline) < 0) {
		close(fd);
		return rc_ext_set_err(err, RC_EXT_EINTERNAL);
	}

	int ack_error = receive_hello_ack_until(fd, api_version, deadline);
	if (ack_error != RC_EXT_OK) {
		close(fd);
		return rc_ext_set_err(err, (rc_ext_err_t)ack_error);
	}
	if (err)
		*err = RC_EXT_OK;
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
