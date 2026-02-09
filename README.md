# SpaceTimePy

- requirements and manual things : for gumtree we need java and isntallation script

## Live Game Explorer

SpaceTimePy includes a Live Game Explorer tool for replaying and debugging pygame executions. Two UI frameworks are available:

- **Tkinter** (original): `live-game-explorer` - Uses Tkinter with sv-ttk theming
- **Qt** (new): `live-game-explorer-qt` - Uses PyQt5 for a modern cross-platform UI

See [docs/QT_INTERFACE.md](docs/QT_INTERFACE.md) for more information about the Qt interface.

### Installation

For Tkinter version:
```bash
pip install .[game]
```

For Qt version:
```bash
pip install .[game-qt]
```