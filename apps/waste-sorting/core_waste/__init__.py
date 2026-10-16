"""与硬件无关的垃圾分类核心层。"""

from .classifier import Classifier, ClassifierError, OpenVocabularyClassifier
from .event import EVENT_TYPE, EVENT_VERSION, TAXONOMY_VERSION, build_event
from .gpio import ActuatorSink, CallbackActuator, NullActuator
from .taxonomy import (
    CHINA_CATEGORIES,
    CHINA_CATEGORY_ZH,
    DEFAULT_CLASSES,
    MATERIAL_CLASSES,
    MATERIAL_TO_CHINA,
    NUM_CLASSES,
    SOURCE_LABEL_ALIASES,
    TaxonomyError,
    china_category,
    china_category_zh,
    class_name,
    counts_by_china_category,
    normalize_label,
    softmax,
    top_k,
)
from .types import TRIGGERS, FrameMeta, ImageRef, Prediction, RuntimeCounters
from .vlm_client import (FALLBACK_EVENT_TYPE, FALLBACK_EVENT_VERSION,
                         TRIGGER_AMBIGUOUS, TRIGGER_LOW_CONFIDENCE, FallbackJob,
                         OffTaxonomyCategory, VlmConfig, VlmFallbackClient,
                         VlmTrigger, build_fallback_event, fallback_trigger,
                         maybe_fallback, should_fallback, taxonomy_payload)

__all__ = [
    "MATERIAL_CLASSES", "DEFAULT_CLASSES", "NUM_CLASSES", "SOURCE_LABEL_ALIASES",
    "CHINA_CATEGORIES", "CHINA_CATEGORY_ZH", "MATERIAL_TO_CHINA",
    "TaxonomyError", "normalize_label", "class_name", "china_category",
    "china_category_zh", "softmax", "top_k", "counts_by_china_category",
    "Classifier", "OpenVocabularyClassifier", "ClassifierError",
    "FrameMeta", "Prediction", "ImageRef", "RuntimeCounters", "TRIGGERS",
    "build_event", "EVENT_TYPE", "EVENT_VERSION", "TAXONOMY_VERSION",
    "ActuatorSink", "CallbackActuator", "NullActuator",
    "VlmConfig", "VlmTrigger", "VlmFallbackClient", "FallbackJob",
    "OffTaxonomyCategory", "fallback_trigger", "should_fallback",
    "build_fallback_event", "taxonomy_payload", "maybe_fallback",
    "FALLBACK_EVENT_TYPE", "FALLBACK_EVENT_VERSION",
    "TRIGGER_LOW_CONFIDENCE", "TRIGGER_AMBIGUOUS",
]
