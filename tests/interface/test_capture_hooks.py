from __future__ import annotations

from typing import Any

import spacetimepy
from spacetimepy import CaptureReturnContext, CaptureStartContext, SpaceTime
from tests.support import SpaceTimeTestCase


class TestCaptureHooks(SpaceTimeTestCase):
    def test_declarative_start_and_return_hooks_persist_trace_metadata(self) -> None:
        self.space.close()
        invocations: list[str] = []

        def first_start(context: CaptureStartContext) -> dict[str, Any]:
            invocations.append("first_start")
            self.assertEqual(context.function_name, "calculate")
            self.assertEqual(context.qualified_name.split(".")[-1], "calculate")
            self.assertEqual(context.locals["value"], 4)
            self.assertIn("__name__", context.globals)
            return {"phase": "start", "input": context.locals["value"]}

        def second_start(_context: CaptureStartContext) -> dict[str, Any]:
            invocations.append("second_start")
            return {"phase": "second_start", "started": True}

        def at_return(context: CaptureReturnContext) -> dict[str, Any]:
            invocations.append("return")
            self.assertEqual(context.locals["value"], 4)
            return {"phase": "return", "result": context.return_value}

        @spacetimepy.function(
            start_hooks=[first_start, second_start],
            return_hooks=[at_return],
        )
        def calculate(value: int) -> int:
            return value + 3

        self.space = SpaceTime.open()
        with self.space.capture.recording() as recording:
            self.assertEqual(calculate(4), 7)

        call = self.space.data.get_branch(recording.branch_id).steps[0].function_call
        self.assertEqual(
            invocations,
            ["first_start", "second_start", "return"],
        )
        self.assertEqual(call.attributes["input"], 4)
        self.assertTrue(call.attributes["started"])
        self.assertEqual(call.attributes["result"], 7)
        # Later hooks and the return phase intentionally win on key conflicts.
        self.assertEqual(call.attributes["phase"], "return")

    def test_hook_failures_are_stored_and_do_not_interrupt_capture(self) -> None:
        executed: list[str] = []

        def broken(_context: CaptureStartContext) -> None:
            executed.append("broken")
            raise RuntimeError("cannot inspect state")

        def unsupported(_context: CaptureStartContext) -> dict[str, object]:
            executed.append("unsupported")
            return {"not_json": object()}

        def healthy(_context: CaptureStartContext) -> dict[str, bool]:
            executed.append("healthy")
            return {"healthy_hook_ran": True}

        def broken_return(_context: CaptureReturnContext) -> None:
            executed.append("broken_return")
            raise LookupError("cannot inspect result")

        @self.space.capture.function(
            start_hooks=[broken, unsupported, healthy],
            return_hooks=[broken_return],
        )
        def calculate() -> int:
            return 5

        with self.space.capture.recording() as recording:
            self.assertEqual(calculate(), 5)

        call = self.space.data.get_branch(recording.branch_id).steps[0].function_call
        self.assertEqual(
            executed,
            ["broken", "unsupported", "healthy", "broken_return"],
        )
        self.assertTrue(call.attributes["healthy_hook_ran"])
        start_errors = call.attributes["start_hook_errors"]
        return_errors = call.attributes["return_hook_errors"]
        self.assertEqual(len(start_errors), 2)
        self.assertEqual(len(return_errors), 1)
        self.assertTrue(
            any("RuntimeError" in error for error in start_errors.values())
        )
        self.assertTrue(
            any("JSON serializable" in error for error in start_errors.values())
        )
        self.assertTrue(
            any("LookupError" in error for error in return_errors.values())
        )
        self.assertNotIn("not_json", call.attributes)

    def test_hooks_can_be_combined_with_low_level_attribute_providers(self) -> None:
        def start_attributes(*_args: object) -> dict[str, str]:
            return {"provider": "start", "winner": "provider"}

        def start_hook(_context: CaptureStartContext) -> dict[str, str]:
            return {"hook": "start", "winner": "hook"}

        @self.space.capture.function(
            start_attributes=start_attributes,
            start_hooks=[start_hook],
        )
        def calculate() -> int:
            return 1

        with self.space.capture.recording() as recording:
            calculate()

        call = self.space.data.get_branch(recording.branch_id).steps[0].function_call
        self.assertEqual(call.attributes["provider"], "start")
        self.assertEqual(call.attributes["hook"], "start")
        self.assertEqual(call.attributes["winner"], "hook")
