// Compile-only ABI layout gate for the supported LP64 hosts and RV1126B target.
#include "recamera_ext.h"

#include <stddef.h>

#define ABI_ASSERT(expr) _Static_assert((expr), #expr)

ABI_ASSERT(sizeof(void *) == 8);
ABI_ASSERT(sizeof(size_t) == 8);

ABI_ASSERT(sizeof(rc_ext_box_t) == 40);
ABI_ASSERT(sizeof(rc_ext_class_t) == 40);
ABI_ASSERT(sizeof(rc_ext_seg_t) == 48);
ABI_ASSERT(sizeof(rc_ext_track_t) == 40);
ABI_ASSERT(sizeof(rc_ext_point_t) == 16);
ABI_ASSERT(sizeof(rc_ext_kpinstance_t) == 56);
ABI_ASSERT(sizeof(rc_ext_plane_t) == 12);
ABI_ASSERT(sizeof(rc_ext_frame_buf_t) == 96);
ABI_ASSERT(sizeof(rc_ext_frame_cfg_t) == 16);
ABI_ASSERT(sizeof(rc_ext_probe_sample_t) == 144);
ABI_ASSERT(sizeof(rc_ext_inference_status_t) == 128);
ABI_ASSERT(sizeof(rc_ext_mask_rect_t) == 20);

ABI_ASSERT(offsetof(rc_ext_box_t, label) == 24);
ABI_ASSERT(offsetof(rc_ext_class_t, label) == 8);
ABI_ASSERT(offsetof(rc_ext_seg_t, mask) == 32);
ABI_ASSERT(offsetof(rc_ext_kpinstance_t, label) == 32);
ABI_ASSERT(offsetof(rc_ext_frame_buf_t, fd) == 72);
ABI_ASSERT(offsetof(rc_ext_probe_sample_t, _fd) == 116);
ABI_ASSERT(offsetof(rc_ext_inference_status_t, source_id) == 60);
