"""Built-in offline alignment for stack-snapshot traces."""

from __future__ import annotations

import ast
import textwrap
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from code_diff.ast import ASTNode, default_create_node
from code_diff.gumtree import (
    compute_chawathe_edit_script,
    gumtree_editmap,
    gumtree_isomap,
)

from .alignment import (
    AlignmentError,
    AlignmentLink,
    AlignmentRelation,
    AlignmentValidationError,
)

if TYPE_CHECKING:
    from .alignment import OfflineAlignmentContext
    from .data import CodeDefinitionDTO, StepDTO


STACK_SNAPSHOT_ALIGNMENT = "stack-snapshot"
STACK_SNAPSHOT_ALIGNMENT_VERSION = "1"


class CodeDiffLineMappingError(AlignmentError):
    """Raised when code-diff cannot produce a source-line mapping."""


@dataclass(frozen=True, slots=True)
class CodeLineCorrespondence:
    """One line correspondence extracted from a code-diff result."""

    reference_line: int
    target_line: int
    relation: AlignmentRelation

    def __post_init__(self) -> None:
        relation = AlignmentRelation(self.relation)
        if relation not in {AlignmentRelation.MATCH, AlignmentRelation.UPDATED}:
            raise ValueError("A line correspondence must be match or updated")
        object.__setattr__(self, "relation", relation)


@dataclass(frozen=True, slots=True)
class CodeLineMapping:
    """Line-level view of one code-diff edit script."""

    reference_lines: tuple[int, ...]
    target_lines: tuple[int, ...]
    correspondences: tuple[CodeLineCorrespondence, ...]
    edit_script: tuple[object, ...]

    def __post_init__(self) -> None:
        reference_lines: set[int] = set()
        target_lines: set[int] = set()
        for correspondence in self.correspondences:
            if correspondence.reference_line in reference_lines:
                raise ValueError(
                    f"Reference line {correspondence.reference_line} is mapped twice"
                )
            if correspondence.target_line in target_lines:
                raise ValueError(
                    f"Target line {correspondence.target_line} is mapped twice"
                )
            reference_lines.add(correspondence.reference_line)
            target_lines.add(correspondence.target_line)

    @property
    def deleted_lines(self) -> tuple[int, ...]:
        """Reference lines with no target correspondence."""

        mapped = {
            correspondence.reference_line for correspondence in self.correspondences
        }
        return tuple(line for line in self.reference_lines if line not in mapped)

    @property
    def inserted_lines(self) -> tuple[int, ...]:
        """Target lines with no reference correspondence."""

        mapped = {correspondence.target_line for correspondence in self.correspondences}
        return tuple(line for line in self.target_lines if line not in mapped)


