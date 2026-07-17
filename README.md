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
