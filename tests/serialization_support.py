"""Reusable custom value and reducer fixtures for serialization tests."""

from __future__ import annotations


class CustomValue:
    def __init__(self, value: str) -> None:
        self.value = value
        self.unpicklable_callback = lambda: value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CustomValue) and other.value == self.value


def rebuild_custom_value(value: str) -> CustomValue:
    return CustomValue(value)


def reduce_custom_value(value: CustomValue):
    return rebuild_custom_value, (value.value,)


def reduce_as_first(_value: CustomValue):
    return rebuild_custom_value, ("first",)


def reduce_as_second(_value: CustomValue):
    return rebuild_custom_value, ("second",)


class CustomValuePickler:
    @staticmethod
    def get_dispatch_table():
        return {CustomValue: reduce_custom_value}
