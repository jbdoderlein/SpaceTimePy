"""Runtime-scoped Dill configuration for captured Python values."""

from __future__ import annotations

import copy
import copyreg
import io
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Any, Protocol

import dill

type Reducer = Callable[[Any], Any]
type DispatchTable = Mapping[type[Any], Reducer]


class CustomPicklerProvider(Protocol):
    """Public contract implemented by an imported custom-pickler module."""

    def get_dispatch_table(self) -> DispatchTable:
        """Return Python types mapped to copyreg-style reduction functions."""


type CustomPickler = CustomPicklerProvider | DispatchTable


class CustomPicklerError(ValueError):
    """Raised when a custom-pickler provider has an invalid contract."""


class SerializationError(RuntimeError):
    """Raised when a captured value cannot be serialized or restored."""


class DillSerializer:
    """Serialize values with an isolated custom reduction dispatch table.

    Providers are evaluated once during runtime creation. Their reducers are
    applied in list order, so a later provider replaces an earlier reducer for
    the same exact Python type. Dill's and the process-wide ``copyreg`` dispatch
    tables are copied during runtime creation, never mutated.
    """

    def __init__(self, custom_picklers: Iterable[CustomPickler] = ()) -> None:
        providers = tuple(custom_picklers)
        dispatch_table: dict[type[Any], Reducer] = copyreg.dispatch_table.copy()
        for provider in providers:
            dispatch_table.update(self._dispatch_table_from(provider))

        self.custom_picklers = providers
        self.dispatch_table: Mapping[type[Any], Reducer] = MappingProxyType(
            dispatch_table
        )
        dill_dispatch = copy.copy(dill.Pickler.dispatch)
        dill_dispatch.update(
            {
                python_type: self._dill_save_function(reducer)
                for python_type, reducer in dispatch_table.items()
            }
        )
        self._dill_dispatch = dill_dispatch

    def dumps(self, value: Any) -> bytes:
        buffer = io.BytesIO()
        pickler = dill.Pickler(buffer, protocol=dill.HIGHEST_PROTOCOL)
        pickler.dispatch = self._dill_dispatch
        try:
            pickler.dump(value)
        except Exception as error:
            type_name = f"{type(value).__module__}.{type(value).__qualname__}"
            raise SerializationError(
                f"Could not serialize value of type {type_name}"
            ) from error
        return buffer.getvalue()

    def loads(self, data: bytes) -> Any:
        try:
            return dill.loads(data)
        except Exception as error:
            raise SerializationError("Could not restore a stored Python value") from error

    @staticmethod
    def _dill_save_function(reducer: Reducer) -> Callable[[Any, Any], None]:
        """Adapt a copyreg-style reducer to Dill's per-type save callback."""

        def save(pickler: Any, value: Any) -> None:
            reduction = reducer(value)
            if isinstance(reduction, str):
                pickler.save_global(value, name=reduction)
                return
            pickler.save_reduce(*reduction, obj=value)

        return save

    @classmethod
    def _dispatch_table_from(cls, provider: CustomPickler) -> dict[type[Any], Reducer]:
        if isinstance(provider, Mapping):
            table = provider
        else:
            get_dispatch_table = getattr(provider, "get_dispatch_table", None)
            if not callable(get_dispatch_table):
                raise CustomPicklerError(
                    f"Custom pickler {cls._provider_name(provider)} must be a "
                    "mapping or expose get_dispatch_table()"
                )
            try:
                table = get_dispatch_table()
            except Exception as error:
                raise CustomPicklerError(
                    f"Custom pickler {cls._provider_name(provider)} failed while "
                    "building its dispatch table"
                ) from error

        if not isinstance(table, Mapping):
            raise CustomPicklerError(
                f"Custom pickler {cls._provider_name(provider)} returned "
                f"{type(table).__name__}, expected a mapping"
            )

        validated: dict[type[Any], Reducer] = {}
        for python_type, reducer in table.items():
            if not isinstance(python_type, type):
                raise CustomPicklerError(
                    "Custom pickler dispatch-table keys must be Python types"
                )
            if not callable(reducer):
                raise CustomPicklerError(
                    f"Reducer for {python_type.__module__}."
                    f"{python_type.__qualname__} must be callable"
                )
            validated[python_type] = reducer
        return validated

    @staticmethod
    def _provider_name(provider: object) -> str:
        return getattr(provider, "__name__", type(provider).__qualname__)


__all__ = [
    "CustomPickler",
    "CustomPicklerError",
    "CustomPicklerProvider",
    "DispatchTable",
    "DillSerializer",
    "Reducer",
    "SerializationError",
]
