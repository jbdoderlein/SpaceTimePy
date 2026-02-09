#!/usr/bin/env python3
"""
Game Explorer - Multi-Branch Replay Tool (Qt Version)

A Qt-based tool to replay pygame game execution with multiple sliders
for different branches and proper tracked function display.

This is a Qt port of the original Tkinter-based livegameexplorer.py
"""
import base64
import contextlib
import datetime
import io
import os
import sys
from typing import Any, TypedDict

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QSlider, QVBoxLayout,
    QHBoxLayout, QSplitter, QTreeWidget, QTreeWidgetItem, QFrame,
    QScrollArea, QCheckBox, QPushButton, QSpinBox, QGroupBox, QTextEdit,
    QMessageBox
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QPointF, QRectF
from PyQt5.QtGui import QPixmap, QImage, QPainter, QPen, QBrush, QColor, QFont

from PIL import Image

from spacetimepy.core import FunctionCall, MonitoringSession, ObjectManager, init_db, FunctionCallRepository
from spacetimepy.core.monitoring import init_monitoring
from spacetimepy.core.reanimation import replay_session_sequence, replay_session_subsequence
from spacetimepy.core.session import end_session, start_session

# Import chlorophyll for code editor
try:
    from chlorophyll import CodeView
    CHLOROPHYLL_AVAILABLE = True
except ImportError:
    CHLOROPHYLL_AVAILABLE = False
    print("Warning: chlorophyll not available. Code editor will be disabled.")

import pygame

HIDDEN_PYGAME = False
original_set_mode = pygame.display.set_mode


def modified_set_mode(*args, **kwargs):
    if 'flags' not in kwargs and HIDDEN_PYGAME:
        kwargs['flags'] = pygame.HIDDEN
    return original_set_mode(*args, **kwargs)


pygame.display.set_mode = modified_set_mode


