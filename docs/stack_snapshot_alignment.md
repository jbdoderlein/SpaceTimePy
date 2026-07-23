# Stack-snapshot alignment

This algorithm aligns two traces recorded at stack-snapshot granularity.

First, a code diff maps lines from the reference program to lines in the
target program. A mapped line is either unchanged or updated; an unmapped line
is deleted or inserted.

The built-in implementation uses `code-diff`'s GumTree matcher and edit script
to create that line mapping. It is registered as `stack-snapshot` and is
selected automatically when no algorithm is specified for a stack-snapshot
session.

Snapshots may correspond only when their code lines correspond. The two
chronological snapshot sequences are then aligned using a constrained
Levenshtein-style alignment:

- snapshots on corresponding unchanged lines produce `match`;
- snapshots on corresponding modified lines produce `updated`;
- an unpaired reference snapshot produces `deleted`;
- an unpaired target snapshot produces `inserted`.

The preferred alignment pairs as many compatible snapshots as possible.
When several alignments are equivalent, it chooses the one with the smallest
chronological distance between paired snapshots.
