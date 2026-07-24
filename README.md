# SpaceTimePy

This repository host the spacetimpy python library.

Capture decorators can be declared when modules are imported, before a
database runtime exists:

```python
import spacetimepy


@spacetimepy.line
def calculate(value):
    value += 1
    return value


space = spacetimepy.SpaceTime.open("trace.db")
with space.capture.recording(mode="line"):
    calculate(3)
space.close()
```

Opening the runtime installs all process-level declarations. Functions are
only persisted while a recording or replay branch is active.

In-process integrations can discover the user-owned runtime without importing
core monitoring state:

```python
space = spacetimepy.get_active_spacetime()
```

This returns the currently open runtime or `None`. Integrations may attach
JSON metadata through `space.capture.annotate_session(...)` and
`space.capture.annotate_branch(...)`; trace consumers receive it through the
corresponding public DTO attributes.

Debugger and notebook integrations that replace an executing Python frame can
move the logical call into a same-session child branch without placing their
frame-mutation machinery in SpaceTimePy:

```python
context = space.replay.begin_active_execution(
    source_frame=old_frame,
    replacement_frame=new_frame,
    replacement_target=new_function,
    name="edited code",
    recipe={"integration": "my-debugger"},
)
```

The source frame must be the single active captured call and its current step
becomes the fork point. SpaceTimePy transfers `sys.monitoring` ownership to the
replacement frame, closes the parent suffix, and records subsequent events in
the child branch. The integration remains responsible for constructing the
replacement frame, migrating state, redirecting execution, and calling
`space.replay.finish()` when it completes.

When an integration must first stop at a trampoline, it can defer automatic
line capture with `replacement_line_numbers=()` and then call
`space.replay.record_active_replacement_state(...)` after restoring locals.
That explicit snapshot becomes the replacement for the fork step; normal line
capture is enabled from the supplied replacement line set afterward.

An integration exploring an earlier checkpoint in the same active session can
also pass both `parent_branch_id` and `forked_from_step_id`. The active branch
is closed, recorder ownership moves to the replacement frame, and the new
branch is attached to the selected historical path. This operation can
supersede an earlier active replacement context, enabling repeated interactive
forks in one debug session.

## Trace alignment

Alignment is an optional service on the runtime. SpaceTimePy prepares the
reference suffix from the child's fork step and supplies the existing public
`BranchDTO` and `StepDTO` objects; the selected algorithm owns the matching
semantics:

```python
import spacetimepy


class MyAlignment:
    name = "my-alignment"
    version = "1"

    def align(self, context):
        return (
            spacetimepy.AlignmentLink(
                context.reference_steps[0],
                context.target_steps[0],
                spacetimepy.AlignmentRelation.MATCH,
            ),
        )


space.alignment.register(
    "my-alignment",
    version="1",
    offline=MyAlignment,
)
result = space.alignment.compare(
    reference_branch_id=parent_branch_id,
    target_branch_id=child_branch_id,
    algorithm="my-alignment",
)
```

Links use only `match`, `updated`, `inserted`, and `deleted`. Algorithms can
load captured state with `context.data.values(step)`, stored source with
`context.data.code(step)`, and the complete trace query service through
`context.data.trace`. A code-diff implementation can be registered separately
and requested lazily with `context.data.diff(reference_step, target_step)`.
For stack-snapshot sessions, omitting `algorithm` selects the built-in
`stack-snapshot` offline algorithm. It uses `code-diff` to map source lines,
then aligns the chronological snapshots. Other granularities still require an
explicit algorithm.

Online algorithms implement `start(context)` and return a stateful session
with `align(target_step)` and `finish()` methods. The same runtime service
validates each incremental link. Replay uses no alignment by default. To opt
in, pass `ReplayAlignmentPolicy(algorithm="registered-name")`; each target
step is then aligned as it is recorded, and `context.external` selects the
matched reference step's external interactions before the target step runs.
The transient final alignment is available on `ReplayResult.alignment`.

For a live runtime, the HTTP API exposes its registered implementations at
`GET /api/alignment/algorithms` and calculates a transient result with
`POST /api/alignment/compare`. The session page offers the same operation for
related parent and child branches. Its comparison workspace displays the
aligned traces above two synchronized source panes; selecting an aligned row
highlights the corresponding line on each side. An API opened from a database
path includes built-in algorithms but not process-local custom registrations.

## JSON API and web explorer

Run the combined v2 API and browser explorer for an existing trusted trace:

```bash
web-spacetimepy trace.db
```

It serves the explorer at `http://127.0.0.1:8000`, JSON endpoints under
`/api`, and generated OpenAPI documentation at `/docs`. Use `--api-only` when
only the transport API is needed, or `--host` and `--port` to change the bind
address.

The explorer currently provides:

- session and replay-branch navigation
- resolved and branch-local step sequences
- function-call search and captured entry/return state
- line-level stack timelines with stored source versions
- on-demand parent/fork alignment with side-by-side, syntax-highlighted source
- snapshots, VM call trees, and contiguous trace parts
- a graph of sessions, branches, VM observations, code, and stored values

The explorer's read-only CodeMirror bundle is committed as a package asset.
After changing `frontend/codemirror.js`, rebuild it with
`npm ci && npm run build:web`.

Applications can create either surface from an open runtime, a public
`TraceData` reader, or a database path:

```python
import spacetimepy

api = spacetimepy.create_api_app("trace.db")
explorer = spacetimepy.create_explorer_app("trace.db")

# Or expose a currently open runtime in a background thread.
server = spacetimepy.start_api(space, port=3456)
server.stop()
```

