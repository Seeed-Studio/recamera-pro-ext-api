"""Prompt parsing and transactional text-feature updates."""
from dataclasses import dataclass
import re

import numpy as np

from object_color import split_color_query

MAX_PROMPTS = 80
EMBEDDING_SIZE = 512
BACKGROUND_PROMPT = " "


@dataclass(frozen=True)
class Term:
    label: str
    prompt: str


def parse_vocabulary(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("检测词汇必须是最多 4096 字符的文本")
    terms = []
    seen = set()
    for part in re.split(r"[,，;；\n]", value):
        part = part.strip()
        if not part:
            continue
        label, separator, prompt = part.partition("=")
        label = label.strip()
        prompt = (prompt if separator else label).strip().lower()
        if (not label or not prompt or len(label.encode("utf-8")) > 64
                or len(prompt.encode("utf-8")) > 128
                or "=" in prompt or any(ord(c) < 32 for c in label + prompt)):
            raise ValueError("词汇格式：物体名称 或 显示名=英文名称；显示名最多 64 UTF-8 字节")
        if prompt in seen:
            continue
        seen.add(prompt)
        terms.append(Term(label, prompt))
    if len(terms) > MAX_PROMPTS:
        raise ValueError("一次最多检测 80 个不同词汇")
    return tuple(terms)


class Vocabulary:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.terms = ()
        self.model_terms = ()
        self.requests_by_slot = ()
        self.has_color_queries = False
        self.features = None
        self.revision = 0
        self._cache = {}

    def begin(self, text):
        return VocabularyUpdate(self, parse_vocabulary(text))

    def update(self, text, encoder):
        """Synchronous update for setup, before any camera frame is borrowed."""
        pending = self.begin(text)
        while not pending.step(encoder):
            pass
        return pending.changed

    def _commit(self, terms, model_terms, requests_by_slot, cache):
        features = None
        if model_terms:
            active = np.concatenate([cache[t.prompt] for t in model_terms], axis=0)
            # V2.1 was trained with blank background prompts in the unused slots.
            padded = active
            if len(model_terms) < MAX_PROMPTS:
                background = np.repeat(cache[BACKGROUND_PROMPT], MAX_PROMPTS - len(model_terms), axis=0)
                padded = np.concatenate((active, background), axis=0)
            features = np.ascontiguousarray(padded[None], dtype=np.float32)
        self.terms, self.features, self._cache = terms, features, cache
        self.model_terms, self.requests_by_slot = model_terms, requests_by_slot
        self.has_color_queries = any(color is not None for group in requests_by_slot for _, color in group)
        self.revision += 1


class VocabularyUpdate:
    """At most one new text inference per step; active features stay untouched."""

    def __init__(self, owner, terms):
        self.owner, self.terms = owner, terms
        self.changed = terms != owner.terms
        self.done = not self.changed
        # Validate all prompts before doing any NPU work. Commit only after all
        # embeddings are ready, so failed changes keep names and features paired.
        grouped = {}
        for index, term in enumerate(terms):
            prompt, color = split_color_query(term.prompt)
            grouped.setdefault(prompt, []).append((index, color))
        self.model_terms = tuple(Term(prompt, prompt) for prompt in grouped)
        self.requests_by_slot = tuple(tuple(group) for group in grouped.values())
        self.tokens = {prompt: owner.tokenizer.encode(prompt) for prompt in grouped}
        if 0 < len(grouped) < MAX_PROMPTS:
            self.tokens[BACKGROUND_PROMPT] = owner.tokenizer.encode(BACKGROUND_PROMPT)
        self.cache = {prompt: owner._cache[prompt] for prompt in self.tokens
                      if prompt in owner._cache}
        self.missing = [prompt for prompt in self.tokens if prompt not in self.cache]

    def step(self, encoder):
        if self.done:
            return True
        if self.missing:
            prompt = self.missing[0]
            outputs = encoder.infer(self.tokens[prompt])
            if len(outputs) != 1:
                raise RuntimeError("CLIP encoder must return one embedding")
            feature = np.asarray(outputs[0], dtype=np.float32)
            if (feature.shape != (1, EMBEDDING_SIZE)
                    or not np.isfinite(feature).all()
                    or float(np.linalg.norm(feature)) < 1e-8):
                raise RuntimeError("CLIP encoder returned an invalid [1,512] embedding")
            self.cache[prompt] = feature.copy()
            self.missing.pop(0)
        if self.missing:
            return False
        self.owner._commit(self.terms, self.model_terms, self.requests_by_slot, self.cache)
        self.done = True
        return True
