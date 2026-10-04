"""OpenHands assembly point, deliberately small to absorb SDK API changes."""
from __future__ import annotations
from typing import Any
from ..config import AgentDietConfig, OpenHandsConfig, WorkflowConfig
from ..diet.condenser import AgentDietCondenser
from ..diet.strategies import Compressor
from .compressor import LLMCompressor, build_compressor
from .llm import build_llm
from ..token_tracking import install_token_tracking
from .container import RepairContainer
from .runtime import require_pinned_sdk
from .condenser import build_sdk_condenser
from .workspace_tool import WORKSPACE_TOOL_REGEX, WorkspaceTools, workspace_tool_specs
from .prompts import REPAIR_SYSTEM_PROMPT


def build_agent(container: RepairContainer, openhands: OpenHandsConfig, diet: AgentDietConfig, workflow: WorkflowConfig, *, execution_plan: dict, compressor: Compressor | None = None) -> tuple[Any, AgentDietCondenser]:
    require_pinned_sdk()
    diet.validate()
    llm = build_llm(openhands)
    if compressor is None and diet.enabled and diet.mode == "ours":
        compressor_llm = (
            llm if diet.compressor_model == "inherit"
            else build_llm(openhands, model=diet.compressor_model)
        )
        compressor = build_compressor(
            compressor_llm, on_usage=lambda usage: condenser.diet.metrics.record_analysis_usage(usage),
        )
    condenser = AgentDietCondenser(diet, compressor=compressor, agent_name="OpenHands", model=openhands.model)
    install_token_tracking(llm, condenser.diet.metrics)
    if isinstance(compressor, LLMCompressor):
        install_token_tracking(compressor.llm, condenser.diet.metrics)
    sdk_condenser = build_sdk_condenser(condenser)
    tool = WorkspaceTools(container, execution_plan=execution_plan, timeout_seconds=workflow.command_timeout_seconds, output_limit_bytes=workflow.output_limit_bytes)
    try:
        from openhands.sdk import Agent, AgentContext
        return Agent(
            llm=llm,
            condenser=sdk_condenser,
            tools=workspace_tool_specs(tool),
            filter_tools_regex=WORKSPACE_TOOL_REGEX,
            include_default_tools=["FinishTool", "ThinkTool"],
            agent_context=AgentContext(system_message_suffix=REPAIR_SYSTEM_PROMPT),
        ), condenser
    except ImportError as exc: raise RuntimeError("OpenHands SDK is required to run an agent") from exc
