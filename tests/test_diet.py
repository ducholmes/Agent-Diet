from __future__ import annotations
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openhands_adapter.config import AgentDietConfig
from openhands_adapter.diet.condenser import AgentDietCondenser
from openhands_adapter.diet.trajectory import logical_steps
class DietTests(unittest.TestCase):
    def test_reduces_an_old_atomic_tool_step_and_preserves_event_mapping(self):
        condenser = AgentDietCondenser(AgentDietConfig(mode="ours", threshold_tokens=3, ctx_before=0, ctx_after=0, minimum_reduction_tokens=1, minimum_reduction_ratio=.1), compressor=lambda text, context: "short")
        result = condenser.condense([{"id": "event-1", "type": "message", "content": "one two three four five"}])
        self.assertEqual(result.forget_event_ids, ("event-1",)); self.assertEqual(result.summary, "short")
    def test_skip_never_forgets_events(self):
        result = AgentDietCondenser(AgentDietConfig(mode="skip")).condense([{"id": "1", "content": "long " * 300}])
        self.assertEqual(result.forget_event_ids, ())

    def test_openhands_event_names_pair_action_and_observation(self):
        events = [
            type("ActionEvent", (), {"id": "action-1", "action": "run"})(),
            type("ObservationEvent", (), {"id": "observation-1", "observation": "ok"})(),
        ]
        steps = logical_steps(events)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].event_ids, ("action-1", "observation-1"))

    def test_parallel_tool_events_pair_by_tool_call_id(self):
        events = [
            {"id": "action-a", "type": "ActionEvent", "tool_call_id": "call-a", "action": "a"},
            {"id": "action-b", "type": "ActionEvent", "tool_call_id": "call-b", "action": "b"},
            {"id": "action-c", "type": "ActionEvent", "tool_call_id": "call-c", "action": "c"},
            {"id": "observation-a", "type": "ObservationEvent", "tool_call_id": "call-a", "observation": "result-a"},
            {"id": "observation-b", "type": "ObservationEvent", "tool_call_id": "call-b", "observation": "result-b"},
            {"id": "observation-c", "type": "ObservationEvent", "tool_call_id": "call-c", "observation": "result-c"},
        ]
        steps = logical_steps(events)
        self.assertEqual(
            [step.event_ids for step in steps],
            [
                ("action-a", "observation-a"),
                ("action-b", "observation-b"),
                ("action-c", "observation-c"),
            ],
        )

    def test_delete_forgets_a_complete_matching_tool_step(self):
        events = [
            {"id": "action-a", "type": "ActionEvent", "tool_call_id": "call-a", "action": "a"},
            {"id": "action-b", "type": "ActionEvent", "tool_call_id": "call-b", "action": "b"},
            {"id": "observation-a", "type": "ObservationEvent", "tool_call_id": "call-a", "observation": "result-a"},
            {"id": "observation-b", "type": "ObservationEvent", "tool_call_id": "call-b", "observation": "result-b"},
        ]
        condenser = AgentDietCondenser(
            AgentDietConfig(
                mode="delete",
                threshold_tokens=1,
                ctx_before=0,
                ctx_after=0,
                minimum_reduction_tokens=1,
                minimum_reduction_ratio=.1,
            )
        )
        result = condenser.condense(events)
        self.assertEqual(result.forget_event_ids, ("action-b", "observation-b"))
if __name__ == "__main__": unittest.main()
