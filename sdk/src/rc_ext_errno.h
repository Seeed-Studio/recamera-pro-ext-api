// Copyright 2025 reCamera Pro Extension API (stage 3b)
// Unified error codes exposed by the extension API and SDK (spec §1.3).
#ifndef RC_EXT_ERRNO_H
#define RC_EXT_ERRNO_H

typedef enum {
	RC_EXT_OK            = 0,
	RC_EXT_EVERSION      = 1, // version interval has no intersection
	RC_EXT_EAUTH         = 2, // source_id spoofing / invalid token
	RC_EXT_EBUSY         = 3, // subscriber / connection limit reached
	RC_EXT_EFORMAT       = 4, // unsupported format / message parse failure
	RC_EXT_EBACKPRESSURE = 5, // slow consumer dropped
	RC_EXT_ERATELIMIT    = 6, // message-rate / quota exceeded
	RC_EXT_EINTERNAL     = 7, // internal server error
} rc_ext_err_t;

#endif // RC_EXT_ERRNO_H
