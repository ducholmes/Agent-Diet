"""Child-process OpenHands conversation; wall-clock control stays in process.py."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from ..config import AgentDietConfig, OpenHandsConfig, WorkflowConfig
from ..events import configure, emit, read_events
from ..token_tracking import aggregate_calls
from ..progress import stage
from .agent import build_agent
from .container import RepairContainer
from .runtime import disable_ambient_discovery, require_pinned_sdk


def _text_content(message: dict[str, Any]) -> str:
    content = message.get("content") or []
    if isinstance(content, str):
        return content
    return "\n".join(
        str(item.get("text", ""))
        for item in content
        if isinstance(item, dict) and item.get("text")
    )


def _preview(value: Any, limit: int = 8_000) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[-limit:] + "\n[truncated]"
    return value


class ConversationEventLogger:
    """Persist useful agent actions without duplicating the SDK event archive."""

    def __init__(self) -> None:
        self.last_response = ""

    def __call__(self, event: Any) -> None:
        data = event.model_dump(mode="json")
        kind = type(event).__name__
        base = {
            "sdk_event_type": kind,
            "event_id": data.get("id"),
            "parent_id": data.get("parent_id"),
            "source": data.get("source"),
        }
        if kind == "ActionEvent":
            action = data.get("action") or data.get("tool_call") or {}
            if data.get("tool_name") == "finish" and isinstance(action, dict):
                self.last_response = str(action.get("message") or action.get("final_answer") or "")
            emit(
                "agent_action",
                **base,
                tool=data.get("tool_name"),
                tool_call_id=data.get("tool_call_id"),
                action=action,
                summary=data.get("summary"),
            )
            return
        if kind in {"ObservationEvent", "AgentErrorEvent", "UserRejectObservation"}:
            observation = data.get("observation") or {}
            output = observation.get("output") if isinstance(observation, dict) else None
            emit(
                "tool_result" if kind == "ObservationEvent" else "agent_error",
                **base,
                tool=data.get("tool_name"),
                tool_call_id=data.get("tool_call_id"),
                exit_code=observation.get("exit_code") if isinstance(observation, dict) else None,
                timed_out=observation.get("timed_out") if isinstance(observation, dict) else None,
                output=_preview(output),
                error=data.get("error") or data.get("rejection_reason"),
            )
            return
        if kind == "MessageEvent":
            message = data.get("llm_message") or {}
            text = _text_content(message)
            if data.get("source") == "agent" and text:
                self.last_response = text
                emit("agent_message", **base, text=_preview(text, 20_000))
            return
        if kind == "Condensation":
            emit(
                "diet_sdk_condensation_event",
                event_id=data.get("id"),
                forgotten_event_ids=sorted(data.get("forgotten_event_ids") or []),
                summary=data.get("summary"),
            )
            return
        if kind in {"ConversationErrorEvent", "ErrorEvent"}:
            emit("agent_error", **base, error=data.get("error") or data.get("detail") or data)


def run_worker(workspace: Path, prompt: str, output_dir: Path, *, execution_plan: dict, container: RepairContainer, openhands: OpenHandsConfig, diet: AgentDietConfig, workflow: WorkflowConfig) -> str:
    """Run one conversation. This code cannot execute host shell commands."""
    require_pinned_sdk()
    disable_ambient_discovery()
    configure(output_dir / "events.jsonl")
    stage("repair", "worker_started", agent="OpenHands", model=openhands.model, max_iterations=openhands.max_iterations)
    agent, condenser = build_agent(container, openhands, diet, workflow, execution_plan=execution_plan)
    event_logger = ConversationEventLogger()
    started = time.monotonic()
    conversation = None
    worker_status = "failed"
    try:
        from openhands.sdk import LocalConversation
        stage("repair", "conversation_started", agent="OpenHands", model=openhands.model)
        conversation = LocalConversation(
            agent=agent,
            workspace=workspace,
            callbacks=[event_logger],
            visualizer=None,
            max_iteration_per_run=openhands.max_iterations,
        )
        conversation.send_message(prompt)
        conversation.run()
        stage("repair", "conversation_finished", agent="OpenHands", model=openhands.model)
        response: Any = event_logger.last_response
        worker_status = "completed"
    except (ImportError, AttributeError) as exc:
        emit("agent_error", phase="worker", error=str(exc), error_type=type(exc).__name__)
        raise RuntimeError("Installed OpenHands SDK does not expose the expected Agent/LocalConversation API") from exc
    except Exception as exc:
        emit("agent_error", phase="worker", error=str(exc), error_type=type(exc).__name__)
        raise
    finally:
        try:
            if conversation is not None and hasattr(conversation, "close"):
                conversation.close()
        finally:
            sdk_condenser = getattr(agent, "condenser", None)
            finish_audit = getattr(sdk_condenser, "finish_audit", None)
            if callable(finish_audit):
                finish_audit()
            metrics = condenser.metrics
            compression = aggregate_calls(read_events(output_dir / "events.jsonl"))['compression']
            if compression['calls']:
                metrics.update(
                    compression_llm_calls=compression['calls'],
                    analysis_prompt_tokens=compression['input_tokens'],
                    analysis_completion_tokens=compression['output_tokens'],
                    compression_total_tokens=compression['total_tokens'],
                )
            emit(
                "diet_metrics",
                metrics=metrics,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
            emit(
                "worker_finished",
                elapsed_seconds=round(time.monotonic() - started, 3),
                status=worker_status,
            )
    output_dir.mkdir(parents=True, exist_ok=True)
    stage("compress", "metrics", agent="OpenHands", model=openhands.model, mode=diet.mode, **metrics)
    return str(response)


def main() -> int:
    """Read a serializable config from process.py and write the final response."""
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    prompt = sys.stdin.read()
    output = Path(config["output_dir"])
    container = RepairContainer(config["container_name"], Path(config["workspace"]), config["image"], config["runtime"])
    response = run_worker(
        Path(config["workspace"]), prompt, output, container=container, execution_plan=config["execution_plan"],
        openhands=OpenHandsConfig.from_mapping(config["openhands"]), diet=AgentDietConfig.from_mapping(config["agentdiet"]), workflow=WorkflowConfig.from_mapping(config["workflow"]),
    )
    Path(config["response_path"]).write_text(response, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