For direct offline Python access without initializing VM monitoring:

```python
with spacetimepy.TraceData.open("trace.db") as trace:
    sessions = trace.list_sessions()
    calls = trace.list_function_calls()
    statistics = trace.get_statistics()
```

Opening stored values may execute pickle data. Only explore databases you
trust, and supply the same custom pickler providers used during capture when
their classes are needed.

## MCP trace explorer for AI agents

Expose a v2 trace path to an MCP-capable coding agent using the local stdio
transport:

```bash
uv run spacetimepy-mcp trace.db
```

If `trace.db` does not exist, the MCP initializes an empty v2 trace and starts
normally. It can therefore provide the capture guide before the first run. The
same server observes committed sessions after an instrumented application
records into that file; no MCP restart is required.

For a client configuration, use an absolute database path so the selected
trace does not depend on the client's working directory:

```json
{
  "mcpServers": {
    "spacetimepy": {
      "command": "spacetimepy-mcp",
      "args": ["/absolute/path/to/trace.db"]
    }
  }
}
```

The first version is read-only and exposes five bounded, agent-oriented tools:

- `spacetime_trace_overview`
- `spacetime_search_calls`
- `spacetime_get_execution_slice`
- `spacetime_inspect_step`
- `spacetime_inspect_call`

Trace, session, branch, step, call, and stored-source resources support
drill-down without exposing the internal ORM model. The
`spacetime://guides/capture` resource and `prepare_capture` prompt teach a
coding agent how to add a small targeted capture when existing evidence is
insufficient. The MCP itself cannot edit code, run the application, replay a
branch, or compare traces.

Primitive state is previewed safely. Non-primitive pickle values remain type
and reference metadata by default. For a trace database you explicitly trust,
full bounded previews can be enabled with:

```bash
uv run spacetimepy-mcp trace.db --trust-stored-values
```

This may execute pickle data. The same custom-pickler provider used during
recording can be imported with repeatable
`--custom-pickler MODULE[:ATTRIBUTE]` options.

Streamable HTTP is available when a process transport is preferable:

```bash
uv run spacetimepy-mcp trace.db \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8000
```

The MCP endpoint is `http://127.0.0.1:8000/mcp`. This read-only first version
has no remote authentication and rejects non-loopback bind addresses.

Applications can also create the server over a path, a `TraceData` reader, or
an open `SpaceTime` runtime:

```python
import spacetimepy

mcp = spacetimepy.create_mcp_server("trace.db")
mcp.run()
```

## Capture hooks

Start and return hooks can derive JSON metadata from a captured invocation.
Their returned mappings are merged into the recorded `FunctionCallDTO.attributes`:

```python
import spacetimepy


def capture_input(context: spacetimepy.CaptureStartContext):
    return {"input": context.locals["value"]}


def capture_result(context: spacetimepy.CaptureReturnContext):
    return {"result": context.return_value}


@spacetimepy.function(
    start_hooks=[capture_input],
    return_hooks=[capture_result],
)
def calculate(value):
    return value + 1
```

Hooks run in list order and later values replace earlier values with the same
key. A failing hook does not interrupt the captured program; its error is
stored in the call's `start_hook_errors` or `return_hook_errors` attribute and
the remaining hooks still run.

## External interactions

External interactions are declared independently from the function that uses
them. SpaceTimePy automatically installs its standard-library catalogue when a
runtime opens; `random.randint`, for example, needs no application declaration.

Application functions that perform input, output, or another side effect use
the import-time decorator:

```python
import spacetimepy


@spacetimepy.external
def read_input():
    return input("Value: ")


@spacetimepy.function
def calculate():
    return int(read_input()) + 1
```

The external call is recorded only when it occurs below an active captured
function. It is attached to that function's current step automatically, even
when unmonitored helper functions occur between them in the Python stack.

SpaceTimePy's maintained definitions live in the
`spacetimepy.interface.standard_external_interactions` directory, with one
descriptor file per standard module. Descriptors contain module and attribute
names only: they do not import the modules they describe. When a configured
module finishes loading, its functions are registered on the active runtime;
modules loaded before `SpaceTime.open()` are synchronized when it opens.

The current monitoring backend can register Python functions with code objects
directly. A C-backed operation can be exposed through a small decorated Python
function such as `read_input` above.

Replay integrations can replace an external function with
`context.external.mock(target)` to return its recorded outcome without calling
the target. `context.external.active(target)` calls the real target with
capture temporarily suppressed, then returns or raises the recorded outcome.
If the real call raises, that exception propagates and the recorded interaction
remains unconsumed. Integrations remain responsible for installing the returned
replacement callable.

## Custom picklers

Import application or integration-owned custom-pickler providers and pass them
when opening the runtime:

```python
from spacetimepy import SpaceTime
from my_integration import custom_pickler


space = SpaceTime.open(
    "trace.db",
    custom_picklers=[custom_pickler],
)
```

A provider is an imported module, class, or object exposing a dispatch table:

```python
def reduce_custom_value(value):
    return CustomValue, (value.data,)


def get_dispatch_table():
    return {CustomValue: reduce_custom_value}
```

A mapping such as `{CustomValue: reduce_custom_value}` can also be passed
directly. Providers are applied in list order, with later reducers overriding
earlier reducers for the same type. The same provider modules must remain
importable when stored values are later read or replayed.
