// Host runtime tests for the internal record@1 bounded socket helpers.
// A local AF_UNIX server covers Hello/HelloAck without privileged endpoints;
// a saturated socketpair proves backpressure cannot block forever.
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "ext_api.pb-c.h"
#include "ext_client_common.h"

#define TEST_ACK_LIMIT 4096u

#define CHECK(expression)                                                      \
	do {                                                                     \
		if (!(expression)) {                                               \
			fprintf(stderr, "CHECK failed %s:%d: %s (errno=%d)\n",      \
			        __FILE__, __LINE__, #expression, errno);                \
			return 1;                                                    \
		}                                                                \
	} while (0)

static int64_t test_monotonic_ms(void) {
	struct timespec now;
	if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
		return -1;
	return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static int make_listener(const char *path) {
	int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return -1;
	struct sockaddr_un address;
	memset(&address, 0, sizeof(address));
	address.sun_family = AF_UNIX;
	if (strlen(path) >= sizeof(address.sun_path)) {
		close(fd);
		errno = ENAMETOOLONG;
		return -1;
	}
	memcpy(address.sun_path, path, strlen(path) + 1);
	if (bind(fd, (struct sockaddr *)&address, sizeof(address)) < 0 ||
	    listen(fd, 1) < 0) {
		close(fd);
		return -1;
	}
	return fd;
}

static void hello_server(int listener, int ack_error, int stall) {
	int client = accept4(listener, NULL, NULL, SOCK_CLOEXEC);
	if (client < 0)
		_exit(10);
	uint8_t buffer[4096];
	ssize_t received = recv(client, buffer, sizeof(buffer), 0);
	if (received <= 0)
		_exit(11);
	Hello *hello = hello__unpack(NULL, (size_t)received, buffer);
	if (!hello || hello->version_min != 1 || hello->version_max != 1 ||
	    !hello->client_name || strcmp(hello->client_name, "bounded-test") != 0)
		_exit(12);
	hello__free_unpacked(hello, NULL);
	if (stall) {
		for (;;)
			pause();
	}

	HelloAck ack = HELLO_ACK__INIT;
	ack.api_version = 1;
	ack.error = ack_error;
	size_t packed_size = hello_ack__get_packed_size(&ack);
	hello_ack__pack(&ack, buffer);
	if (send(client, buffer, packed_size, MSG_NOSIGNAL) !=
	    (ssize_t)packed_size)
		_exit(13);
	close(client);
	close(listener);
	_exit(0);
}

static int wait_server(pid_t child) {
	int status = 0;
	if (waitpid(child, &status, 0) != child)
		return -1;
	return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static int test_success_and_server_error(const char *directory) {
	char path[sizeof(((struct sockaddr_un *)0)->sun_path)];
	CHECK(snprintf(path, sizeof(path), "%s/success.sock", directory) > 0);
	int listener = make_listener(path);
	CHECK(listener >= 0);
	pid_t child = fork();
	CHECK(child >= 0);
	if (child == 0)
		hello_server(listener, 0, 0);

	int error = -1;
	uint32_t api_version = 0;
	int fd = rc_ext_connect_hello_bounded(
	    path, "bounded-test", &api_version, &error, 250);
	CHECK(fd >= 0);
	CHECK(error == RC_EXT_OK && api_version == 1);
	CHECK((fcntl(fd, F_GETFL, 0) & O_NONBLOCK) != 0);
	close(fd);
	close(listener);
	CHECK(wait_server(child) == 0);
	CHECK(unlink(path) == 0);

	CHECK(snprintf(path, sizeof(path), "%s/rejected.sock", directory) > 0);
	listener = make_listener(path);
	CHECK(listener >= 0);
	child = fork();
	CHECK(child >= 0);
	if (child == 0)
		hello_server(listener, RC_EXT_EAUTH, 0);
	error = -1;
	fd = rc_ext_connect_hello_bounded(
	    path, "bounded-test", NULL, &error, 250);
	CHECK(fd < 0 && error == RC_EXT_EAUTH);
	close(listener);
	CHECK(wait_server(child) == 0);
	CHECK(unlink(path) == 0);
	return 0;
}

static int test_ack_timeout(const char *directory) {
	char path[sizeof(((struct sockaddr_un *)0)->sun_path)];
	CHECK(snprintf(path, sizeof(path), "%s/stall.sock", directory) > 0);
	int listener = make_listener(path);
	CHECK(listener >= 0);
	pid_t child = fork();
	CHECK(child >= 0);
	if (child == 0)
		hello_server(listener, 0, 1);

	int error = -1;
	int64_t started = test_monotonic_ms();
	CHECK(started >= 0);
	int fd = rc_ext_connect_hello_bounded(
	    path, "bounded-test", NULL, &error, 50);
	int64_t elapsed = test_monotonic_ms() - started;
	CHECK(fd < 0 && error == RC_EXT_EINTERNAL);
	CHECK(elapsed >= 0 && elapsed < 500);
	CHECK(kill(child, SIGTERM) == 0);
	int status = 0;
	CHECK(waitpid(child, &status, 0) == child);
	CHECK(WIFSIGNALED(status) && WTERMSIG(status) == SIGTERM);
	close(listener);
	CHECK(unlink(path) == 0);
	return 0;
}

static int test_send_timeout(void) {
	int pair[2];
	CHECK(socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, pair) == 0);
	int send_buffer = 4096;
	CHECK(setsockopt(pair[0], SOL_SOCKET, SO_SNDBUF, &send_buffer,
	                 sizeof(send_buffer)) == 0);

	uint8_t packet[1024] = {0};
	int filled = 0;
	for (int i = 0; i < 100000; i++) {
		ssize_t sent = send(pair[0], packet, sizeof(packet),
		                    MSG_DONTWAIT | MSG_NOSIGNAL);
		if (sent == (ssize_t)sizeof(packet)) {
			filled++;
			continue;
		}
		CHECK(sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK));
		break;
	}
	CHECK(filled > 0 && filled < 100000);

	errno = 0;
	int64_t started = test_monotonic_ms();
	CHECK(started >= 0);
	CHECK(rc_ext_send_packet_bounded(pair[0], packet, sizeof(packet), 50) < 0);
	int64_t elapsed = test_monotonic_ms() - started;
	CHECK(errno == ETIMEDOUT);
	CHECK(elapsed >= 0 && elapsed < 500);

	uint8_t received[sizeof(packet)];
	int drained = 0;
	for (;;) {
		ssize_t count = recv(pair[1], received, sizeof(received), MSG_DONTWAIT);
		if (count > 0) {
			drained++;
			continue;
		}
		CHECK(count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK));
		break;
	}
	CHECK(drained == filled);
	CHECK(rc_ext_send_packet_bounded(pair[0], packet, sizeof(packet), 50) == 0);
	close(pair[0]);
	close(pair[1]);
	return 0;
}

