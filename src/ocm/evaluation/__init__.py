"""Quality gate: rubric scoring, hard compliance floors, and near-duplicate rejection."""

from .compliance import ComplianceFloor, ComplianceViolation
from .dedup import DedupIndex, similarity
from .gate import QualityGate
from .rubric import Dimension, Rubric

__all__ = [
    "ComplianceFloor",
    "ComplianceViolation",
    "DedupIndex",
    "similarity",
    "QualityGate",
    "Dimension",
    "Rubric",
]
