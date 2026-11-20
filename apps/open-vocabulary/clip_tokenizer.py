"""CLIP byte BPE, with the Unicode token boundaries used by CLIPTokenizer.

The vocabulary/merge tables are upstream data; no transformers or torch is
needed on the camera. ftfy handles the same text cleanup as the CLIP encoder.
"""
from functools import lru_cache
import html
import json
from pathlib import Path
import unicodedata

import ftfy
import numpy as np


def _pieces(text):
    """Scan CLIP's letter/number/punctuation pattern without a regex wheel."""
    i = 0
    special = ("<|startoftext|>", "<|endoftext|>", "'s", "'t", "'re",
               "'ve", "'m", "'ll", "'d")
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        token = next((s for s in special if text.startswith(s, i)), None)
        if token is not None:
            yield token
            i += len(token)
            continue
        start = i
        kind = unicodedata.category(text[i])[0]
        i += 1
        if kind == "L":
            while i < len(text) and unicodedata.category(text[i]).startswith("L"):
                i += 1
        elif kind != "N":
            while (i < len(text) and not text[i].isspace()
                   and unicodedata.category(text[i])[0] not in ("L", "N")):
                i += 1
        yield text[start:i]


class ClipTokenizer:
    sequence_length = 20
    start_id = 49406
    end_id = 49407

    def __init__(self, directory):
        directory = Path(directory)
        self.vocab = json.loads((directory / "vocab.json").read_text("utf-8"))
        lines = (directory / "merges.txt").read_text("utf-8").splitlines()
        self.ranks = {tuple(line.split()): i for i, line in enumerate(lines[1:])
                      if line.strip()}
        visible = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
        extra = [b for b in range(256) if b not in visible]
        self.byte_chars = dict(zip(visible + extra,
                                   map(chr, visible + list(range(256, 256 + len(extra))))))

    @lru_cache(maxsize=1024)
    def _bpe(self, token):
        if token in ("<|startoftext|>", "<|endoftext|>"):
            return (self.vocab[token],)
        word = tuple(token[:-1]) + (token[-1] + "</w>",)
        while len(word) > 1:
            pairs = set(zip(word, word[1:]))
            pair = min(pairs, key=lambda p: self.ranks.get(p, float("inf")))
            if pair not in self.ranks:
                break
            merged = []
            i = 0
            while i < len(word):
                if i + 1 < len(word) and word[i:i + 2] == pair:
                    merged.append(word[i] + word[i + 1])
                    i += 2
                else:
                    merged.append(word[i])
                    i += 1
            word = tuple(merged)
        return tuple(self.vocab[p] for p in word)

    def encode(self, text):
        text = " ".join(html.unescape(html.unescape(ftfy.fix_text(text))).split()).lower()
        ids = []
        for piece in _pieces(text):
            encoded = "".join(self.byte_chars[b] for b in piece.encode("utf-8"))
            ids.extend(self._bpe(encoded))
        if len(ids) > self.sequence_length - 2:
            raise ValueError("词汇过长：每个名称最多 18 个 CLIP token，请缩短描述")
        # Kit sends float32 IDs to RKNNLite. The host ONNX encoder takes int64.
        return np.array([[self.start_id, *ids, self.end_id]
                         + [self.end_id] * (self.sequence_length - len(ids) - 2)],
                        dtype=np.float32)
