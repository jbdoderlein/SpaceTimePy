from __future__ import annotations

import datetime
import unittest

from spacetimepy import AlignmentRelation, CodeDefinitionDTO, CodeDiffLineMapper


def code(source: str, start: int = 1) -> CodeDefinitionDTO:
    return CodeDefinitionDTO(
        id=str(start),
        name="f",
        qualified_name="f",
        kind="function",
        module_path="example.py",
        code_content=source,
        first_line_number=start,
        created_at=datetime.datetime(2026, 1, 1),
    )


class TestCodeDiffLineMapper(unittest.TestCase):
    def check_mapping(self, reference, target, expected):
        mapping = CodeDiffLineMapper().compare(reference, target)
        self.assertEqual(
            [
                (p.reference_line, p.target_line, p.relation.value)
                for p in mapping.correspondences
            ],
            expected,
        )
        return mapping

    def test_multiline_formatting_preserves_statement_start(self) -> None:
        mapping = self.check_mapping(
            code("def f(left, right):\n    result = left + right\n    return result\n"),
            code(
                "def f(left, right):\n    result = (\n        left\n        + right\n    )\n    return result\n"
            ),
            [(1, 1, "match"), (2, 2, "match"), (3, 6, "match")],
        )
        self.assertEqual(mapping.edit_script, ())

    def test_literal_update_uses_target_start_with_different_offsets(self) -> None:
        self.check_mapping(
            code("def f():\n    value = 1\n    return value\n", 10),
            code("def f():\n    value = 2\n    return value\n", 50),
            [(10, 50, "match"), (11, 51, "updated"), (12, 52, "match")],
        )

    def test_complete_statement_insert_and_delete(self) -> None:
        original = "def f(x):\n    value = x + 1\n    return value\n"
        changed = "def f(x):\n    value = x + 1\n    print(value)\n    return value\n"
        for reference, target, deleted, inserted, pairs in (
            (original, changed, (), (3,), [(1, 1), (2, 2), (3, 4)]),
            (changed, original, (3,), (), [(1, 1), (2, 2), (4, 3)]),
        ):
            with self.subTest(reference=reference):
                mapping = self.check_mapping(
                    code(reference), code(target), [(a, b, "match") for a, b in pairs]
                )
                self.assertEqual(mapping.deleted_lines, deleted)
                self.assertEqual(mapping.inserted_lines, inserted)

    def test_operator_and_subexpression_edits_keep_statement_pair(self) -> None:
        for before, after in (
            ("if x > 0:", "if x >= 0:"),
            ("value = x", "value = x + 1"),
            ("value = x + 1", "value = x"),
        ):
            with self.subTest(before=before, after=after):
                suffix = (
                    "        return x\n"
                    if before.startswith("if")
                    else "    return value\n"
                )
                self.check_mapping(
                    code(f"def f(x):\n    {before}\n{suffix}"),
                    code(f"def f(x):\n    {after}\n{suffix}"),
                    [(1, 1, "match"), (2, 2, "updated"), (3, 3, "match")],
                )

    def test_multiple_nodes_per_line_are_deterministic_and_one_to_one(self) -> None:
        reference = code("def f(x):\n    a = x; b = x + 1\n    return a + b\n", 20)
        target = code("def f(x):\n    a = x\n    b = x + 2\n    return a + b\n", 100)
        for _ in range(10):
            self.check_mapping(
                reference,
                target,
                [(20, 100, "match"), (21, 101, "updated"), (22, 103, "match")],
            )

    def test_identical_code_uses_only_ast_start_lines(self) -> None:
        source = "def f():\n    # A comment.\n    value = (\n        1\n    )\n    return value\n"
        mapping = self.check_mapping(
            code(source, 10),
            code(source, 30),
            [
                (10, 30, "match"),
                (12, 32, "match"),
                (13, 33, "match"),
                (15, 35, "match"),
            ],
        )
        self.assertTrue(
            all(p.relation == AlignmentRelation.MATCH for p in mapping.correspondences)
        )
