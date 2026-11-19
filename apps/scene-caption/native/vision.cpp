#include <cstddef>
#include <cstdint>
#include <vector>
#include <string>
#include <rknn_api.h>

namespace {
constexpr size_t pixels = 448 * 448 * 3;
constexpr size_t tokens = 1025 * 1024;
constexpr size_t embedding = 256 * 896;

int run_stage(const char *path, void *data, size_t elements, bool is_image,
              float *features, size_t output_count) {
    rknn_context ctx{};
    // With size zero RKNN loads the file itself. The caller does not retain
    // a second in-memory copy of these weights while the context is active.
    int rc = rknn_init(&ctx, const_cast<char *>(path), 0, 0, nullptr);
    if (rc != RKNN_SUCC) return rc;
    rknn_tensor_attr in{}, out{};
    rc = rknn_query(ctx, RKNN_QUERY_INPUT_ATTR, &in, sizeof(in));
    if (!rc) rc = rknn_query(ctx, RKNN_QUERY_OUTPUT_ATTR, &out, sizeof(out));
    if (!rc && (in.n_elems != elements || out.n_elems != output_count)) rc = -1;

    rknn_input input{};
    input.buf = data;
    input.size = elements * (is_image ? sizeof(uint8_t) : sizeof(float));
    input.type = is_image ? RKNN_TENSOR_UINT8 : RKNN_TENSOR_FLOAT32;
    input.fmt = is_image ? RKNN_TENSOR_NHWC : in.fmt;
    if (!rc) rc = rknn_inputs_set(ctx, 1, &input);
    if (!rc) rc = rknn_run(ctx, nullptr);

    rknn_output output{};
    output.want_float = 1;
    output.is_prealloc = 1;
    output.buf = features;
    output.size = output_count * sizeof(float);
    if (!rc) {
        rc = rknn_outputs_get(ctx, 1, &output, nullptr);
        if (!rc) rc = rknn_outputs_release(ctx, 1, &output);
    }
    int closed = rknn_destroy(ctx);
    return rc ? rc : closed;
}
}

extern "C" int caption_encode(const char *directory, uint8_t *rgb, size_t rgb_count,
                              float *result, size_t count) {
    if (!directory || !rgb || !result || rgb_count != pixels || count != embedding) return -1;
    std::vector<float> a(tokens), b(tokens);
    void *input = rgb;
    for (int i = 0; i < 6; ++i) {
        const bool first = i == 0;
        const bool last = i == 5;
        float *output = last ? result : (i % 2 == 0 ? a.data() : b.data());
        std::string path = std::string(directory) + "/vision-" + std::to_string(i) + ".rknn";
        int rc = run_stage(path.c_str(), input, first ? pixels : tokens, first,
                           output, last ? embedding : tokens);
        if (rc) return rc;
        input = output;
    }
    return 0;
}
