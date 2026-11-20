#include <cstddef>
#include <string>
#include <rkllm.h>

using TokenCallback = void (*)(const char *text);

struct Generation {
    TokenCallback on_token;
    bool failed;
};

static int forward_token(RKLLMResult *result, void *context, LLMCallState state) {
    if (!context) return 0;
    auto &generation = *static_cast<Generation *>(context);
    if (state == RKLLM_RUN_ERROR) generation.failed = true;
    if (state == RKLLM_RUN_NORMAL && result && result->text && *result->text) {
        generation.on_token(result->text);
    }
    return 0;
}

extern "C" int caption_open(const char *path, int max_new_tokens, LLMHandle *handle) {
    if (!path || !handle || max_new_tokens < 8 || max_new_tokens > 512) return -1;
    RKLLMParam p = rkllm_createDefaultParam();
    p.model_path = path;
    // Reserve room for 256 visual tokens, the UTF-8 prompt and chat markers.
    p.max_context_len = max_new_tokens <= 128 ? 1024 : 2048;
    p.extend_param.embed_flash = 1;
    p.max_new_tokens = max_new_tokens;
    p.top_k = 1;
    p.skip_special_token = true;
    p.is_async = false;
    p.img_start = "<img>";
    p.img_end = "</img>";
    p.img_content = "<IMG_CONTEXT>";
    int status = rkllm_init(handle, &p, forward_token);
    if (status != 0) return status;
    status = rkllm_set_chat_template(*handle,
        "<|im_start|>system\n你是一个图像描述助手。<|im_end|>\n",
        "<|im_start|>user\n", "<|im_end|>\n<|im_start|>assistant\n");
    if (status != 0) {
        rkllm_destroy(*handle);
        *handle = nullptr;
    }
    return status;
}

extern "C" int caption_generate(LLMHandle handle, float *image, size_t count,
                                const char *instruction, TokenCallback on_token) {
    if (!handle || !image || count != 256 * 896 || !instruction || !*instruction || !on_token) return -1;
    std::string prompt = "<image>\n";
    prompt += instruction;
    RKLLMInput input{};
    input.input_type = RKLLM_INPUT_MULTIMODAL;
    input.role = "user";
    input.multimodal_input = {&prompt[0], image, 256, 1, 448, 448};
    RKLLMInferParam params{};
    params.mode = RKLLM_INFER_GENERATE;
    params.keep_history = 0;
    Generation generation{on_token, false};
    const int status = rkllm_run(handle, &input, &params, &generation);
    return status ? status : (generation.failed ? -1 : 0);
}

extern "C" int caption_close(LLMHandle handle) {
    return rkllm_destroy(handle);
}
