# kit.runtime.postprocess.ctc

[API 索引](../index.md) · [接口特性与边界](../features.md)

源码基线：[kit/runtime/postprocess/ctc.py](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/postprocess/ctc.py)；签名由 AST 提取，不导入硬件依赖。

OCR CTC 字典加载、blank/重复折叠与置信度解码；字典顺序必须匹配模型。

源码说明中的历史验证记录、迁移目标和旧示例不代表当前固件可用；应用入口、资源准入和接口可用性以本页补充及接口特性文档为准。

## 模块契约（源码）

CTC greedy decoder for PP-OCR text recognition (reCamera Pro). Pure numpy.

Port of the first-gen C++ `TextRecognizer::ctcDecode` / `loadDictionary`
(sscma-example-sg200x/solutions/ppocr-reader/main/text_recognizer.cpp).

The rec rknn takes a 48x320 uint8 RGB text crop ([-1,1] normalization baked in)
and emits a (1, T, C) logit sequence (T=40, C=6625 for the PP-OCRv3 Chinese
model). The class layout is CTC-style:

    index 0            = CTC blank
    index 1 .. N       = characters from ppocr_keys_v1.txt (N = 6623)
    index N+1          = space   (PP-OCR use_space_char=True)

Greedy decode: per time-step argmax, then collapse repeats and drop blanks.

## 常量与类型别名

以下为该版本源码值；默认地址不等于所有固件都开放该服务。

```python
BLANK_INDEX = 0
```


## kit.runtime.postprocess.ctc.load_dictionary

```python
def load_dictionary(dict_path: str) -> List[str]
```

Load a PP-OCR keys file into the full CTC class list.

Returns [blank(''), char_1, ..., char_N, ' '(space)] so that the returned
list length equals the model's number of output classes (6625 for the ch
PP-OCRv3 rec model). Only \r/\n are stripped per line (spaces preserved),
matching PaddleOCR.

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/postprocess/ctc.py#L26)

## kit.runtime.postprocess.ctc.decode

```python
def decode(outputs, dictionary: List[str]) -> Tuple[str, float]
```

CTC greedy decode -> (text, mean confidence of emitted chars).

outputs    : raw rknn outputs (list); outputs[0] is (1,T,C).
dictionary : list from load_dictionary (index -> character).

The PP-OCRv3 rec model ends in a softmax, so the raw output values are
already per-class probabilities. Confidence is therefore the mean of the
raw output value at each emitted (non-blank, non-repeat) time-step -- the
same quantity the first-gen C++ ctcDecode averaged (best_val). We do NOT
re-softmax (that would flatten a peaked 6625-way distribution to ~1/6625).

[实现与参数校验](https://github.com/Seeed-Studio/recamera-pro-ext-api/blob/7b67185f34b9f4ab0e6d60d234c7568a5f94b1e6/kit/runtime/postprocess/ctc.py#L54)
