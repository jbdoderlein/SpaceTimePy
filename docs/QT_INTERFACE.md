# Qt Interface for Live Game Explorer

## Overview

A Qt-based interface for the Live Game Explorer has been added as an alternative to the Tkinter interface. This provides a more modern, cross-platform UI using PyQt5.

## Installation

To use the Qt interface, install the additional dependencies:

```bash
pip install .[game-qt]
```

Or install PyQt5 separately:

```bash
pip install PyQt5
```

## Usage

The Qt interface can be launched using the `live-game-explorer-qt` command:

```bash
live-game-explorer-qt path/to/database.db
```

### Command-line Options

All the same options from the Tkinter version are supported:

```bash
live-game-explorer-qt path/to/database.db \
    --function display_game \
    --image-key image \
    --title "My Game Explorer" \
    --geometry 1400x1200 \
    --scale 0.8
```

Options:
- `--function`, `-f`: Name of the function to track for replay (default: display_game)
- `--image-key`, `-i`: Metadata key for image data (default: image)
- `--title`, `-t`: Window title (default: "Game Explorer - Multi-Branch (Qt)")
- `--geometry`, `-g`: Window geometry in WIDTHxHEIGHT format (default: 1400x1200)
- `--scale`, `-s`: Image scale factor (default: 0.8)

## Features

The Qt interface provides the same core functionality as the Tkinter version:

- **Multi-session replay**: View and navigate multiple game execution sessions
- **Variable inspection**: Tree view of local and global variables
- **Code editor**: View source code with syntax highlighting (when chlorophyll is available)
- **Image display**: View game screen captures from each function call
- **Range selection**: Custom two-handle range selector for subsequence replay
- **Session controls**: Replay entire sessions, from current position, or subsequences

## Differences from Tkinter Version

The Qt version has a few differences:

1. **UI Framework**: Uses PyQt5 instead of Tkinter
2. **Theming**: Uses native Qt theming instead of sv-ttk
3. **Platform Support**: Better cross-platform support with Qt
4. **Code Editor**: Currently uses QTextEdit instead of chlorophyll (which is Tkinter-specific)

## Architecture

The Qt implementation is in `src/spacetimepy/interface/gameexplorer/livegameexplorer_qt.py` and includes:

- `TwoHandleRangeQt`: Custom Qt widget for dual-handle range selection
- `GameExplorerQt`: Main window class inheriting from QMainWindow

## Development

The Qt interface was designed to be a drop-in replacement for the Tkinter version, maintaining the same command-line interface and core functionality while providing a modern Qt-based UI.

## Example

```python
from spacetimepy.interface.gameexplorer.livegameexplorer_qt import GameExplorerQt
from PyQt5.QtWidgets import QApplication
import sys

app = QApplication(sys.argv)
explorer = GameExplorerQt(
    db_path="game_data.db",
    tracked_function="display_game",
    window_title="My Game"
)
explorer.run()
sys.exit(app.exec_())
```
