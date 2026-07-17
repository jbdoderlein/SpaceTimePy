from __future__ import annotations

import importlib
import random
import sys
import tempfile
from pathlib import Path

from spacetimepy.interface.standard_external_interactions import (
    STANDARD_EXTERNAL_MODULES,
    StandardExternalInteraction,
    StandardExternalInteractionRegistry,
    StandardExternalModule,
)
from tests.support import SpaceTimeTestCase


class TestStandardExternalInteractions(SpaceTimeTestCase):
    def test_random_randint_is_recorded_without_parent_configuration(self) -> None:
        def unmonitored_helper() -> int:
            return random.randint(10, 20)

        @self.space.capture.function
        def roll() -> int:
            return unmonitored_helper()

        random_state = random.getstate()
        random.seed(42)
        try:
            with self.space.capture.recording() as recording:
                result = roll()
        finally:
            random.setstate(random_state)

        step = self.space.data.get_branch(recording.branch_id).steps[0]
        self.assertGreaterEqual(result, 10)
        self.assertLessEqual(result, 20)
        self.assertEqual(len(step.external_interactions), 1)
        interaction = step.external_interactions[0]
        self.assertEqual(interaction.call.module_name, "random")
        self.assertEqual(interaction.call.qualified_name, "Random.randint")
        self.assertIsNotNone(interaction.call.return_reference)
        self.assertEqual(interaction.call.caller_call_id, step.function_call.id)
        self.assertEqual(
            self.space.data.load_references(
                interaction.call.entry_local_references
            ),
            {"a": 10, "b": 20},
        )

    def test_standard_interaction_outside_a_captured_call_is_ignored(self) -> None:
        with self.space.capture.recording() as recording:
            random.randint(1, 2)

        branch = self.space.data.get_branch(recording.branch_id)
        self.assertEqual(branch.steps, ())

    def test_standard_catalogue_exposes_the_maintainer_definitions(self) -> None:
        random_definition = next(
            definition
            for definition in STANDARD_EXTERNAL_MODULES
            if definition.module_name == "random"
        )
        self.assertIn(
            "randint",
            {
                interaction.attribute_path
                for interaction in random_definition.interactions
            },
        )

    def test_module_loaded_after_runtime_is_registered_dynamically(self) -> None:
        module_name = "spacetimepy_test_external_module"
        definition = StandardExternalModule(
            module_name=module_name,
            interactions=(StandardExternalInteraction("read_value"),),
        )
        registry = StandardExternalInteractionRegistry(
            self.space._monitor,
            (definition,),
        )

        with tempfile.TemporaryDirectory() as directory:
            module_path = Path(directory) / f"{module_name}.py"
            module_path.write_text(
                "def read_value(value):\n    return value * 2\n",
                encoding="utf-8",
            )
            sys.path.insert(0, directory)
            registry.start()
            try:
                @self.space.capture.function
                def calculate(value: int) -> int:
                    module = importlib.import_module(module_name)
                    return module.read_value(value) + 1

                with self.space.capture.recording() as recording:
                    self.assertEqual(calculate(3), 7)
            finally:
                registry.stop()
                sys.path.remove(directory)
                sys.modules.pop(module_name, None)

        step = self.space.data.get_branch(recording.branch_id).steps[0]
        self.assertEqual(len(step.external_interactions), 1)
        self.assertEqual(
            step.external_interactions[0].call.function_name,
            "read_value",
        )
