# Stack-snapshot alignment

This algorithm aligns two traces recorded at stack-snapshot granularity.
It uses the Python `code-diff` GumTree matcher and edit script.
The algorithm name is `stack-snapshot`.
A stack-snapshot session selects this algorithm when the caller does not specify one.

The mapper projects matched AST nodes onto their first source lines.
It processes parents before children in a fixed breadth-first order.
Thus, enclosing statements establish their correspondences before their subexpressions.
The mapper keeps the first valid pair for each line.
Neither line can participate in another pair.
Each version uses its own recorded function offset to calculate absolute line numbers.

An unchanged pair produces `match`.
An AST edit on either starting line of an established pair produces `updated`.
The mapper obtains an update's target position from the matched target node.
It identifies inserted constructs from unmatched nodes in the target AST.
An insertion operation can refer to the insertion parent instead of the inserted construct.
Deleted constructs use their starting positions in the reference AST.
An inserted or deleted subexpression does not remove an established statement pair.
Moves affect the starting lines of the moved nodes.

A line without a counterpart remains unpaired.
An unpaired reference line is `deleted`.
An unpaired target line is `inserted`.
This rule also applies to physical lines without an AST starting position, such as comment-only lines.
The same projection applies when both source texts are identical.
The mapper does not use text similarity, weighted votes, or configurable scoring.

For example, consider this reference function:

```python
def f(left, right):
    result = left + right
    return result
```

The target changes only the formatting:

```python
def f(left, right):
    result = (
        left
        + right
    )
    return result
```

The assignment pairs reference line 2 with target line 2.
The return pairs reference line 3 with target line 6.
Both pairs produce `match` because formatting alone creates no AST edit.

Snapshots can correspond only when their code lines correspond.
The algorithm aligns the chronological snapshot sequences with a constrained edit alignment:

- A pair on unchanged lines produces `match`.
- A pair on updated lines produces `updated`.
- An unpaired reference snapshot produces `deleted`.
- An unpaired target snapshot produces `inserted`.

The algorithm first minimizes the number of unpaired snapshots.
It then minimizes the total chronological distance between paired snapshots.
For zero-based indices `i` and `j`, the distance is
`abs(i * max(target_count - 1, 1) - j * max(reference_count - 1, 1))`.
If scores are equal, the action order is pair, delete, then insert.
