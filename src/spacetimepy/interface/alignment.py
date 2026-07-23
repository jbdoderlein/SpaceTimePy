"""Pluggable trace-alignment interfaces.

Alignment algorithms operate exclusively on the public DTOs from
``spacetimepy.interface.data``.  SpaceTimePy prepares the reference and target
branch suffixes, while algorithms decide how their steps correspond.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .data import BranchDTO, CodeDefinitionDTO, StepDTO, TraceData


class AlignmentError(RuntimeError):
    """Base error raised by the alignment interface."""


class AlignmentAlgorithmNotFoundError(AlignmentError):
    """Raised when a named alignment algorithm is not registered."""


class CodeDiffProviderNotFoundError(AlignmentError):
    """Raised when an algorithm requests an unknown code-diff provider."""


class AlignmentValidationError(AlignmentError):
    """Raised when an algorithm returns links outside its alignment context."""


class AlignmentRelation(StrEnum):
    """Relationship between one reference and one target trace step."""

    MATCH = "match"
    UPDATED = "updated"
    INSERTED = "inserted"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class AlignmentLink:
    """One algorithm-produced correspondence between trace steps."""

    reference_step: StepDTO | None
    target_step: StepDTO | None
    relation: AlignmentRelation

    def __post_init__(self) -> None:
        relation = AlignmentRelation(self.relation)
        object.__setattr__(self, "relation", relation)

        if relation in {AlignmentRelation.MATCH, AlignmentRelation.UPDATED}:
            if self.reference_step is None or self.target_step is None:
                raise ValueError(f"{relation.value} alignment links require both steps")
            return
        if relation == AlignmentRelation.INSERTED:
            if self.reference_step is not None or self.target_step is None:
                raise ValueError("inserted alignment links require only a target step")
            return
        if self.reference_step is None or self.target_step is not None:
            raise ValueError("deleted alignment links require only a reference step")


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    """Transient output of one branch-alignment run."""

    algorithm: str
    algorithm_version: str
    reference_branch_id: int
    target_branch_id: int
    links: tuple[AlignmentLink, ...]

    def __post_init__(self) -> None:
        reference_ids: set[int] = set()
        target_ids: set[int] = set()
        for link in self.links:
            if link.reference_step is not None:
                if link.reference_step.id in reference_ids:
                    raise ValueError(
                        f"Reference step {link.reference_step.id} has multiple "
                        "alignment links"
                    )
                reference_ids.add(link.reference_step.id)
            if link.target_step is not None:
                if link.target_step.id in target_ids:
                    raise ValueError(
                        f"Target step {link.target_step.id} has multiple "
                        "alignment links"
                    )
                target_ids.add(link.target_step.id)

    def link_for_reference(
        self,
        step: StepDTO | int,
    ) -> AlignmentLink | None:
        """Return the link containing one reference step."""

        step_id = step if isinstance(step, int) else step.id
        return next(
            (
                link
                for link in self.links
                if link.reference_step is not None and link.reference_step.id == step_id
            ),
            None,
        )

    def link_for_target(
        self,
        step: StepDTO | int,
    ) -> AlignmentLink | None:
        """Return the link containing one target step."""

        step_id = step if isinstance(step, int) else step.id
        return next(
            (
                link
                for link in self.links
                if link.target_step is not None and link.target_step.id == step_id
            ),
            None,
        )

    def reference_for(self, target_step: StepDTO | int) -> StepDTO | None:
        """Return the reference step corresponding to one target step."""

        link = self.link_for_target(target_step)
        return link.reference_step if link is not None else None

    def target_for(self, reference_step: StepDTO | int) -> StepDTO | None:
        """Return the target step corresponding to one reference step."""

        link = self.link_for_reference(reference_step)
        return link.target_step if link is not None else None


@dataclass(frozen=True, slots=True)
class AlignmentAlgorithmDescriptor:
    """One named alignment implementation available in a runtime."""

    name: str
    version: str
    offline: bool
    online: bool


class CodeDiffProvider(Protocol):
    """Compare two stored source definitions using one diff implementation."""

    def compare(
        self,
        reference: CodeDefinitionDTO,
        target: CodeDefinitionDTO,
    ) -> object: ...


class AlignmentData:
    """Lazy code, value and code-diff access for alignment algorithms."""

    def __init__(self, trace_data: TraceData) -> None:
        self.trace = trace_data
        self._diff_providers: dict[str, CodeDiffProvider] = {}
        self._diff_cache: dict[tuple[str, str, str], object] = {}

    def register_diff_provider(
        self,
        name: str,
        provider: CodeDiffProvider,
        *,
        replace: bool = False,
    ) -> None:
        """Register a named code-diff provider used by :meth:`diff`."""

        if not name:
            raise ValueError("A code-diff provider name cannot be empty")
        if name in self._diff_providers and not replace:
            raise ValueError(f"Code-diff provider {name!r} is already registered")
        if not callable(getattr(provider, "compare", None)):
            raise TypeError("A code-diff provider must define compare()")
        self._diff_providers[name] = provider
        self._diff_cache = {
            key: value for key, value in self._diff_cache.items() if key[0] != name
        }

    def has_diff_provider(self, name: str) -> bool:
        """Report whether a named code-diff provider is available."""

        return name in self._diff_providers

    def code(self, step: StepDTO) -> CodeDefinitionDTO | None:
        """Return the code definition effective for one step."""

        definition_id: str | None
        if step.function_call is not None:
            definition_id = step.function_call.code_definition_id
        elif step.stack_snapshot is not None:
            definition_id = step.stack_snapshot.code_definition_id
            if definition_id is None:
                call = self.trace.get_function_call(
                    step.stack_snapshot.function_call_id
                )
                definition_id = call.code_definition_id
        else:
            definition_id = None

        return (
            self.trace.get_code_definition(definition_id)
            if definition_id is not None
            else None
        )

    @staticmethod
    def references(
        step: StepDTO,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return captured local and global references without loading values."""

        if step.function_call is not None:
            return (
                dict(step.function_call.entry_local_references),
                dict(step.function_call.entry_global_references),
            )
        if step.stack_snapshot is not None:
            return (
                dict(step.stack_snapshot.local_references),
                dict(step.stack_snapshot.global_references),
            )
        return {}, {}

    def values(
        self,
        step: StepDTO,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Materialize the captured local and global values of one step."""

        local_references, global_references = self.references(step)
        return (
            self.trace.load_references(local_references),
            self.trace.load_references(global_references),
        )

    def diff(
        self,
        reference: StepDTO,
        target: StepDTO,
        *,
        provider: str = "code-diff",
    ) -> object | None:
        """Lazily compare the source definitions effective for two steps."""

        selected = self._diff_providers.get(provider)
        if selected is None:
            raise CodeDiffProviderNotFoundError(
                f"No code-diff provider named {provider!r} is registered"
            )

        reference_code = self.code(reference)
        target_code = self.code(target)
        if reference_code is None or target_code is None:
            return None

        cache_key = (provider, reference_code.id, target_code.id)
        if cache_key not in self._diff_cache:
            self._diff_cache[cache_key] = selected.compare(
                reference_code,
                target_code,
            )
        return self._diff_cache[cache_key]


@dataclass(frozen=True, slots=True)
class OfflineAlignmentContext:
    """Complete recorded inputs supplied to an offline alignment algorithm."""

    reference_branch: BranchDTO
    target_branch: BranchDTO
    reference_steps: tuple[StepDTO, ...]
    target_steps: tuple[StepDTO, ...]
    data: AlignmentData
    options: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class OnlineAlignmentContext:
    """Recorded reference inputs supplied when an online alignment starts."""

    reference_branch: BranchDTO
    target_branch_id: int
    reference_steps: tuple[StepDTO, ...]
    data: AlignmentData
    options: Mapping[str, Any]


class OfflineAlignmentAlgorithm(Protocol):
    """Algorithm capable of aligning two complete recorded branch suffixes."""

    name: str
    version: str

    def align(self, context: OfflineAlignmentContext) -> tuple[AlignmentLink, ...]: ...


class OnlineAlignmentSession(Protocol):
    """Mutable state of one online alignment run."""

    def align(self, target_step: StepDTO) -> tuple[AlignmentLink, ...]: ...

    def finish(self) -> tuple[AlignmentLink, ...]: ...


class OnlineAlignmentAlgorithm(Protocol):
    """Algorithm capable of aligning target steps as they are recorded."""

    name: str
    version: str

    def start(self, context: OnlineAlignmentContext) -> OnlineAlignmentSession: ...


class _OfflineAlignmentFactory(Protocol):
    def __call__(self) -> OfflineAlignmentAlgorithm: ...


class _OnlineAlignmentFactory(Protocol):
    def __call__(self) -> OnlineAlignmentAlgorithm: ...


@dataclass(frozen=True, slots=True)
class _AlignmentRegistration:
    name: str
    version: str
    offline: _OfflineAlignmentFactory | None
    online: _OnlineAlignmentFactory | None


class AlignmentRegistry:
    """Runtime-local registry of named offline and online algorithms."""

    def __init__(self) -> None:
        self._registrations: dict[str, _AlignmentRegistration] = {}

    def register(
        self,
        name: str,
        *,
        version: str,
        offline: _OfflineAlignmentFactory | None = None,
        online: _OnlineAlignmentFactory | None = None,
        replace: bool = False,
    ) -> None:
        if not name:
            raise ValueError("An alignment algorithm name cannot be empty")
        if not version:
            raise ValueError("An alignment algorithm version cannot be empty")
        if offline is None and online is None:
            raise ValueError(
                "An alignment registration needs an offline or online factory"
            )
        if offline is not None and not callable(offline):
            raise TypeError("An offline alignment factory must be callable")
        if online is not None and not callable(online):
            raise TypeError("An online alignment factory must be callable")
        if name in self._registrations and not replace:
            raise ValueError(f"Alignment algorithm {name!r} is already registered")
        self._registrations[name] = _AlignmentRegistration(
            name=name,
            version=version,
            offline=offline,
            online=online,
        )

    def unregister(self, name: str) -> bool:
        """Remove one named registration and report whether it existed."""

        return self._registrations.pop(name, None) is not None

    def contains(self, name: str) -> bool:
        """Report whether an algorithm name is registered."""

        return name in self._registrations

    def algorithms(self) -> tuple[AlignmentAlgorithmDescriptor, ...]:
        """Describe registered algorithms without constructing them."""

        return tuple(
            AlignmentAlgorithmDescriptor(
                name=registration.name,
                version=registration.version,
                offline=registration.offline is not None,
                online=registration.online is not None,
            )
            for registration in self._registrations.values()
        )

    def _require(self, name: str) -> _AlignmentRegistration:
        registration = self._registrations.get(name)
        if registration is None:
            raise AlignmentAlgorithmNotFoundError(
                f"No alignment algorithm named {name!r} is registered"
            )
        return registration


@dataclass(frozen=True, slots=True)
class _SelectedOfflineAlgorithm:
    name: str
    version: str
    algorithm: OfflineAlignmentAlgorithm


@dataclass(frozen=True, slots=True)
class _SelectedOnlineAlgorithm:
    name: str
    version: str
    algorithm: OnlineAlignmentAlgorithm


class AlignmentService:
    """Select algorithms and prepare branch suffixes for alignment."""

    def __init__(
        self,
        trace_data: TraceData,
        registry: AlignmentRegistry | None = None,
    ) -> None:
        self.data = AlignmentData(trace_data)
        self.registry = registry or AlignmentRegistry()
        self._register_builtins()

    def register(
        self,
        name: str,
        *,
        version: str,
        offline: _OfflineAlignmentFactory | None = None,
        online: _OnlineAlignmentFactory | None = None,
        replace: bool = False,
    ) -> None:
        self.registry.register(
            name,
            version=version,
            offline=offline,
            online=online,
            replace=replace,
        )

    def register_diff_provider(
        self,
        name: str,
        provider: CodeDiffProvider,
        *,
        replace: bool = False,
    ) -> None:
        self.data.register_diff_provider(name, provider, replace=replace)

    def compare(
        self,
        *,
        reference_branch_id: int,
        target_branch_id: int,
        algorithm: str | OfflineAlignmentAlgorithm | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> AlignmentResult | None:
        """Align branch suffixes, selecting the granularity default when omitted."""

        reference, target, reference_steps = self._branch_inputs(
            reference_branch_id,
            target_branch_id,
        )
        if algorithm is None:
            session = self.data.trace.get_session(reference.session_id)
            if session.step_kind != "stack_snapshot":
                return None
            from .stack_snapshot_alignment import STACK_SNAPSHOT_ALIGNMENT

            algorithm = STACK_SNAPSHOT_ALIGNMENT
        selected = self._select_offline(algorithm)
        context = OfflineAlignmentContext(
            reference_branch=reference,
            target_branch=target,
            reference_steps=reference_steps,
            target_steps=target.steps,
            data=self.data,
            options=MappingProxyType(dict(options or {})),
        )
        links = selected.algorithm.align(context)
        self._validate_links_for_steps(
            links,
            reference_steps=reference_steps,
            target_steps=target.steps,
        )
        return AlignmentResult(
            algorithm=selected.name,
            algorithm_version=selected.version,
            reference_branch_id=reference_branch_id,
            target_branch_id=target_branch_id,
            links=links,
        )

    def _register_builtins(self) -> None:
        from .stack_snapshot_alignment import (
            STACK_SNAPSHOT_ALIGNMENT,
            STACK_SNAPSHOT_ALIGNMENT_VERSION,
            StackSnapshotAlignment,
        )

        if not self.registry.contains(STACK_SNAPSHOT_ALIGNMENT):
            self.registry.register(
                STACK_SNAPSHOT_ALIGNMENT,
                version=STACK_SNAPSHOT_ALIGNMENT_VERSION,
                offline=StackSnapshotAlignment,
            )

    def start_online(
        self,
        *,
        reference_branch_id: int,
        target_branch_id: int,
        algorithm: str | OnlineAlignmentAlgorithm,
        options: Mapping[str, Any] | None = None,
    ) -> OnlineAlignmentRun:
        """Start an online run whose target steps will be supplied incrementally."""

        selected = self._select_online(algorithm)
        reference, _target, reference_steps = self._branch_inputs(
            reference_branch_id,
            target_branch_id,
        )
        context = OnlineAlignmentContext(
            reference_branch=reference,
            target_branch_id=target_branch_id,
            reference_steps=reference_steps,
            data=self.data,
            options=MappingProxyType(dict(options or {})),
        )
        session = selected.algorithm.start(context)
        if not callable(getattr(session, "align", None)) or not callable(
            getattr(session, "finish", None)
        ):
            raise TypeError(
                "An online alignment algorithm must return a session with "
                "align() and finish()"
            )
        return OnlineAlignmentRun(
            session,
            algorithm=selected.name,
            algorithm_version=selected.version,
            reference_branch_id=reference_branch_id,
            reference_steps=reference_steps,
            target_branch_id=target_branch_id,
        )

    def _branch_inputs(
        self,
        reference_branch_id: int,
        target_branch_id: int,
    ) -> tuple[BranchDTO, BranchDTO, tuple[StepDTO, ...]]:
        reference = self.data.trace.get_branch(reference_branch_id, resolve=True)
        target = self.data.trace.get_branch(target_branch_id)
        if target.parent_branch_id != reference_branch_id:
            raise AlignmentValidationError(
                f"Target branch {target_branch_id} is not a direct child of "
                f"reference branch {reference_branch_id}"
            )
        if target.forked_from_step_id is None:
            raise AlignmentValidationError(
                f"Target branch {target_branch_id} has no fork step"
            )

        for position, step in enumerate(reference.steps):
            if step.id == target.forked_from_step_id:
                return reference, target, reference.steps[position:]

        raise AlignmentValidationError(
            f"Fork step {target.forked_from_step_id} is not on resolved "
            f"reference branch {reference_branch_id}"
        )

    def _select_offline(
        self,
        algorithm: str | OfflineAlignmentAlgorithm,
    ) -> _SelectedOfflineAlgorithm:
        if isinstance(algorithm, str):
            registration = self.registry._require(algorithm)
            if registration.offline is None:
                raise AlignmentAlgorithmNotFoundError(
                    f"Alignment algorithm {algorithm!r} has no offline implementation"
                )
            selected = registration.offline()
            name, version = registration.name, registration.version
        else:
            selected = algorithm
            name, version = self._direct_identity(selected)
        if not callable(getattr(selected, "align", None)):
            raise TypeError("An offline alignment algorithm must define align()")
        return _SelectedOfflineAlgorithm(name, version, selected)

    def _select_online(
        self,
        algorithm: str | OnlineAlignmentAlgorithm,
    ) -> _SelectedOnlineAlgorithm:
        if isinstance(algorithm, str):
            registration = self.registry._require(algorithm)
            if registration.online is None:
                raise AlignmentAlgorithmNotFoundError(
                    f"Alignment algorithm {algorithm!r} has no online implementation"
                )
            selected = registration.online()
            name, version = registration.name, registration.version
        else:
            selected = algorithm
            name, version = self._direct_identity(selected)
        if not callable(getattr(selected, "start", None)):
            raise TypeError("An online alignment algorithm must define start()")
        return _SelectedOnlineAlgorithm(name, version, selected)

    @staticmethod
    def _direct_identity(algorithm: object) -> tuple[str, str]:
        name = getattr(algorithm, "name", None)
        version = getattr(algorithm, "version", None)
        if not isinstance(name, str) or not name:
            raise TypeError("A direct alignment algorithm needs a non-empty name")
        if not isinstance(version, str) or not version:
            raise TypeError("A direct alignment algorithm needs a non-empty version")
        return name, version

    @classmethod
    def _validate_links_for_steps(
        cls,
        links: tuple[AlignmentLink, ...],
        *,
        reference_steps: tuple[StepDTO, ...],
        target_steps: tuple[StepDTO, ...],
    ) -> None:
        cls._validate_links(
            links,
            reference_ids={step.id for step in reference_steps},
            target_ids={step.id for step in target_steps},
        )

    @staticmethod
    def _validate_links(
        links: tuple[AlignmentLink, ...],
        *,
        reference_ids: set[int],
        target_ids: set[int],
    ) -> None:
        if not isinstance(links, tuple) or not all(
            isinstance(link, AlignmentLink) for link in links
        ):
            raise TypeError("Alignment links must be a tuple of AlignmentLink")
        for link in links:
            if (
                link.reference_step is not None
                and link.reference_step.id not in reference_ids
            ):
                raise AlignmentValidationError(
                    f"Reference step {link.reference_step.id} is outside the "
                    "alignment reference suffix"
                )
            if link.target_step is not None and link.target_step.id not in target_ids:
                raise AlignmentValidationError(
                    f"Target step {link.target_step.id} is outside the target branch"
                )


class OnlineAlignmentRun:
    """Validated, transient state and accumulated links of one online run."""

    def __init__(
        self,
        session: OnlineAlignmentSession,
        *,
        algorithm: str,
        algorithm_version: str,
        reference_branch_id: int,
        reference_steps: tuple[StepDTO, ...],
        target_branch_id: int,
    ) -> None:
        self._session = session
        self._algorithm = algorithm
        self._algorithm_version = algorithm_version
        self._reference_branch_id = reference_branch_id
        self._reference_ids = {step.id for step in reference_steps}
        self._target_branch_id = target_branch_id
        self._seen_target_ids: set[int] = set()
        self._links: list[AlignmentLink] = []
        self._result: AlignmentResult | None = None

    @property
    def links(self) -> tuple[AlignmentLink, ...]:
        return tuple(self._links)

    @property
    def result(self) -> AlignmentResult | None:
        """Return the final result after :meth:`finish`, otherwise ``None``."""

        return self._result

    def link_for_target(self, step: StepDTO | int) -> AlignmentLink | None:
        """Return the latest accumulated link for one target step."""

        step_id = step if isinstance(step, int) else step.id
        return next(
            (
                link
                for link in reversed(self._links)
                if link.target_step is not None and link.target_step.id == step_id
            ),
            None,
        )

    def align(self, target_step: StepDTO) -> tuple[AlignmentLink, ...]:
        if self._result is not None:
            raise AlignmentValidationError("The online alignment is already finished")
        if target_step.branch_id != self._target_branch_id:
            raise AlignmentValidationError(
                f"Target step {target_step.id} belongs to branch "
                f"{target_step.branch_id}, not online target branch "
                f"{self._target_branch_id}"
            )
        self._seen_target_ids.add(target_step.id)
        links = self._session.align(target_step)
        AlignmentService._validate_links(
            links,
            reference_ids=self._reference_ids,
            target_ids=self._seen_target_ids,
        )
        self._append_links(links)
        return links

    def finish(self) -> tuple[AlignmentLink, ...]:
        if self._result is not None:
            raise AlignmentValidationError("The online alignment is already finished")
        links = self._session.finish()
        AlignmentService._validate_links(
            links,
            reference_ids=self._reference_ids,
            target_ids=self._seen_target_ids,
        )
        self._append_links(links)
        self._result = AlignmentResult(
            algorithm=self._algorithm,
            algorithm_version=self._algorithm_version,
            reference_branch_id=self._reference_branch_id,
            target_branch_id=self._target_branch_id,
            links=tuple(self._links),
        )
        return links

    def _append_links(self, links: tuple[AlignmentLink, ...]) -> None:
        # Constructing the transient result enforces the one-link-per-step
        # invariant before the online session can expose ambiguous state.
        combined = (*self._links, *links)
        AlignmentResult(
            algorithm=self._algorithm,
            algorithm_version=self._algorithm_version,
            reference_branch_id=self._reference_branch_id,
            target_branch_id=self._target_branch_id,
            links=combined,
        )
        self._links.extend(links)


__all__ = [
    "AlignmentAlgorithmDescriptor",
    "AlignmentAlgorithmNotFoundError",
    "AlignmentData",
    "AlignmentError",
    "AlignmentLink",
    "AlignmentRegistry",
    "AlignmentRelation",
    "AlignmentResult",
    "AlignmentService",
    "AlignmentValidationError",
    "CodeDiffProvider",
    "CodeDiffProviderNotFoundError",
    "OfflineAlignmentAlgorithm",
    "OfflineAlignmentContext",
    "OnlineAlignmentAlgorithm",
    "OnlineAlignmentContext",
    "OnlineAlignmentRun",
    "OnlineAlignmentSession",
]
