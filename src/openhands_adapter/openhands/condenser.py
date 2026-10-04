"""OpenHands SDK adapter for the SDK-neutral Agent Diet condenser."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from ..diet.core import count_tokens
from ..diet.condenser import AgentDietCondenser
from ..diet.trajectory import event_text
from ..events import emit


def build_sdk_condenser(delegate: AgentDietCondenser) -> Any:
    """Wrap Agent Diet in the pinned OpenHands SDK condenser contract.

    OpenHands imports are intentionally lazy so configuration and unit-test modules
    remain usable without installing the OpenHands SDK.
    """
    try:
        from openhands.sdk.context.condenser import CondenserBase
        from openhands.sdk.context.view import View
        from openhands.sdk.event.condenser import Condensation as SDKCondensation
        from openhands.sdk.event.condenser import CondensationSummaryEvent
        from openhands.sdk.event import MessageEvent
        from openhands.sdk.llm import LLM, Message, TextContent
    except ImportError as exc:
        raise RuntimeError(
            "The pinned OpenHands SDK is required to build the condenser adapter"
        ) from exc

    class OpenHandsDietSDKCondenser(CondenserBase):
        """Translate Agent Diet decisions into OpenHands condensation events."""

        def __init__(self) -> None:
            super().__init__()
            self._pending_expected_view_ids: tuple[str, ...] | None = None
            self._pending_forgotten_ids: tuple[str, ...] = ()
            self._pending_expected_tokens = 0

        @staticmethod
        def _view_ids(events: list[Any]) -> tuple[str, ...]:
            return tuple(str(event.id) for event in events)

        @staticmethod
        def _view_tokens(events: list[Any]) -> int:
            return sum(count_tokens(event_text(event)) for event in events)

        def _verify_previous_condensation(self, view: View) -> None:
            expected = self._pending_expected_view_ids
            if expected is None:
                return
            observed = self._view_ids(list(view.events))
            expected_set, observed_set = set(expected), set(observed)
            emit(
                "diet_application_check",
                status="verified" if observed == expected else "view_mismatch",
                forgotten_event_ids=list(self._pending_forgotten_ids),
                forgotten_ids_still_present=sorted(
                    set(self._pending_forgotten_ids) & observed_set
                ),
                expected_event_count=len(expected),
                observed_event_count=len(observed),
                expected_token_estimate=self._pending_expected_tokens,
                observed_token_estimate=self._view_tokens(list(view.events)),
                missing_event_ids=sorted(expected_set - observed_set),
                unexpected_event_ids=sorted(observed_set - expected_set),
            )
            self._pending_expected_view_ids = None
            self._pending_forgotten_ids = ()
            self._pending_expected_tokens = 0

        def finish_audit(self) -> None:
            """Mark a final condensation that had no later view to inspect."""
            if self._pending_expected_view_ids is None:
                return
            emit(
                "diet_application_check",
                status="not_observed_before_run_end",
                forgotten_event_ids=list(self._pending_forgotten_ids),
                expected_event_count=len(self._pending_expected_view_ids),
                expected_token_estimate=self._pending_expected_tokens,
            )
            self._pending_expected_view_ids = None
            self._pending_forgotten_ids = ()
            self._pending_expected_tokens = 0

        def handles_condensation_requests(self) -> bool:
            # Let OpenHands retry after a context-window or malformed-history
            # signal. ``condense`` returns the unchanged view when Agent Diet has
            # no safe candidate yet.
            return True

        @staticmethod
        def _agent_view(view: View) -> View:
            # Keep SDK-native condensation events in the archive. Only project
            # this adapter's summaries to assistant messages for the LLM.
            events = [
                MessageEvent(
                    id=event.id, timestamp=event.timestamp, parent_id=event.parent_id,
                    source="agent",
                    llm_message=Message(role="assistant", content=[TextContent(text=event.summary)]),
                ) if isinstance(event, CondensationSummaryEvent) and event.id in delegate.summary_to_step else event
                for event in view.events
            ]
            return view.model_copy(update={"events": events})

        def condense(
            self,
            view: View,
            agent_llm: LLM | None = None,  # noqa: ARG002
        ) -> View | SDKCondensation:
            self._verify_previous_condensation(view)
            result = delegate.condense(view.events)

            if result.reason != "reduced" or not result.forget_event_ids:
                return self._agent_view(view)

            forgotten_ids = {
                event.id
                for event in view.events
                if event.id in result.forget_event_ids
            }
            if not forgotten_ids:
                return self._agent_view(view)

            first_forgotten_index = min(
                index
                for index, event in enumerate(view.events)
                if event.id in forgotten_ids
            )
            summary = (
                f"(System reminder: compressed for better efficiency) {result.summary.strip()}"
                if result.summary else
                "(System reminder: long content deleted for better efficiency)"
            )
            summary_offset = (
                sum(
                    event.id not in forgotten_ids
                    for event in view.events[:first_forgotten_index]
                )
                if summary is not None
                else None
            )

            sdk_result = SDKCondensation(
                forgotten_event_ids=forgotten_ids,
                summary=summary,
                summary_offset=summary_offset,
                # Agent Diet may use a local compressor instead of an LLM. The SDK
                # still requires this event metadata, so use a unique local ID.
                llm_response_id=f"agent-diet-{uuid4()}",
            )
            projected_view = sdk_result.apply(list(view.events))
            delegate.bind_summary(sdk_result.summary_event.id, result.forget_event_ids)
            self._pending_expected_view_ids = self._view_ids(projected_view)
            self._pending_forgotten_ids = tuple(
                sorted(str(event_id) for event_id in forgotten_ids)
            )
            self._pending_expected_tokens = self._view_tokens(projected_view)
            emit(
                "diet_sdk_condensation",
                status="returned_to_openhands",
                forgotten_event_ids=list(self._pending_forgotten_ids),
                before_event_count=len(view.events),
                expected_after_event_count=len(projected_view),
                before_token_estimate=self._view_tokens(list(view.events)),
                expected_after_token_estimate=self._pending_expected_tokens,
                reminder_token_estimate=count_tokens(summary.partition(")")[0] + ")"),
                replacement_message_token_estimate=count_tokens(summary),
                token_estimate_encoding="gpt-4o",
            )
            return sdk_result

    return OpenHandsDietSDKCondenser()
