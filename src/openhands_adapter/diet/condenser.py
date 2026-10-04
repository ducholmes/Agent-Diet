"""Adapter around AgentDiet that returns an SDK-neutral condensation result."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from collections.abc import Callable, Iterable
from typing import Any
from ..config import AgentDietConfig
from ..events import emit
from ..progress import stage
from .core import AgentDiet, count_tokens
from .strategies import Compressor, delete, lingua, ours, random_drop
from .trajectory import LogicalStep, logical_steps
@dataclass(frozen=True, slots=True)
class Condensation:
    forget_event_ids: tuple[str, ...] = (); summary: str | None = None; reason: str = "unchanged"
class AgentDietCondenser:
    def __init__(self, config: AgentDietConfig, *, compressor: Compressor | None = None, agent_name: str = "OpenHands", model: str | None = None):
        config.validate(); self.config, self.compressor = config, compressor
        self.agent_name, self.model = agent_name, model or "unknown"
        self.compressor_model = self.model if config.compressor_model == "inherit" else config.compressor_model
        self.diet = AgentDiet(threshold_tokens=config.threshold_tokens, ctx_before=config.ctx_before, ctx_after=config.ctx_after, use_lz4=config.use_lz4, minimum_reduction_tokens=config.minimum_reduction_tokens, minimum_reduction_ratio=config.minimum_reduction_ratio)
        # Worker-local originals are never part of the agent's LLM view.
        self.original_steps: dict[int, LogicalStep] = {}
        self._event_to_step: dict[str, int] = {}
        self.summary_to_step: dict[str, int] = {}
        stage("compress", "configured", agent=self.agent_name, model=self.model, mode=config.mode, compressor_model=self.compressor_model, compressor_available=compressor is not None)
    @property
    def metrics(self) -> dict[str, Any]:
        metrics = asdict(self.diet.metrics)
        metrics['step_content_reduction_tokens'] = metrics['erase_in_tokens'] - metrics['erase_out_tokens']
        return metrics

    def bind_summary(self, summary_event_id: str, original_event_ids: tuple[str, ...]) -> None:
        """Associate the SDK's replacement event with its original logical step."""
        self.summary_to_step[summary_event_id] = self._event_to_step[original_event_ids[0]]

    def _steps_for_compression(self, events: Iterable[Any]) -> tuple[LogicalStep, ...]:
        steps = []
        for step in logical_steps(events):
            summary_index = self.summary_to_step.get(step.event_ids[0])
            if summary_index is not None:
                original = self.original_steps[summary_index]
                steps.append(LogicalStep(original.index, step.event_ids, original.text, original.complete))
                continue
            index = self._event_to_step.get(step.event_ids[0])
            if index is None:
                index = -1 if step.index == -1 else max(self.original_steps, default=-1) + 1
            original = LogicalStep(index, step.event_ids, step.text, step.complete)
            # Refresh unmatched actions once their observations arrive.
            self.original_steps[index] = original
            for event_id in step.event_ids:
                self._event_to_step[event_id] = index
            steps.append(original)
        return tuple(steps)

    def condense(self, events: Iterable[Any]) -> Condensation:
        stage("compress", "started", agent=self.agent_name, model=self.model, mode=self.config.mode, compressor_model=self.compressor_model)
        if not self.config.enabled or self.config.mode == "skip":
            result = Condensation(reason="disabled_or_skip")
            stage("compress", "finished", agent=self.agent_name, model=self.model, result=result.reason)
            return result
        steps = self._steps_for_compression(events)
        target_position = len(steps) - 1 - self.config.ctx_after
        if target_position >= 0 and steps[target_position].event_ids[0] in self.summary_to_step:
            # The SDK calls again immediately after applying a condensation.
            # Summaries supply original context, but are not new targets.
            stage("compress", "finished", agent=self.agent_name, model=self.model, result="already_compressed")
            return Condensation(reason="already_compressed")
        candidate = self.diet.candidate(steps)
        if candidate is None:
            result = Condensation(reason="no_candidate")
            stage("compress", "finished", agent=self.agent_name, model=self.model, result=result.reason)
            return result
        try:
            self.diet.metrics.analysis_count += 1
            emit("diet_analysis_started", mode=self.config.mode, step_index=candidate.step.index)
            if self.config.mode == "delete": replacement = delete(candidate.step.text)
            elif self.config.mode == "random": replacement = random_drop(candidate.step.serialize(), ratio=self.config.lingua_ratio)
            elif self.config.mode == "lingua": replacement = lingua(candidate.step.serialize(), ratio=self.config.lingua_ratio)
            elif self.config.mode == "ours":
                if self.compressor is None:
                    self.diet.metrics.reject("no_compressor")
                    result = Condensation(reason="no_compressor")
                    stage("compress", "finished", agent=self.agent_name, model=self.model, result=result.reason)
                    return result
                context = candidate.context if self.config.show_ctx else candidate.step.serialize()
                compress_step = getattr(type(self.compressor), "compress_step", None)
                replacement = (
                    compress_step(self.compressor, candidate.step.text, context, step_index=candidate.step.index)
                    if callable(compress_step) else ours(candidate.step.text, context, self.compressor)
                )
            else:
                result = Condensation(reason="unknown_mode")
                stage("compress", "finished", agent=self.agent_name, model=self.model, result=result.reason)
                return result
        except Exception as exc:
            self.diet.metrics.reject("compressor_error")
            emit("diet_compressor_error", mode=self.config.mode, error_type=type(exc).__name__, error=str(exc))
            result = Condensation(reason="compressor_error")
            stage("compress", "finished", agent=self.agent_name, model=self.model, result=result.reason)
            return result
        if not isinstance(replacement, str):
            self.diet.metrics.reject("invalid_compressor_output")
            result = Condensation(reason="invalid_compressor_output")
            stage("compress", "finished", agent=self.agent_name, model=self.model, result=result.reason)
            return result
        rejected_before = dict(self.diet.metrics.rejected)
        if not self.diet.accept(candidate, replacement, require_reduction=self.config.mode == "ours"):
            newly_rejected = {
                reason: count - rejected_before.get(reason, 0)
                for reason, count in self.diet.metrics.rejected.items()
                if count > rejected_before.get(reason, 0)
            }
            emit(
                "diet_step_change",
                status="rejected",
                mode=self.config.mode,
                reason=next(iter(newly_rejected), "rejected"),
                step_index=candidate.step.index,
                event_ids=list(candidate.step.event_ids),
                before_text=candidate.step.text,
                proposed_text=replacement,
                before_token_estimate=candidate.input_tokens,
                proposed_token_estimate=count_tokens(replacement),
            )
            result = Condensation(reason="rejected")
            stage("compress", "finished", agent=self.agent_name, model=self.model, result=result.reason)
            return result
        emit(
            "diet_step_change",
            status="accepted",
            mode=self.config.mode,
            action="deleted" if not replacement else "replaced",
            step_index=candidate.step.index,
            event_ids=list(candidate.step.event_ids),
            before_text=candidate.step.text,
            after_text=replacement,
            before_token_estimate=candidate.input_tokens,
            after_token_estimate=count_tokens(replacement),
        )
        result = Condensation(candidate.step.event_ids, replacement, "reduced")
        stage("compress", "finished", agent=self.agent_name, model=self.model, result=result.reason)
        return result
