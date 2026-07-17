"""Lazy registration of standard-library external interactions."""

from __future__ import annotations

import importlib.machinery
import logging
import sys
from contextlib import suppress
from threading import RLock
from types import ModuleType
from typing import TYPE_CHECKING, Any

from .random import DEFINITION as RANDOM_DEFINITION

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from importlib.machinery import ModuleSpec

    from spacetimepy.core.monitoring import SpaceTimeMonitor

    from .definitions import StandardExternalModule


logger = logging.getLogger(__name__)

STANDARD_EXTERNAL_MODULES: tuple[StandardExternalModule, ...] = (
    RANDOM_DEFINITION,
)


class _NotifyingLoader:
    """Delegate module execution and notify only after it succeeds."""

    def __init__(
        self,
        loader: Any,
        on_loaded: Callable[[ModuleType], None],
    ) -> None:
        self._loader = loader
        self._on_loaded = on_loaded

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        create_module = getattr(self._loader, "create_module", None)
        if create_module is None:
            return None
        return create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self._loader.exec_module(module)
        self._on_loaded(module)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loader, name)


class _StandardModuleFinder:
    """Wrap PathFinder loaders only for modules in the standard catalogue."""

    def __init__(
        self,
        module_names: frozenset[str],
        on_loaded: Callable[[ModuleType], None],
    ) -> None:
        self._module_names = module_names
        self._on_loaded = on_loaded

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname not in self._module_names:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None:
            spec.loader = _NotifyingLoader(spec.loader, self._on_loaded)
        return spec


class StandardExternalInteractionRegistry:
    """Observe configured imports and register their callables on a monitor."""

    def __init__(
        self,
        monitor: SpaceTimeMonitor,
        definitions: tuple[
            StandardExternalModule, ...
        ] = STANDARD_EXTERNAL_MODULES,
    ) -> None:
        self._monitor = monitor
        self._definitions = {
            definition.module_name: definition for definition in definitions
        }
        self._finder = _StandardModuleFinder(
            frozenset(self._definitions),
            self._module_loaded,
        )
        self._started = False
        self._lock = RLock()

    def start(self) -> None:
        """Watch future imports and register modules already in ``sys.modules``."""

        with self._lock:
            if self._started:
                return
            self._insert_finder()
            self._started = True
            self.refresh()

    def refresh(self) -> None:
        """Register configured modules that are currently loaded."""

        with self._lock:
            for module_name in self._definitions:
                module = sys.modules.get(module_name)
                if isinstance(module, ModuleType):
                    self._register_module(module)

    def stop(self) -> None:
        """Remove this runtime's import watcher."""

        with self._lock:
            if not self._started:
                return
            with suppress(ValueError):
                sys.meta_path.remove(self._finder)
            self._started = False

    def _module_loaded(self, module: ModuleType) -> None:
        try:
            with self._lock:
                self._register_module(module)
        except BaseException:
            # SpaceTime configuration must never make a valid standard-library
            # import fail in application code.
            logger.exception(
                "Could not register standard external interactions for %s",
                module.__name__,
            )

    def _register_module(self, module: ModuleType) -> None:
        definition = self._definitions.get(module.__name__)
        if definition is None:
            return

        # Imported here to keep catalogue import free of VM-monitoring side
        # effects and avoid pulling target standard modules into the process.
        from spacetimepy.core.monitoring import CallRole

        for interaction in definition.interactions:
            target = self._resolve_attribute(module, interaction.attribute_path)
            self._monitor.register_capture(
                target,
                role=CallRole.EXTERNAL_INTERACTION,
                ignored_names=interaction.ignored_names,
            )

    def _insert_finder(self) -> None:
        try:
            path_finder_index = sys.meta_path.index(
                importlib.machinery.PathFinder
            )
        except ValueError:
            sys.meta_path.append(self._finder)
        else:
            sys.meta_path.insert(path_finder_index, self._finder)

    @staticmethod
    def _resolve_attribute(module: ModuleType, path: str) -> Any:
        target: Any = module
        for component in path.split("."):
            target = getattr(target, component)
        return target


__all__ = [
    "STANDARD_EXTERNAL_MODULES",
    "StandardExternalInteractionRegistry",
]