class TwoHandleRangeQt(QWidget):
    """A two-handle range selector widget for Qt.
    
    Emits value_changed(handle_name: 'start'|'end', value:int) when a handle moves.
    """
    value_changed = pyqtSignal(str, int)  # handle_name, value
    
    def __init__(self, parent=None, min_val: int = 0, max_val: int = 100,
                 start: int = 0, end: int = 100, width: int = 400,
                 height: int = 36, handle_radius: int = 6):
        super().__init__(parent)
        self.min_val = min_val
        self.max_val = max_val
        self.start = max(min_val, min(start, max_val))
        self.end = max(min_val, min(end, max_val))
        if self.start > self.end:
            self.start, self.end = self.end, self.start
        
        self.widget_width = width
        self.widget_height = height
        self.handle_radius = handle_radius
        self._dragging = None  # 'start' or 'end' or None
        self._pad_x = 10
        self._track_y = height // 2
        
        self.setMinimumSize(width, height)
        self.setMaximumHeight(height)
    
    def _val_to_x(self, val: int) -> int:
        span = max(1, self.max_val - self.min_val)
        frac = (val - self.min_val) / span
        left = self._pad_x
        right = self.widget_width - self._pad_x
        return int(left + frac * (right - left))
    
    def _x_to_val(self, x: int) -> int:
        left = self._pad_x
        right = self.widget_width - self._pad_x
        frac = (x - left) / max(1, (right - left))
        val = int(round(self.min_val + frac * (self.max_val - self.min_val)))
        return max(self.min_val, min(self.max_val, val))
    
    def _pick_handle(self, x, y):
        sx = self._val_to_x(self.start)
        ex = self._val_to_x(self.end)
        ds = abs(x - sx)
        de = abs(x - ex)
        return 'start' if ds <= de else 'end'
    
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            which = self._pick_handle(event.x(), event.y())
            self._dragging = which
            self._move_handle_to(which, event.x())
    
    def mouseMoveEvent(self, event):
        if self._dragging and event.buttons() & Qt.LeftButton:
            self._move_handle_to(self._dragging, event.x())
    
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = None
    
    def _move_handle_to(self, which: str, x: int):
        val = self._x_to_val(x)
        if which == 'start':
            if val > self.end:
                val = self.end
            if val != self.start:
                self.start = val
                self.update()
                with contextlib.suppress(Exception):
                    self.value_changed.emit('start', self.start)
        else:
            if val < self.start:
                val = self.start
            if val != self.end:
                self.end = val
                self.update()
                with contextlib.suppress(Exception):
                    self.value_changed.emit('end', self.end)
    
    def set_values(self, start: int, end: int):
        start = max(self.min_val, min(start, self.max_val))
        end = max(self.min_val, min(end, self.max_val))
        if start > end:
            start, end = end, start
        self.start = start
        self.end = end
        self.update()
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        left = self._pad_x
        right = self.widget_width - self._pad_x
        y = self._track_y
        
        # Draw track background
        pen = QPen(QColor('#d0d0d0'), 8, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(left, y, right, y)
        
        # Draw selected range
        sx = self._val_to_x(self.start)
        ex = self._val_to_x(self.end)
        pen = QPen(QColor('#4a90e2'), 8, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(sx, y, ex, y)
        
        # Draw handles
        r = self.handle_radius
        painter.setPen(QPen(QColor('#666666'), 1))
        painter.setBrush(QBrush(QColor('#ffffff')))
        painter.drawEllipse(QPointF(sx, y), r, r)
        painter.drawEllipse(QPointF(ex, y), r, r)


class SessionData(TypedDict):
    session_id: int
    calls: list[Any]
    

class GameExplorerQt(QMainWindow):
    """Qt-based Game Explorer for replaying pygame executions"""
    
    def __init__(self, db_path: str, tracked_function: str = 'display_game',
                 image_metadata_key: str = 'image', window_title: str = "Game Explorer - Multi-Branch (Qt)",
                 window_geometry: tuple = (1400, 1200), image_scale: float = 0.8):
        super().__init__()
        
        self.db_path = db_path
        self.tracked_function = tracked_function
        self.image_metadata_key = image_metadata_key
        self.window_title = window_title
        self.window_geometry = window_geometry
        self.image_scale = image_scale
        
        self.session = None
        self.object_manager = None
        self.sessions_data: dict[int, SessionData] = {}
        self.current_session_id = None
        self.current_call_index = 0
        
        # UI components
        self.image_label = None
        self.sliders_frame = None
        self.info_label = None
        self.variables_tree = None
        self.globals_content = None
        self.tracked_content = None
        self.status_label = None
        
        # Code editor
        self.code_editor = None
        self.code_editor_frame = None
        self.current_source_file = None
        self.current_highlighted_line = None
        self.file_modified = False
        self.save_button = None
        
        # Checkbox variables
        self.global_vars = {}
        self.tracked_vars = {}
        
        # Slider widgets for each session
        self.session_sliders = {}
        
        # Tree state management
        self.expanded_items = set()
        
        # Branching and comparison overlay features
        self.session_relationships = {}
        self.comparison_session_id = None
        self.comparison_checkboxes = {}
        
        # Stroboscopic effect features
        self.stroboscopic_session_id = None
        self.stroboscopic_checkboxes = {}
        self.stroboscopic_control_panels = {}
        
        # Stroboscopic settings
        self.stroboscopic_ghost_count = {}
        self.stroboscopic_offset = {}
        self.stroboscopic_start_position = {}
        
        # Range selection per session
        self.range_start = {}
        self.range_end = {}
        self.session_range_widgets = {}
        
        self.in_memory_db = True
        
        # Initialize database
        self._init_database()
    
    def _init_database(self):
        """Initialize database connection and load sessions"""
        try:
            init_db(self.db_path, reset=False, in_memory=self.in_memory_db)
            self.session = MonitoringSession(ObjectManager())
            
            # Load all sessions
            repo = FunctionCallRepository(self.session)
            
            # Get unique session IDs
            all_calls = repo.get_all()
            session_ids = sorted(set(call.session_id for call in all_calls))
            
            # Load calls for each session
            for session_id in session_ids:
                calls = repo.get_by_session(session_id)
                # Filter by tracked function
                tracked_calls = [c for c in calls if c.function_name == self.tracked_function]
                if tracked_calls:
                    self.sessions_data[session_id] = SessionData(
                        session_id=session_id,
                        calls=tracked_calls
                    )
            
            if not self.sessions_data:
                print(f"No tracked function calls found for '{self.tracked_function}'")
                return
            
            # Analyze relationships between sessions
            self._analyze_session_relationships()
            
            # Set initial session
            self.current_session_id = min(self.sessions_data.keys())
            self.current_call_index = 0
            
        except Exception as e:
            print(f"Error initializing database: {e}")
            import traceback
            traceback.print_exc()
    
    def _analyze_session_relationships(self):
        """Analyze parent-child relationships between sessions"""
        for session_id, session_data in self.sessions_data.items():
            self.session_relationships[session_id] = {
                'parent_session_id': None,
                'branch_point_call_id': None,
                'branch_point_index': None,
                'child_sessions': []
            }
            
            # Check if any call in this session has a parent_call_id from another session
            for call in session_data['calls']:
                if call.parent_call_id:
                    # Find which session contains this parent_call_id
                    for other_sid, other_data in self.sessions_data.items():
                        if other_sid == session_id:
                            continue
                        for idx, other_call in enumerate(other_data['calls']):
                            if other_call.id == call.parent_call_id:
                                self.session_relationships[session_id]['parent_session_id'] = other_sid
                                self.session_relationships[session_id]['branch_point_call_id'] = call.parent_call_id
                                self.session_relationships[session_id]['branch_point_index'] = idx
                                self.session_relationships[other_sid]['child_sessions'].append(session_id)
                                break
    
    def _create_ui(self):
        """Create the main user interface"""
        self.setWindowTitle(self.window_title)
        self.resize(*self.window_geometry)
        
        # Main widget and layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # Main splitter - vertical split
        main_splitter = QSplitter(Qt.Vertical)
        main_layout.addWidget(main_splitter)
        
        # Top splitter - horizontal split for image and variables
        top_splitter = QSplitter(Qt.Horizontal)
        main_splitter.addWidget(top_splitter)
        
        # Left side - Image frame
        image_frame = QGroupBox("Game Screen")
        image_layout = QVBoxLayout(image_frame)
        self.image_label = QLabel("Loading...")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(400, 300)
        image_layout.addWidget(self.image_label)
        top_splitter.addWidget(image_frame)
        
        # Right side - vertical splitter for variables, tracked functions, and code editor
        right_splitter = QSplitter(Qt.Vertical)
        top_splitter.addWidget(right_splitter)
        
        # Variables tree frame
        info_frame = QGroupBox("Variables (Locals & Globals)")
        info_layout = QVBoxLayout(info_frame)
        
        self.variables_tree = QTreeWidget()
        self.variables_tree.setHeaderLabels(['Variable', 'Current Value', 'Previous Value'])
        self.variables_tree.setColumnWidth(0, 200)
        self.variables_tree.setColumnWidth(1, 250)
        self.variables_tree.setColumnWidth(2, 250)
        info_layout.addWidget(self.variables_tree)
        
        self.info_label = QLabel("Loading...")
        self.info_label.setFont(QFont("Courier", 9))
        info_layout.addWidget(self.info_label)
        
        right_splitter.addWidget(info_frame)
        
        # Tracked Functions frame
        tracked_frame = QGroupBox("Tracked Functions")
        tracked_layout = QVBoxLayout(tracked_frame)
        
        tracked_scroll = QScrollArea()
        tracked_scroll.setWidgetResizable(True)
        tracked_scroll.setMaximumHeight(100)
        self.tracked_content = QWidget()
        self.tracked_content_layout = QVBoxLayout(self.tracked_content)
        tracked_scroll.setWidget(self.tracked_content)
        tracked_layout.addWidget(tracked_scroll)
        
        right_splitter.addWidget(tracked_frame)
        
        # Code Editor frame
        code_editor_frame = QGroupBox("Code Editor")
        code_editor_layout = QVBoxLayout(code_editor_frame)
        
        # Save button
        editor_controls = QWidget()
        editor_controls_layout = QHBoxLayout(editor_controls)
        editor_controls_layout.setContentsMargins(0, 0, 0, 0)
        
        self.save_button = QPushButton("Save (Ctrl+S)")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save_current_file)
        editor_controls_layout.addWidget(self.save_button)
        editor_controls_layout.addStretch()
        
        code_editor_layout.addWidget(editor_controls)
        
        # Code editor (placeholder - chlorophyll is tkinter-based)
        self.code_editor = QTextEdit()
        self.code_editor.setReadOnly(True)
        self.code_editor.setFont(QFont("Courier", 10))
        code_editor_layout.addWidget(self.code_editor)
        
        right_splitter.addWidget(code_editor_frame)
        
        # Set splitter proportions
        top_splitter.setSizes([400, 800])
        right_splitter.setSizes([400, 100, 200])
        
        # Bottom section - Sessions and sliders
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        
        # Status label
        self.status_label = QLabel("Ready")
        bottom_layout.addWidget(self.status_label)
        
        # Scrollable area for session sliders
        sliders_scroll = QScrollArea()
        sliders_scroll.setWidgetResizable(True)
        sliders_scroll.setMinimumHeight(150)
        
        self.sliders_frame = QWidget()
        self.sliders_layout = QVBoxLayout(self.sliders_frame)
        sliders_scroll.setWidget(self.sliders_frame)
        
        bottom_layout.addWidget(sliders_scroll)
        
        main_splitter.addWidget(bottom_widget)
        main_splitter.setSizes([700, 300])
        
        # Create session sliders
        self._create_session_sliders()
        
        # Update display
        self._update_display()
    
    def _create_session_sliders(self):
        """Create slider controls for each session"""
        for session_id in sorted(self.sessions_data.keys()):
            session_data = self.sessions_data[session_id]
            calls = session_data['calls']
            
            if not calls:
                continue
            
            # Create session frame
            session_frame = QGroupBox(f"Session {session_id} ({len(calls)} calls)")
            session_layout = QVBoxLayout(session_frame)
            
            # Slider
            slider = QSlider(Qt.Horizontal)
            slider.setMinimum(0)
            slider.setMaximum(len(calls) - 1)
            slider.setValue(0)
            slider.setTickPosition(QSlider.TicksBelow)
            slider.setTickInterval(max(1, len(calls) // 10))
            slider.valueChanged.connect(lambda val, sid=session_id: self._on_slider_change(sid, val))
            
            session_layout.addWidget(slider)
            
            # Range selector
            range_widget = TwoHandleRangeQt(
                min_val=0,
                max_val=len(calls) - 1,
                start=0,
                end=len(calls) - 1,
                width=400
            )
            range_widget.value_changed.connect(
                lambda handle, val, sid=session_id: self._on_range_changed(sid, handle, val)
            )
            session_layout.addWidget(range_widget)
            
            # Control buttons
            controls = QWidget()
            controls_layout = QHBoxLayout(controls)
            controls_layout.setContentsMargins(0, 0, 0, 0)
            
            replay_btn = QPushButton("Replay All")
            replay_btn.clicked.connect(lambda _, sid=session_id: self._replay_all(sid))
            controls_layout.addWidget(replay_btn)
            
            replay_from_btn = QPushButton("Replay From Here")
            replay_from_btn.clicked.connect(lambda _, sid=session_id: self._replay_from_here(sid))
            controls_layout.addWidget(replay_from_btn)
            
            replay_sub_btn = QPushButton("Replay Subsequence")
            replay_sub_btn.clicked.connect(lambda _, sid=session_id: self._replay_subsequence(sid))
            controls_layout.addWidget(replay_sub_btn)
            
            controls_layout.addStretch()
            
            session_layout.addWidget(controls)
            
            self.sliders_layout.addWidget(session_frame)
            
            # Store references
            self.session_sliders[session_id] = {
                'slider': slider,
                'range_widget': range_widget,
                'frame': session_frame
            }
            
            # Initialize range variables
            self.range_start[session_id] = 0
            self.range_end[session_id] = len(calls) - 1
    
    def _on_slider_change(self, session_id: int, value: int):
        """Handle slider position change"""
        self.current_session_id = session_id
        self.current_call_index = value
        self._update_display()
    
    def _on_range_changed(self, session_id: int, handle: str, value: int):
        """Handle range selector change"""
        if handle == 'start':
            self.range_start[session_id] = value
        else:
            self.range_end[session_id] = value
    
    def _update_display(self):
        """Update all display elements"""
        if not self.current_session_id or self.current_session_id not in self.sessions_data:
            return
        
        session_data = self.sessions_data[self.current_session_id]
        calls = session_data['calls']
        
        if self.current_call_index >= len(calls):
            return
        
        current_call = calls[self.current_call_index]
        call_data = self._get_call_data(current_call)
        
        # Update image
        if call_data and self.image_metadata_key in call_data.get('metadata', {}):
            image = self._decode_image(call_data['metadata'][self.image_metadata_key])
            if image:
                self._display_image(image)
        
        # Update info label
        info_text = f"Session: {self.current_session_id} | Call: {self.current_call_index + 1}/{len(calls)}\n"
        info_text += f"Function: {current_call.function_name}\n"
        if current_call.source_file:
            info_text += f"File: {current_call.source_file}:{current_call.source_line}"
        self.info_label.setText(info_text)
        
        # Update variables tree
        self._update_variables_display(call_data)
        
        # Update status
        self.status_label.setText(f"Viewing call {self.current_call_index + 1} of {len(calls)}")
    
    def _get_call_data(self, call: FunctionCall) -> dict:
        """Get call data including variables and metadata"""
        try:
            return {
                'locals': call.locals_data or {},
                'globals': call.globals_data or {},
                'metadata': call.metadata or {},
                'return_value': call.return_value
            }
        except Exception as e:
            print(f"Error getting call data: {e}")
            return {}
    
    def _decode_image(self, image_data: str) -> Image.Image:
        """Decode base64 image data"""
        try:
            if isinstance(image_data, str):
                image_bytes = base64.b64decode(image_data)
                return Image.open(io.BytesIO(image_bytes))
        except Exception as e:
            print(f"Error decoding image: {e}")
        return None
    
    def _display_image(self, pil_image: Image.Image):
        """Display PIL image in the label"""
        try:
            # Scale image
            width = int(pil_image.width * self.image_scale)
            height = int(pil_image.height * self.image_scale)
            pil_image = pil_image.resize((width, height), Image.Resampling.LANCZOS)
            
            # Convert to QPixmap
            img_byte_arr = io.BytesIO()
            pil_image.save(img_byte_arr, format='PNG')
            img_byte_arr = img_byte_arr.getvalue()
            
            qimage = QImage()
            qimage.loadFromData(img_byte_arr)
            pixmap = QPixmap.fromImage(qimage)
            
            self.image_label.setPixmap(pixmap)
        except Exception as e:
            print(f"Error displaying image: {e}")
    
    def _update_variables_display(self, call_data: dict):
        """Update the variables tree widget"""
        self.variables_tree.clear()
        
        if not call_data:
            return
        
        # Add locals
        if call_data.get('locals'):
            locals_root = QTreeWidgetItem(self.variables_tree, ['Locals', '', ''])
            locals_root.setExpanded(True)
            for key, value in call_data['locals'].items():
                item = QTreeWidgetItem(locals_root, [str(key), str(value), ''])
        
        # Add globals (filtered)
        if call_data.get('globals'):
            globals_root = QTreeWidgetItem(self.variables_tree, ['Globals', '', ''])
            for key, value in call_data['globals'].items():
                if not key.startswith('__'):
                    item = QTreeWidgetItem(globals_root, [str(key), str(value), ''])
    
    def _replay_all(self, session_id: int):
        """Replay entire session"""
        try:
            session_data = self.sessions_data.get(session_id)
            if not session_data:
                return
            
            self.status_label.setText(f"Replaying session {session_id}...")
            QApplication.processEvents()
            
            # Replay using core functionality
            replay_session_sequence(session_id, self.db_path)
            
            self.status_label.setText(f"Replay of session {session_id} complete")
        except Exception as e:
            self.status_label.setText(f"Error replaying: {e}")
            print(f"Error replaying session: {e}")
            import traceback
            traceback.print_exc()
    
    def _replay_from_here(self, session_id: int):
        """Replay from current position"""
        try:
            if session_id not in self.session_sliders:
                return
            
            slider = self.session_sliders[session_id]['slider']
            start_index = slider.value()
            
            session_data = self.sessions_data.get(session_id)
            if not session_data:
                return
            
            calls = session_data['calls']
            if start_index >= len(calls):
                return
            
            self.status_label.setText(f"Replaying from call {start_index}...")
            QApplication.processEvents()
            
            # Get start call ID
            start_call_id = calls[start_index].id
            
            # Replay subsequence
            replay_session_subsequence(session_id, start_call_id, None, self.db_path)
            
            self.status_label.setText(f"Replay complete")
        except Exception as e:
            self.status_label.setText(f"Error replaying: {e}")
            print(f"Error replaying from here: {e}")
            import traceback
            traceback.print_exc()
    
    def _replay_subsequence(self, session_id: int):
        """Replay selected subsequence"""
        try:
            start_idx = self.range_start.get(session_id, 0)
            end_idx = self.range_end.get(session_id, 0)
            
            session_data = self.sessions_data.get(session_id)
            if not session_data:
                return
            
            calls = session_data['calls']
            if start_idx >= len(calls) or end_idx >= len(calls):
                return
            
            self.status_label.setText(f"Replaying calls {start_idx} to {end_idx}...")
            QApplication.processEvents()
            
            start_call_id = calls[start_idx].id
            end_call_id = calls[end_idx].id
            
            replay_session_subsequence(session_id, start_call_id, end_call_id, self.db_path)
            
            self.status_label.setText(f"Replay complete")
        except Exception as e:
            self.status_label.setText(f"Error replaying: {e}")
            print(f"Error replaying subsequence: {e}")
            import traceback
            traceback.print_exc()
    
    def _save_current_file(self):
        """Save current file (placeholder)"""
        QMessageBox.information(self, "Save", "Save functionality not yet implemented in Qt version")
    
    def run(self):
        """Run the application"""
        self._create_ui()
        self.show()


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Game Explorer - Multi-Branch Replay Tool (Qt)")
    parser.add_argument("db_path", help="Path to the game database file")
    parser.add_argument("--function", "-f", default="display_game",
                       help="Name of the function to track for replay (default: display_game)")
    parser.add_argument("--image-key", "-i", default="image",
                       help="Metadata key for image data (default: image)")
    parser.add_argument("--title", "-t", default="Game Explorer - Multi-Branch (Qt)",
                       help="Window title (default: Game Explorer - Multi-Branch (Qt))")
    parser.add_argument("--geometry", "-g", default="1400x1200",
                       help="Window geometry (default: 1400x1200)")
    parser.add_argument("--scale", "-s", type=float, default=0.8,
                       help="Image scale factor (default: 0.8)")
    args = parser.parse_args()
    
    # Parse geometry
    if 'x' in args.geometry:
        width, height = map(int, args.geometry.split('x'))
        geometry = (width, height)
    else:
        geometry = (1400, 1200)
    
    # Create Qt application
    app = QApplication(sys.argv)
    
    # Create and run the game explorer
    explorer = GameExplorerQt(
        db_path=args.db_path,
        tracked_function=args.function,
        image_metadata_key=args.image_key,
        window_title=args.title,
        window_geometry=geometry,
        image_scale=args.scale
    )
    explorer.run()
    
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
