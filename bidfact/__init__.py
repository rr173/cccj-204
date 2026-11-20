"""Offline tender announcement fact processor (bidfact)."""
from .pipeline import RULE_VERSION, run_pipeline

__all__ = ["run_pipeline", "RULE_VERSION"]
