"""Content generation: the style space, deterministic identity, and the grounded drafter."""

from .generator import (
    FactBank,
    GenerationError,
    GenerationRequest,
    Generator,
    TemplateGenerator,
    build_prompt,
    mask_terms,
    normalized_equal,
)
from .identity import assert_unique_slots, slot_id, variant_id
from .style import Axis, Style, StyleSpace

__all__ = [
    "Axis",
    "Style",
    "StyleSpace",
    "FactBank",
    "GenerationError",
    "GenerationRequest",
    "Generator",
    "TemplateGenerator",
    "build_prompt",
    "mask_terms",
    "normalized_equal",
    "slot_id",
    "variant_id",
    "assert_unique_slots",
]
