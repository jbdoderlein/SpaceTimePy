# SpaceTimePy v2 tests

The active suite is organized by architectural responsibility:

- `core/`: persistence-model invariants and the low-level VM recorder;
- `interface/`: capture declarations, DTO access, replay, and runtime lifecycle;
- `integration/`: complete user-facing workflows across those layers;
- `old/`: archived v1 tests, excluded from active discovery.

Run the dependency-free suite with:

```shell
python -m tests
```

The equivalent explicit discovery command is:

```shell
python -m unittest discover -s tests -t . -v
```

Pytest can also discover the same tests when installed. Its configuration in
`pyproject.toml` excludes `tests/old`.
