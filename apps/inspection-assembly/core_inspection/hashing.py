"""制品摘要。运行时各处（core 与 backends）共用一份实现。

单独成一个模块而不是塞进 `types.py`：backends 只需要这一个函数，
不该为了算个 sha256 把 core 的数据类型都拉进来。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_BYTES = 1 << 20


def sha256_of(path: str | Path) -> str:
    """按 1 MiB 分块读，返回小写 hex 摘要。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()
