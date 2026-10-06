"""One raw repair response per SDK step; Trae owns repair transitions."""
from __future__ import annotations
from copy import deepcopy
from typing import Literal
from pydantic import PrivateAttr
from openhands.sdk import Message, TextContent
from openhands.sdk.agent.base import AgentBase
from openhands.sdk.conversation import LocalConversation
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.event import MessageEvent
from ..compat.trae_contract import MessageManager, PROFILES, SYS_PROMPT, TOOLS
from ..events import emit
from ..compat.audit import request_context, byte_hash
from ..workflow.trae_patch import TIME_OUT_LABEL, remove_patches_to_tests
from .trae_tools import parse_tool_response


class TraeLocalConversation(LocalConversation):
    """Keep SDK scheduling/events, disable ambient hooks/agents per instance."""
    def _ensure_plugins_loaded(self):
        self._plugins_loaded = True

    def _register_file_based_agents(self):
        pass


class TraeContractAgent(AgentBase):
    reference_profile: Literal["trae_verified", "trae_multiswe"] = "trae_verified"
    system_prompt: str | None = SYS_PROMPT
    include_default_tools: list[str] = []
    _transport: object = PrivateAttr(default=None)
    _sandbox: object = PrivateAttr(default=None)
    _session: object = PrivateAttr(default=None)
    _hook: object = PrivateAttr(default=None)

    def bind_runtime(self, *, transport, sandbox, after_normal_turn=None):
        self._transport = transport
        self._sandbox = sandbox
        self._session = sandbox.get_session()
        self._hook = after_normal_turn
        return self

    @property
    def dynamic_context(self):
        return None

    @property
    def supports_openhands_tools(self):
        return False

    @property
    def supports_openhands_mcp(self):
        return False

    @property
    def supports_condenser(self):
        return False

    def _initialize(self, state):
        self._tools = {}
        self._initialized = True

    def init_state(self, state, on_event):
        self._initialize(state)

    def _manager(self, state):
        cap, reminder = PROFILES[self.reference_profile]
        mgr = MessageManager("", "", self._sandbox, {}, reminder, cap, deepcopy(TOOLS))
        saved = state.agent_state.get("trae_contract")
        if saved:
            if saved["profile"] != self.reference_profile:
                raise ValueError("Cannot resume with a different Trae reference profile")
            mgr.user_message = deepcopy(saved["user_message"])
            mgr.steps = deepcopy(saved["steps"])
            mgr.last_analyzed_turn = saved.get("last_analyzed_turn")
            mgr.diet_metrics = deepcopy(saved.get("diet_metrics"))
        else:
            initial = next((event.llm_message for event in state.events
                            if isinstance(event, MessageEvent) and event.llm_message.role == "user"), None)
            if initial is None:
                raise ValueError("Trae agent requires an initial user prompt")
            mgr.user_message = {"role": "user", "content": "".join(c.text for c in initial.content)}
        return mgr, saved or {}

    def _persist(self, state, mgr, *, gen=None, original_patch="", patch=""):
        snapshot = {"profile": self.reference_profile, "user_message": deepcopy(mgr.user_message),
                    "steps": deepcopy(mgr.steps), "turns": mgr.count_turn(),
                    "last_analyzed_turn": getattr(mgr, "last_analyzed_turn", None),
                    "diet_metrics": deepcopy(getattr(mgr, "diet_metrics", None)),
                    "gen": gen, "original_patch": original_patch,
                    "filtered_patch": remove_patches_to_tests(original_patch), "patch": patch,
                    "patch_origin": "error_wip" if gen == "generation_error" else "terminal_snapshot" if gen else "in_progress"}
        state.agent_state = {**state.agent_state, "trae_contract": snapshot}
        emit("trae_contract_state", **snapshot)
        if gen is not None:
            state.execution_status = ConversationExecutionStatus.FINISHED

    def _capture(self, trigger):
        original = self._sandbox.get_diff()
        patch = remove_patches_to_tests(original)
        emit("trae_patch_capture", trigger=trigger, origin="container",
             command=["git", "--no-pager", "diff", "--ignore-submodules=all"],
             raw_bytes_sha256=byte_hash(original.encode('utf-8')),
             filtered_bytes_sha256=byte_hash(patch.encode('utf-8')),
             original_patch=original, filtered_patch=patch)
        return original, patch

    def step(self, conversation, on_event, on_token=None):
        saved = conversation.state.agent_state.get("trae_contract", {})
        turn = min(saved.get("turns", 0) + 1, PROFILES[self.reference_profile][0])
        with request_context("repair", force_new=True, logical_turn=turn, step_index=turn-1):
            try:
                return self._step(conversation, on_event, on_token)
            except Exception as error:
                mgr, current = self._manager(conversation.state)
                if current.get("gen") != "generation_error":
                    self._persist(conversation.state, mgr, gen="generation_error")
                emit("trae_generation_error", phase="generation", error_type=type(error).__name__,
                     cause_type=type(error.__cause__).__name__ if error.__cause__ else None)
                raise

    def _step(self, conversation, on_event, on_token=None):
        state = conversation.state
        mgr, saved = self._manager(state)
        if saved.get("gen"):
            state.execution_status = ConversationExecutionStatus.FINISHED
            return
        if mgr.count_turn() >= mgr.max_turn:
            original, patch = self._capture("cap")
            self._persist(state, mgr, gen="turn_capped", original_patch=original, patch=patch if patch.strip() else "")
            return
        if self._transport is None or self._sandbox is None:
            raise RuntimeError("Trae agent runtime has not been bound")
        messages = mgr.format_messages()
        emit("trae_request", logical_turn=mgr.count_turn()+1, messages=messages, tools=TOOLS)
        answer, reason, usage = self._transport(deepcopy(messages), deepcopy(TOOLS))
        emit("trae_response", logical_turn=mgr.count_turn()+1, response=answer, finish_reason=reason, usage=usage)
        followups = []
        gen = None
        original = patch = ""
        if usage.get("completion_tokens") is not None:
            if self._session is None:
                self._session = self._sandbox.get_session()
            results = parse_tool_response(answer, reason, self._session)
            emit("trae_tool_results", logical_turn=mgr.count_turn()+1, results=results)
            if len(results) == 1 and results[0]["agent_caller"][0] == "task_done":
                original, patch = self._capture("completion")
                if patch.strip():
                    gen = "task_done"
                else:
                    followups = results + [{"role": "user", "content": "ERROR! Your Patch is empty. Please provide a patch that fixes the problem."}]
            elif len(results) == 1 and results[0]["agent_caller"][0] == "task_failed":
                gen = "task_failed"
            else:
                followups = results
                if TIME_OUT_LABEL in str(followups):
                    self._session = self._sandbox.get_session()
                    emit("trae_session_restarted", logical_turn=mgr.count_turn()+1)
        mgr.push_step(deepcopy(answer), followups)
        # SDK events are an archive only; the persisted contract builds requests.
        sdk_event = MessageEvent(source="agent", llm_message=Message(
            role="assistant", content=[TextContent(text=answer.get("content") or "")],
        ))
        on_event(sdk_event)
        emit("trae_step_pushed", logical_turn=mgr.count_turn(), step_index=mgr.count_turn()-1,
             sdk_event_ids=[str(sdk_event.id)], observation_count=len(followups))
        self._persist(state, mgr, gen=gen, original_patch=original, patch=patch)
        if gen is None:
            emit("trae_after_normal_turn", logical_turn=mgr.count_turn())
            if self._hook is not None:
                try:
                    self._hook(mgr)
                except Exception as exc:
                    self._persist(state, mgr, gen="generation_error")
                    emit("trae_generation_error", phase="compression", error_type=type(exc).__name__, error=str(exc))
                    raise
                self._persist(state, mgr)

    # AgentBase.astep delegates to this same step in a thread; no stock async loop.
    def close(self):
        if self._session is not None:
            self._session.close()
            self._session = None