class CodeDiffLineMapper:
    """Turn a ``code-diff`` GumTree edit script into a one-to-one line mapping."""

    def compare(
        self,
        reference: CodeDefinitionDTO,
        target: CodeDefinitionDTO,
    ) -> CodeLineMapping:
        reference_source = textwrap.dedent(reference.code_content)
        target_source = textwrap.dedent(target.code_content)
        reference_content = reference_source.splitlines()
        target_content = target_source.splitlines()
        reference_start = reference.first_line_number or 1
        target_start = target.first_line_number or 1
        reference_lines = tuple(
            range(reference_start, reference_start + len(reference_content))
        )
        target_lines = tuple(range(target_start, target_start + len(target_content)))

        if reference_source == target_source:
            correspondences = tuple(
                CodeLineCorrespondence(
                    reference_start + offset,
                    target_start + offset,
                    AlignmentRelation.MATCH,
                )
                for offset in range(min(len(reference_content), len(target_content)))
            )
            return CodeLineMapping(
                reference_lines,
                target_lines,
                correspondences,
                (),
            )

        try:
            reference_tree = _python_tree(reference_source)
            target_tree = _python_tree(target_source)
            node_mapping = gumtree_editmap(
                gumtree_isomap(reference_tree, target_tree),
                reference_tree,
                target_tree,
            )
            mapped_nodes = tuple(node_mapping)
            edit_script = tuple(
                compute_chawathe_edit_script(
                    node_mapping,
                    reference_tree,
                    target_tree,
                )
            )
        except Exception as error:
            raise CodeDiffLineMappingError(
                "code-diff could not compare "
                f"{reference.qualified_name or reference.name!r} with "
                f"{target.qualified_name or target.name!r}: {error}"
            ) from error

        votes: Counter[tuple[int, int]] = Counter()
        for reference_node, target_node in mapped_nodes:
            reference_row = _start_row(reference_node)
            target_row = _start_row(target_node)
            if (
                reference_row is None
                or target_row is None
                or reference_row >= len(reference_content)
                or target_row >= len(target_content)
            ):
                continue
            weight = (
                3 if not reference_node.children and not target_node.children else 1
            )
            votes[reference_row, target_row] += weight

        selected_reference_rows: set[int] = set()
        selected_target_rows: set[int] = set()
        correspondences: list[CodeLineCorrespondence] = []
        candidates = sorted(
            votes,
            key=lambda pair: (
                -votes[pair],
                reference_content[pair[0]] != target_content[pair[1]],
                abs(pair[0] - pair[1]),
                pair[0],
                pair[1],
            ),
        )
        for reference_row, target_row in candidates:
            if (
                reference_row in selected_reference_rows
                or target_row in selected_target_rows
            ):
                continue
            selected_reference_rows.add(reference_row)
            selected_target_rows.add(target_row)
            relation = (
                AlignmentRelation.MATCH
                if reference_content[reference_row] == target_content[target_row]
                else AlignmentRelation.UPDATED
            )
            correspondences.append(
                CodeLineCorrespondence(
                    reference_start + reference_row,
                    target_start + target_row,
                    relation,
                )
            )

        correspondences.sort(key=lambda item: item.reference_line)
        return CodeLineMapping(
            reference_lines,
            target_lines,
            tuple(correspondences),
            edit_script,
        )


class StackSnapshotAlignment:
    """Align complete snapshot sequences constrained by their code-line mapping."""

    name = STACK_SNAPSHOT_ALIGNMENT
    version = STACK_SNAPSHOT_ALIGNMENT_VERSION

    def align(
        self,
        context: OfflineAlignmentContext,
    ) -> tuple[AlignmentLink, ...]:
        self._validate_steps(context)
        if not context.data.has_diff_provider("code-diff"):
            context.data.register_diff_provider("code-diff", CodeDiffLineMapper())

        relation_cache: dict[
            tuple[str, str],
            dict[tuple[int, int], AlignmentRelation],
        ] = {}

        def compatible(
            reference_step: StepDTO,
            target_step: StepDTO,
        ) -> AlignmentRelation | None:
            reference_code = context.data.code(reference_step)
            target_code = context.data.code(target_step)
            if reference_code is None or target_code is None:
                return None
            cache_key = (reference_code.id, target_code.id)
            relations = relation_cache.get(cache_key)
            if relations is None:
                mapping = context.data.diff(
                    reference_step,
                    target_step,
                    provider="code-diff",
                )
                if not isinstance(mapping, CodeLineMapping):
                    raise CodeDiffLineMappingError(
                        "The 'code-diff' provider used by stack-snapshot "
                        "alignment must return CodeLineMapping"
                    )
                relations = {
                    (item.reference_line, item.target_line): item.relation
                    for item in mapping.correspondences
                }
                relation_cache[cache_key] = relations
            reference_line = reference_step.stack_snapshot.line_number
            target_line = target_step.stack_snapshot.line_number
            return relations.get((reference_line, target_line))

        return _align_sequences(
            context.reference_steps,
            context.target_steps,
            compatible,
        )

    @staticmethod
    def _validate_steps(context: OfflineAlignmentContext) -> None:
        invalid = next(
            (
                step
                for step in (*context.reference_steps, *context.target_steps)
                if step.kind != "stack_snapshot" or step.stack_snapshot is None
            ),
            None,
        )
        if invalid is not None:
            raise AlignmentValidationError(
                "The stack-snapshot alignment algorithm only accepts "
                f"stack_snapshot steps; step {invalid.id} is {invalid.kind!r}"
            )


