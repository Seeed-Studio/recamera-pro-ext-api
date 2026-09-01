// Copyright 2025 reCamera Pro Extension API
// Shared client-side plumbing for the librecamera_ext receivers/sink:
//   - the connect + Hello/HelloAck handshake (rc_ext_connect_hello)
//   - the SCM_RIGHTS recvmsg discipline    (rc_ext_recv_msg_fds)
//   - the set_err helper                   (rc_ext_set_err)
// Internal build-only header; not part of the public ABI (recamera_ext.h).
#ifndef RC_EXT_CLIENT_COMMON_H
#define RC_EXT_CLIENT_COMMON_H

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <stddef.h>
#include <stdint.h>
#include <sys/socket.h>
#include <sys/types.h>

#include "rc_ext_errno.h"

// send()/recvmsg() portability fallbacks. <sys/socket.h> is included first
// (above) so that, on GNU/glibc where these are enum constants defined only
// under _GNU_SOURCE, the guards see the real definitions and skip the fallback
// -- defining them before <sys/socket.h> would clobber that enum.
#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif
#ifndef MSG_CMSG_CLOEXEC
#define MSG_CMSG_CLOEXEC 0
#endif

#ifdef __cplusplus
extern "C" {
#endif

// These helpers are internal to the DSO (they replace what used to be static
// functions in each source file). Hide them from the dynamic symbol table so
// the public export surface stays exactly the rc_ext_frame/result/probe ABI.
#if defined(__GNUC__) || defined(__clang__)
#define RC_EXT_INTERNAL __attribute__((visibility("hidden")))
#else
#define RC_EXT_INTERNAL
#endif

// Sets *err to code when err != NULL and returns -1 (convenience for the error
// paths of the client openers).
RC_EXT_INTERNAL int rc_ext_set_err(int *err, rc_ext_err_t code);

// Opens an AF_UNIX SOCK_SEQPACKET socket, connects it to `path`, and performs
// the Hello -> HelloAck handshake with the given client_name (NULL -> "ext").
// On success returns the connected fd (>= 0) and, if api_version != NULL,
// writes the negotiated api_version. On failure closes the fd, sets *err to an
// rc_ext_err_t (EINTERNAL transport, EVERSION no ack, EFORMAT bad ack, or the
// server-reported error), and returns -1.
RC_EXT_INTERNAL int rc_ext_connect_hello(const char *path, const char *client_name,
                         uint32_t *api_version, int *err);

// Receives one SEQPACKET datagram into buf plus any SCM_RIGHTS fds.
// require_exactly_one: 1 -> exactly one fd is required (frame proxy); 0 -> 0 or
// 1 fd is allowed (probe tap). Returns the payload byte count (> 0), 0 on EOF,
// -1 on transport error, or -2 on a protocol violation (wrong fd count or a
// truncated control message; all received fds are closed first). *out_fd is set
// to the single fd or -1.
RC_EXT_INTERNAL ssize_t rc_ext_recv_msg_fds(int sock, void *buf, size_t buflen,
                            int require_exactly_one, int *out_fd);

#ifdef __cplusplus
}
#endif

#endif // RC_EXT_CLIENT_COMMON_H
