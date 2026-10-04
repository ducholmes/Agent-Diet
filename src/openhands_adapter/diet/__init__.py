"""SDK-independent Agent Diet trajectory reduction."""
from .core import AgentDiet, DietMetrics, ReductionCandidate, count_tokens
from .trajectory import LogicalStep, logical_steps
__all__ = ["AgentDiet", "DietMetrics", "ReductionCandidate", "count_tokens", "LogicalStep", "logical_steps"]