static void ack_server(int fd, int ack_error, uint32_t api_version) {
	uint8_t buffer[256];
	if (recv(fd, buffer, sizeof(buffer), 0) <= 0)
		_exit(20);
	HelloAck ack = HELLO_ACK__INIT;
	/* Keep successful protobuf ACKs non-empty so recv(0) remains EOF. */
	ack.api_version = api_version;
	ack.error = ack_error;
	size_t packed_size = hello_ack__get_packed_size(&ack);
	hello_ack__pack(&ack, buffer);
	if (send(fd, buffer, packed_size, MSG_NOSIGNAL) !=
	    (ssize_t)packed_size)
		_exit(21);
	close(fd);
	_exit(0);
}

static int test_request_ack(void) {
	const uint8_t request[] = {0x08, 0x01};
	const struct {
		int error;
		uint32_t version;
		int result;
	} ack_cases[] = {
		{RC_EXT_OK, 1, 0},
		{RC_EXT_EAUTH, 1, -RC_EXT_EAUTH},
		{RC_EXT_OK, 0, -RC_EXT_EVERSION},
	};
	for (size_t index = 0; index < sizeof(ack_cases) / sizeof(ack_cases[0]);
	     index++) {
		int pair[2];
		CHECK(socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, pair) == 0);
		pid_t child = fork();
		CHECK(child >= 0);
		if (child == 0) {
			close(pair[0]);
			ack_server(pair[1], ack_cases[index].error,
			           ack_cases[index].version);
		}
		close(pair[1]);
		int result = rc_ext_send_packet_ack_bounded(
		    pair[0], request, sizeof(request), 250);
		CHECK(result == ack_cases[index].result);
		close(pair[0]);
		CHECK(wait_server(child) == 0);
	}

	int pair[2];
	CHECK(socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, pair) == 0);
	int64_t started = test_monotonic_ms();
	CHECK(started >= 0);
	CHECK(rc_ext_send_packet_ack_bounded(
	          pair[0], request, sizeof(request), 50) == -RC_EXT_EINTERNAL);
	int64_t elapsed = test_monotonic_ms() - started;
	CHECK(elapsed >= 0 && elapsed < 500);
	uint8_t received[sizeof(request)];
	CHECK(recv(pair[1], received, sizeof(received), 0) ==
	      (ssize_t)sizeof(request));
	close(pair[0]);
	close(pair[1]);

	/* MSG_TRUNC must reject an oversized ACK without unpacking beyond the
	 * bounded receive buffer. */
	CHECK(socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, pair) == 0);
	uint8_t oversized_ack[TEST_ACK_LIMIT + 1] = {0};
	CHECK(send(pair[1], oversized_ack, sizeof(oversized_ack), MSG_NOSIGNAL) ==
	      (ssize_t)sizeof(oversized_ack));
	CHECK(rc_ext_send_packet_ack_bounded(
	          pair[0], request, sizeof(request), 250) == -RC_EXT_EFORMAT);
	CHECK(recv(pair[1], received, sizeof(received), 0) ==
	      (ssize_t)sizeof(request));
	close(pair[0]);
	close(pair[1]);
	return 0;
}

int main(void) {
	char directory[] = "/tmp/recamera-bounded-XXXXXX";
	CHECK(mkdtemp(directory) != NULL);
	CHECK(test_success_and_server_error(directory) == 0);
	CHECK(test_ack_timeout(directory) == 0);
	CHECK(test_send_timeout() == 0);
	CHECK(test_request_ack() == 0);
	CHECK(rmdir(directory) == 0);
	printf("PASS bounded record@1 connect, Hello/ACK and backpressure send\n");
	return 0;
}