def _align_sequences(
    reference_steps: tuple[StepDTO, ...],
    target_steps: tuple[StepDTO, ...],
    compatible: Any,
) -> tuple[AlignmentLink, ...]:
    """Compute a lexicographically scored, constrained edit alignment."""

    reference_count = len(reference_steps)
    target_count = len(target_steps)
    scores: list[list[tuple[int, int]]] = [
        [(0, 0)] * (target_count + 1) for _ in range(reference_count + 1)
    ]
    actions: list[list[str | None]] = [
        [None] * (target_count + 1) for _ in range(reference_count + 1)
    ]
    relations: list[list[AlignmentRelation | None]] = [
        [None] * target_count for _ in range(reference_count)
    ]

    for reference_index in range(reference_count, -1, -1):
        for target_index in range(target_count, -1, -1):
            if reference_index == reference_count and target_index == target_count:
                continue

            candidates: list[tuple[tuple[int, int], int, str]] = []
            if reference_index < reference_count:
                unmatched, distance = scores[reference_index + 1][target_index]
                candidates.append(((unmatched + 1, distance), 1, "delete"))
            if target_index < target_count:
                unmatched, distance = scores[reference_index][target_index + 1]
                candidates.append(((unmatched + 1, distance), 2, "insert"))
            if reference_index < reference_count and target_index < target_count:
                relation = compatible(
                    reference_steps[reference_index],
                    target_steps[target_index],
                )
                relations[reference_index][target_index] = relation
                if relation is not None:
                    unmatched, distance = scores[reference_index + 1][target_index + 1]
                    pair_distance = abs(
                        reference_index * max(target_count - 1, 1)
                        - target_index * max(reference_count - 1, 1)
                    )
                    candidates.append(
                        ((unmatched, distance + pair_distance), 0, "pair")
                    )

            score, _priority, action = min(
                candidates,
                key=lambda candidate: (candidate[0], candidate[1]),
            )
            scores[reference_index][target_index] = score
            actions[reference_index][target_index] = action

    links: list[AlignmentLink] = []
    reference_index = 0
    target_index = 0
    while reference_index < reference_count or target_index < target_count:
        action = actions[reference_index][target_index]
        if action == "pair":
            links.append(
                AlignmentLink(
                    reference_steps[reference_index],
                    target_steps[target_index],
                    relations[reference_index][target_index],
                )
            )
            reference_index += 1
            target_index += 1
        elif action == "delete":
            links.append(
                AlignmentLink(
                    reference_steps[reference_index],
                    None,
                    AlignmentRelation.DELETED,
                )
            )
            reference_index += 1
        elif action == "insert":
            links.append(
                AlignmentLink(
                    None,
                    target_steps[target_index],
                    AlignmentRelation.INSERTED,
                )
            )
            target_index += 1
        else:
            raise RuntimeError("Incomplete stack-snapshot alignment matrix")
    return tuple(links)


def _python_tree(source: str) -> ASTNode:
    parsed = ast.parse(source)
    return _convert_node(parsed)


def _convert_node(
    node: ast.AST,
    inherited_position: tuple[tuple[int, int], tuple[int, int]] | None = None,
) -> ASTNode:
    position = _python_position(node) or inherited_position
    children = [_convert_node(child, position) for child in ast.iter_child_nodes(node)]
    semantic_values = []
    for field, value in ast.iter_fields(node):
        if isinstance(value, ast.AST | list) or value is None:
            continue
        semantic_values.append(f"{field}={value!r}")
    text = "|".join(semantic_values) or (type(node).__name__ if not children else None)
    return default_create_node(
        type(node).__name__,
        children,
        text=text,
        position=position,
    )


def _python_position(
    node: ast.AST,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    line = getattr(node, "lineno", None)
    if line is None:
        return None
    column = getattr(node, "col_offset", 0)
    end_line = getattr(node, "end_lineno", line)
    end_column = getattr(node, "end_col_offset", column)
    return ((line - 1, column), (end_line - 1, end_column))


def _start_row(node: ASTNode) -> int | None:
    if node.position is None:
        return None
    return node.position[0][0]


__all__ = [
    "CodeDiffLineMapper",
    "CodeDiffLineMappingError",
    "CodeLineCorrespondence",
    "CodeLineMapping",
    "STACK_SNAPSHOT_ALIGNMENT",
    "STACK_SNAPSHOT_ALIGNMENT_VERSION",
    "StackSnapshotAlignment",
]
