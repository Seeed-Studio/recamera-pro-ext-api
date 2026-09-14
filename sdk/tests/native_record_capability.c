// Exercise the actual HelloAck parser over a socketpair, without device paths.
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "../src/ext_client_common.c"
#include <stdio.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "failure at %d: %s\n", __LINE__, #x); return 1; } } while (0)

static int check_capability(const char *name, unsigned version, int expected) {
    int pair[2];
    CHECK(socketpair(AF_UNIX, SOCK_SEQPACKET, 0, pair) == 0);
    Capability cap = CAPABILITY__INIT;
    cap.name = (char *)(name ? name : "");
    cap.version = version;
    Capability *caps[] = {&cap};
    HelloAck ack = HELLO_ACK__INIT;
    ack.api_version = 1;
    ack.capabilities = caps;
    ack.n_capabilities = name ? 1 : 0;
    unsigned char wire[512];
    size_t n = hello_ack__pack(&ack, wire);
    CHECK(send(pair[0], wire, n, MSG_NOSIGNAL) == (ssize_t)n);
    int64_t deadline;
    CHECK(make_deadline(100, &deadline) == 0);
    CHECK(receive_hello_ack_until(pair[1], NULL, deadline, "record-delivery") == expected);
    // Queue-clear acknowledgements intentionally do not need capability lists.
    ack.n_capabilities = 0;
    n = hello_ack__pack(&ack, wire);
    CHECK(send(pair[0], wire, n, MSG_NOSIGNAL) == (ssize_t)n);
    CHECK(receive_hello_ack_until(pair[1], NULL, deadline, NULL) == RC_EXT_OK);
    close(pair[0]); close(pair[1]);
    return 0;
}
int main(void) {
    CHECK(check_capability(NULL, 0, RC_EXT_EVERSION) == 0);
    CHECK(check_capability("record", 1, RC_EXT_EVERSION) == 0);
    CHECK(check_capability("record-delivery", 2, RC_EXT_EVERSION) == 0);
    CHECK(check_capability("record-delivery", 1, RC_EXT_OK) == 0);
    puts("PASS new recording capability rejects old/absent versions; reset ACK remains queue-only");
    return 0;
}
