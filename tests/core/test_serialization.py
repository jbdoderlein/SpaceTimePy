from __future__ import annotations

import copyreg
import types
import unittest

from spacetimepy import CustomPicklerError, SerializationError
from spacetimepy.core.serialization import PickleSerializer
from tests.serialization_support import (
    CustomValue,
    CustomValuePickler,
    reduce_as_first,
    reduce_as_second,
    reduce_custom_value,
)


class TestPickleSerializer(unittest.TestCase):
    def test_default_pickle_failure_has_capture_specific_error(self) -> None:
        serializer = PickleSerializer()

        try:
            serializer.dumps(CustomValue("value"))
        except SerializationError as error:
            self.assertIn("CustomValue", str(error))
        else:
            raise AssertionError("CustomValue unexpectedly used default pickle")

    def test_provider_dispatch_table_round_trips_custom_value(self) -> None:
        serializer = PickleSerializer([CustomValuePickler])

        restored = serializer.loads(serializer.dumps(CustomValue("value")))

        self.assertEqual(restored, CustomValue("value"))

    def test_imported_module_shape_is_accepted(self) -> None:
        provider = types.SimpleNamespace(
            __name__="application.custom_pickler",
            get_dispatch_table=lambda: {CustomValue: reduce_custom_value},
        )
        serializer = PickleSerializer([provider])

        restored = serializer.loads(serializer.dumps(CustomValue("module")))

        self.assertEqual(restored, CustomValue("module"))

    def test_dispatch_mapping_can_be_passed_directly(self) -> None:
        serializer = PickleSerializer([{CustomValue: reduce_custom_value}])

        restored = serializer.loads(serializer.dumps(CustomValue("mapping")))

        self.assertEqual(restored, CustomValue("mapping"))

    def test_later_provider_overrides_reducer_for_same_type(self) -> None:
        serializer = PickleSerializer(
            [
                {CustomValue: reduce_as_first},
                {CustomValue: reduce_as_second},
            ]
        )

        restored = serializer.loads(serializer.dumps(CustomValue("original")))

        self.assertEqual(restored, CustomValue("second"))

    def test_serializer_does_not_mutate_process_copyreg_table(self) -> None:
        before = copyreg.dispatch_table.copy()

        PickleSerializer([{CustomValue: reduce_custom_value}])

        self.assertEqual(copyreg.dispatch_table, before)
        self.assertNotIn(CustomValue, copyreg.dispatch_table)

    def test_provider_must_expose_dispatch_table(self) -> None:
        try:
            PickleSerializer([object()])
        except CustomPicklerError as error:
            self.assertIn("get_dispatch_table", str(error))
        else:
            raise AssertionError("Invalid provider was accepted")

    def test_provider_table_and_entries_are_validated(self) -> None:
        invalid_table = types.SimpleNamespace(
            __name__="invalid_table",
            get_dispatch_table=lambda: [],
        )
        invalid_key = {"not-a-type": reduce_custom_value}
        invalid_reducer = {CustomValue: "not-callable"}

        for provider in (invalid_table, invalid_key, invalid_reducer):
            try:
                PickleSerializer([provider])
            except CustomPicklerError:
                pass
            else:
                raise AssertionError(f"Invalid custom pickler accepted: {provider!r}")
