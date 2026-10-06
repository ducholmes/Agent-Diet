"""Pure Agent Diet candidate selection and acceptance rules."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING
from .trajectory import LogicalStep
from ..events import emit

if TYPE_CHECKING:
    import tiktoken


@lru_cache(maxsize=1)
def _token_encoding() -> tiktoken.Encoding:
    import tiktoken

    return tiktoken.encoding_for_model("gpt-4o")


def count_tokens(text: str) -> int:
    """Count with Trae's gpt-4o encoding, regardless of the agent model.

    Special-token spellings in tool output are counted as ordinary text.
    """
    return len(_token_encoding().encode(text, disallowed_special=()))

@dataclass(slots=True)
class DietMetrics:
    seen_tokens: int = 0; analysis_count: int = 0; erase_count: int = 0
    compression_total_tokens: int = 0
    compression_llm_calls: int = 0
    analysis_prompt_tokens: int = 0
    analysis_completion_tokens: int = 0
    erase_in_tokens: int = 0; erase_out_tokens: int = 0; rejected: dict[str, int] = field(default_factory=dict)
    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        emit("diet_rejection", reason=reason)
    def record_analysis_usage(self, usage: dict[str, int]) -> None:
        self.compression_total_tokens += usage["total_tokens"]
        self.analysis_prompt_tokens += usage["prompt_tokens"]
        self.analysis_completion_tokens += usage["completion_tokens"]

@dataclass(frozen=True, slots=True)
class ReductionCandidate:
    step: LogicalStep; context: str; input_tokens: int

class AgentDiet:
    def __init__(self, *, threshold_tokens: int = 500, ctx_before: int = 1, ctx_after: int = 2, use_lz4: bool = False, minimum_reduction_tokens: int = 400, minimum_reduction_ratio: float = .20):
        self.threshold_tokens, self.ctx_before, self.ctx_after, self.use_lz4 = threshold_tokens, ctx_before, ctx_after, use_lz4
        self.minimum_reduction_tokens, self.minimum_reduction_ratio = minimum_reduction_tokens, minimum_reduction_ratio
        self.metrics = DietMetrics()

    def candidate(self, steps: tuple[LogicalStep, ...]) -> ReductionCandidate | None:
        index = len(steps) - 1 - self.ctx_after
        if index < self.ctx_before or index < 0: return None
        step = steps[index]
        if step.index < 0 or not all(item.complete for item in steps[index - self.ctx_before:]):
            return None
        serialized = step.serialize()
        tokens = count_tokens(serialized); self.metrics.seen_tokens += tokens
        if tokens < self.threshold_tokens: self.metrics.reject("below_threshold"); return None
        if self.use_lz4:
            following_context = "".join(item.serialize() for item in steps[index + 1:])
            if self._compressible(serialized, following_context, tokens) < self.threshold_tokens:
                self.metrics.reject("lz4_not_compressible"); return None
        start, end = index - self.ctx_before, index + self.ctx_after + 1
        return ReductionCandidate(step, "\n".join(item.serialize() for item in steps[start:end]), tokens)
    
    @staticmethod
    def _compressible(text: str, following_context: str, input_tokens: int) -> float:
        """Estimate redundant tokens from LZ4's marginal size, as in Trae.

        Context is concatenated without separators. With ctx_after=0 the
        following context is empty, rather than Python's misleading [-0:] slice.
        """
        import lz4.frame

        raw = text.encode("utf-8")
        following = following_context.encode("utf-8")
        without_step = len(lz4.frame.compress(following))
        with_step = len(lz4.frame.compress(raw + following))
        added_bytes = max(0, with_step - without_step)
        return input_tokens * (1 - added_bytes / max(1, len(raw)))
    
    def accept(self, candidate: ReductionCandidate, replacement: str | None, *, require_reduction: bool = True) -> bool:
        if replacement is None: replacement = ""
        # Match Trae: serialized original step versus bare compressed content.
        output = count_tokens(replacement); saved = candidate.input_tokens - output
        if require_reduction and saved < self.minimum_reduction_tokens and output >= candidate.input_tokens * (1 - self.minimum_reduction_ratio):
            self.metrics.reject("insufficient_reduction"); return False
        self.metrics.erase_count += 1; self.metrics.erase_in_tokens += candidate.input_tokens; self.metrics.erase_out_tokens += output
        return True
