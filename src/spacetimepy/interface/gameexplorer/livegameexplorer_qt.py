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
    QMessageBox, QSizePolicy
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QPointF, QRectF, QEvent
from PyQt5.QtGui import (
    QPixmap, QImage, QPainter, QPen, QBrush, QColor, QFont, QTextCursor, 
    QTextFormat
)

from PIL import Image

# Import Pygments for syntax highlighting
try:
    from pygments import highlight
    from pygments.lexers import PythonLexer
    from pygments.formatters import HtmlFormatter
    PYGMENTS_AVAILABLE = True
except ImportError:
    PYGMENTS_AVAILABLE = False
    print("Warning: pygments not available. Syntax highlighting will be disabled.")

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
        self.sessions_data: dict[int, dict] = {}  # Dict[session_id, session_data]
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

        # Image scaling
        self.current_pixmap = None
        
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
        if not os.path.exists(self.db_path):
            print(f"Error: Database file not found at {self.db_path}")
            sys.exit(1)
        
        try:
            # Initialize database session
            Session = init_db(self.db_path)
            self.session = Session()
            self.object_manager = ObjectManager(self.session)
            
            # Get all monitoring sessions
            sessions = self.session.query(MonitoringSession).order_by(MonitoringSession.start_time).all()
            
            if not sessions:
                print("Error: No monitoring sessions found in database")
                sys.exit(1)
            
            # Load tracked function calls for each session
            for session in sessions:
                tracked_calls = self.session.query(FunctionCall).filter(
                    FunctionCall.session_id == session.id,
                    FunctionCall.function == self.tracked_function
                ).order_by(FunctionCall.order_in_session).all()
                
                if tracked_calls:
                    self.sessions_data[session.id] = {
                        'session': session,
                        'calls': tracked_calls,
                        'name': session.name or f"Session {session.id}",
                        'start_time': session.start_time
                    }
            
            if not self.sessions_data:
                print(f"No tracked function calls found for '{self.tracked_function}'")
                sys.exit(1)
            
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
    
    def _get_sorted_sessions_for_display(self) -> list[int]:
        """Sort sessions to display parent sessions before child sessions"""
        sorted_sessions = []
        processed = set()

        # First add all main sessions (no parent)
        for session_id, rel in self.session_relationships.items():
            if not rel.get('parent_session_id'):
                sorted_sessions.append(session_id)
                processed.add(session_id)

        # Then add child sessions in order
        while len(processed) < len(self.sessions_data):
            added_in_iteration = False
            for session_id, rel in self.session_relationships.items():
                if session_id not in processed:
                    parent_id = rel.get('parent_session_id')
                    if parent_id is None or parent_id in processed:
                        sorted_sessions.append(session_id)
                        processed.add(session_id)
                        added_in_iteration = True

            if not added_in_iteration:
                # Add any remaining sessions to avoid infinite loop
                for session_id in self.sessions_data:
                    if session_id not in processed:
                        sorted_sessions.append(session_id)
                        processed.add(session_id)
                break

        return sorted_sessions
    
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
        self.image_label.installEventFilter(self)
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
        tracked_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.tracked_content = QWidget()
        self.tracked_content_layout = QVBoxLayout(self.tracked_content)
        tracked_scroll.setWidget(self.tracked_content)
        tracked_layout.addWidget(tracked_scroll)
        
        right_splitter.addWidget(tracked_frame)
        
        # Code Editor frame
        code_editor_frame = QGroupBox("Code Editor")
        self.code_editor_frame = code_editor_frame
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
        
        # Code editor with Pygments syntax highlighting
        self.code_editor = QTextEdit()
        self.code_editor.setReadOnly(True)
        
        # Use HTML rendering for syntax highlighting if Pygments is available
        if PYGMENTS_AVAILABLE:
            self.code_editor.setAcceptRichText(True)
        else:
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
        
        # General control buttons (not branch-specific)
        controls_frame = QWidget()
        controls_layout = QHBoxLayout(controls_frame)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        
        replay_all_btn = QPushButton("Replay All")
        replay_all_btn.clicked.connect(self._replay_all)
        controls_layout.addWidget(replay_all_btn)
        
        replay_from_btn = QPushButton("Replay From Here")
        replay_from_btn.clicked.connect(self._replay_from_here)
        controls_layout.addWidget(replay_from_btn)
        
        replay_range_btn = QPushButton("Replay Range")
        replay_range_btn.clicked.connect(self._replay_subsequence)
        controls_layout.addWidget(replay_range_btn)
        
        refresh_btn = QPushButton("Refresh DB")
        refresh_btn.clicked.connect(self._refresh_database)
        controls_layout.addWidget(refresh_btn)
        
        # Hidden pygame checkbox
        self.hidden_pygame_checkbox = QCheckBox("Hidden Pygame")
        self.hidden_pygame_checkbox.setChecked(HIDDEN_PYGAME)
        self.hidden_pygame_checkbox.stateChanged.connect(self._on_hidden_pygame_changed)
        controls_layout.addWidget(self.hidden_pygame_checkbox)
        
        controls_layout.addStretch()
        
        # Status label
        self.status_label = QLabel("Ready")
        controls_layout.addWidget(self.status_label)
        
        bottom_layout.addWidget(controls_frame)
        
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

        # Populate tracked functions
        self._setup_tracked_functions()
        
        # Update display
        self._update_display()
    
    def _create_session_sliders(self):
        """Create slider controls for each session with branching visualization"""
        # Sort sessions to show parent sessions before child sessions
        sorted_sessions = self._get_sorted_sessions_for_display()
        
        for session_id in sorted_sessions:
            session_data = self.sessions_data[session_id]
            calls = session_data['calls']
            
            if not calls:
                continue
            
            # Get relationship info
            rel = self.session_relationships.get(session_id, {})
            is_branch = rel.get('parent_session_id') is not None
            branch_point_index = rel.get('branch_point_index')
            
            # Create frame with indent for branches
            session_name = session_data.get('name', f"Session {session_id}")
            session_frame = QGroupBox(f"{session_name} ({len(calls)} calls)")
            session_layout = QVBoxLayout(session_frame)
            
            # Add margin for branches
            if is_branch:
                session_frame.setStyleSheet("QGroupBox { margin-left: 20px; }")
            
            # Session info with branch information
            info_text = ""
            if 'session' in session_data:
                session_info = session_data['session']
                info_text = f"Started: {session_info.start_time.strftime('%H:%M:%S')}"
                if session_info.description:
                    info_text += f" - {session_info.description}"
            
            if is_branch:
                parent_id = rel['parent_session_id']
                parent_name = self.sessions_data[parent_id].get('name', f"Session {parent_id}") if parent_id in self.sessions_data else f"Session {parent_id}"
                info_text += f" | Branches from {parent_name}"
                if branch_point_index is not None:
                    info_text += f" at frame {branch_point_index + 1}"
            
            if info_text:
                info_label = QLabel(info_text)
                info_label.setWordWrap(True)
                session_layout.addWidget(info_label)
            
            # Checkbox frame for Compare and Stroboscopic
            checkbox_frame = QWidget()
            checkbox_layout = QHBoxLayout(checkbox_frame)
            checkbox_layout.setContentsMargins(0, 0, 0, 0)
            
            # Compare checkbox
            if session_id not in self.comparison_checkboxes:
                self.comparison_checkboxes[session_id] = QCheckBox("Compare")
                self.comparison_checkboxes[session_id].stateChanged.connect(
                    lambda state, sid=session_id: self._on_comparison_selection_changed(sid)
                )
            checkbox_layout.addWidget(self.comparison_checkboxes[session_id])
            
            # Stroboscopic checkbox
            if session_id not in self.stroboscopic_checkboxes:
                self.stroboscopic_checkboxes[session_id] = QCheckBox("Stroboscopic")
                self.stroboscopic_checkboxes[session_id].stateChanged.connect(
                    lambda state, sid=session_id: self._on_stroboscopic_selection_changed(sid)
                )
            checkbox_layout.addWidget(self.stroboscopic_checkboxes[session_id])
            
            checkbox_layout.addStretch()
            session_layout.addWidget(checkbox_frame)
            
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

            range_label = QLabel(f"Range: 1 - {len(calls)}")
            session_layout.addWidget(range_label)
            
            self.sliders_layout.addWidget(session_frame)
            
            # Store references
            self.session_sliders[session_id] = {
                'slider': slider,
                'range_widget': range_widget,
                'range_label': range_label,
                'frame': session_frame
            }
            
            # Initialize range variables
            self.range_start[session_id] = 0
            self.range_end[session_id] = len(calls) - 1
    
    def _on_slider_change(self, session_id: int, value: int):
        """Handle slider position change"""
        self.current_session_id = session_id
        self.current_call_index = value
        
        # Sync parent and child sliders
        self._sync_parent_slider(session_id, value)
        self._sync_child_sliders(session_id, value)
        
        self._update_display()
    
    def _on_range_changed(self, session_id: int, handle: str, value: int):
        """Handle range selector change"""
        if handle == 'start':
            self.range_start[session_id] = value
            # Ensure start <= end
            if value > self.range_end[session_id]:
                self.range_end[session_id] = value
                # Update the range widget to reflect the change
                slider_info = self.session_sliders.get(session_id)
                if slider_info and 'range_widget' in slider_info:
                    range_widget = slider_info['range_widget']
                    range_widget.set_values(self.range_start[session_id], self.range_end[session_id])
        else:
            self.range_end[session_id] = value
            # Ensure end >= start
            if value < self.range_start[session_id]:
                self.range_start[session_id] = value
                # Update the range widget to reflect the change
                slider_info = self.session_sliders.get(session_id)
                if slider_info and 'range_widget' in slider_info:
                    range_widget = slider_info['range_widget']
                    range_widget.set_values(self.range_start[session_id], self.range_end[session_id])

        self._update_range_label(session_id)
        
        # Move main slider to the changed position and update display
        slider_info = self.session_sliders.get(session_id)
        if slider_info and 'slider' in slider_info:
            slider = slider_info['slider']
            slider.blockSignals(True)  # Prevent recursion
            slider.setValue(value)
            slider.blockSignals(False)
            
            # Update the display
            self.current_session_id = session_id
            self.current_call_index = value
            self._update_display()

    def _update_range_label(self, session_id: int):
        slider_info = self.session_sliders.get(session_id)
        if not slider_info:
            return

        start_idx = self.range_start.get(session_id, 0)
        end_idx = self.range_end.get(session_id, 0)
        label = slider_info.get('range_label')
        if label:
            label.setText(f"Range: {start_idx + 1} - {end_idx + 1}")
    
    def _sync_parent_slider(self, session_id: int, current_index: int):
        """Synchronize parent session slider when exploring a branch session"""
        if session_id not in self.session_relationships:
            return

        rel = self.session_relationships[session_id]
        parent_session_id = rel.get('parent_session_id')
        branch_point_index = rel.get('branch_point_index')

        if parent_session_id and branch_point_index is not None:
            # Calculate the corresponding position in the parent session
            parent_index = branch_point_index + current_index

            # Update the parent session slider if it exists
            if parent_session_id in self.session_sliders:
                parent_slider = self.session_sliders[parent_session_id]['slider']
                if parent_slider:
                    # Check if the parent index is within bounds
                    parent_calls_count = len(self.sessions_data[parent_session_id]['calls'])
                    if parent_index < parent_calls_count:
                        # Block signals to avoid recursion
                        parent_slider.blockSignals(True)
                        parent_slider.setValue(parent_index)
                        parent_slider.blockSignals(False)

    def _sync_child_sliders(self, session_id: int, current_index: int):
        """Synchronize child session sliders when exploring a parent session"""
        if session_id not in self.session_relationships:
            return

        rel = self.session_relationships[session_id]
        child_sessions = rel.get('child_sessions', [])

        for child_session_id in child_sessions:
            if child_session_id in self.session_relationships:
                child_rel = self.session_relationships[child_session_id]
                branch_point_index = child_rel.get('branch_point_index')

                if branch_point_index is not None and current_index >= branch_point_index:
                    # Calculate the corresponding position in the child session
                    child_index = current_index - branch_point_index

                    # Update the child session slider if it exists and the index is valid
                    if child_session_id in self.session_sliders:
                        child_slider = self.session_sliders[child_session_id]['slider']
                        if child_slider:
                            child_calls_count = len(self.sessions_data[child_session_id]['calls'])
                            if child_index < child_calls_count:
                                # Block signals to avoid recursion
                                child_slider.blockSignals(True)
                                child_slider.setValue(child_index)
                                child_slider.blockSignals(False)
    
    def _on_hidden_pygame_changed(self, state):
        """Handle changes to the hidden pygame checkbox"""
        global HIDDEN_PYGAME
        HIDDEN_PYGAME = bool(state)
        print(f"Hidden pygame mode: {'ON' if HIDDEN_PYGAME else 'OFF'}")
    
    def _on_comparison_selection_changed(self, session_id: int):
        """Handle comparison overlay selection change"""
        # First, uncheck all other checkboxes (only one can be selected at a time)
        for sid, checkbox in self.comparison_checkboxes.items():
            if sid != session_id:
                checkbox.blockSignals(True)
                checkbox.setChecked(False)
                checkbox.blockSignals(False)

        # Set the comparison session
        if self.comparison_checkboxes[session_id].isChecked():
            self.comparison_session_id = session_id
            print(f"Comparison mode enabled for session {session_id}")
        else:
            self.comparison_session_id = None
            print("Comparison mode disabled")

        # Update display to show comparison if enabled
        self._update_display()
    
    def _on_stroboscopic_selection_changed(self, session_id: int):
        """Handle stroboscopic effect selection change"""
        # First, uncheck all other checkboxes (only one can be selected at a time)
        for sid, checkbox in self.stroboscopic_checkboxes.items():
            if sid != session_id:
                checkbox.blockSignals(True)
                checkbox.setChecked(False)
                checkbox.blockSignals(False)

        # Set the stroboscopic session
        if self.stroboscopic_checkboxes[session_id].isChecked():
            self.stroboscopic_session_id = session_id
            print(f"Stroboscopic mode enabled for session {session_id}")
            # TODO: Add stroboscopic control panel
        else:
            self.stroboscopic_session_id = None
            print("Stroboscopic mode disabled")

        # Update display
        self._update_display()
    
    def _refresh_database(self):
        """Refresh database and reload sessions"""
        try:
            self.status_label.setText("Refreshing database...")
            QApplication.processEvents()
            
            # Close current session
            if self.session:
                self.session.close()
            
            # Reinitialize database
            self._init_database()
            
            # Clear and recreate UI
            # Clear existing sliders
            while self.sliders_layout.count():
                child = self.sliders_layout.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()
            
            self.session_sliders.clear()
            self.range_start.clear()
            self.range_end.clear()
            
            # Recreate session sliders
            self._create_session_sliders()
            
            # Update display
            self._update_display()
            
            self.status_label.setText("Database refreshed")
        except Exception as e:
            self.status_label.setText(f"Error refreshing: {e}")
            print(f"Error refreshing database: {e}")
            import traceback
            traceback.print_exc()
    
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
        info_text += f"Function: {current_call.function}\n"
        if current_call.file:
            info_text += f"File: {current_call.file}:{current_call.line}"
        self.info_label.setText(info_text)
        
        # Update variables tree
        self._update_variables_display(call_data)

        # Update code editor
        self._update_code_editor(current_call)
        
        # Update status
        self.status_label.setText(f"Viewing call {self.current_call_index + 1} of {len(calls)}")
    
    def _get_call_data(self, call: FunctionCall) -> dict:
        """Get call data including variables and metadata"""
        try:
            data = {
                'locals': {},
                'globals': {},
                'metadata': call.call_metadata or {},
                'return_value': None
            }

            # Load locals
            if call.locals_refs and self.object_manager:
                for var_name, ref in call.locals_refs.items():
                    if ref == "<unserializable>":
                        continue
                    try:
                        data['locals'][var_name] = self.object_manager.rehydrate(ref)
                    except Exception as e:
                        print(f"Error loading local variable {var_name}: {e}")
                        data['locals'][var_name] = f"Error loading: {e}"

            # Load globals
            if call.globals_refs and self.object_manager:
                for var_name, ref in call.globals_refs.items():
                    if ref == "<unserializable>":
                        continue
                    if not var_name.startswith('__'):
                        try:
                            data['globals'][var_name] = self.object_manager.rehydrate(ref)
                        except Exception as e:
                            print(f"Error loading global variable {var_name}: {e}")
                            data['globals'][var_name] = f"Error loading: {e}"

            if call.return_ref and self.object_manager:
                try:
                    data['return_value'] = self.object_manager.rehydrate(call.return_ref)
                except Exception as e:
                    print(f"Error loading return value: {e}")
                    data['return_value'] = f"Error loading: {e}"

            return data
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
            # Convert to QPixmap
            img_byte_arr = io.BytesIO()
            pil_image.save(img_byte_arr, format='PNG')
            img_byte_arr = img_byte_arr.getvalue()
            
            qimage = QImage()
            qimage.loadFromData(img_byte_arr)
            self.current_pixmap = QPixmap.fromImage(qimage)
            self._fit_image_to_label()
        except Exception as e:
            print(f"Error displaying image: {e}")
    
    def _update_variables_display(self, call_data: dict):
        """Update the variables tree widget"""
        self._snapshot_tree_expanded()
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

        self._restore_tree_expanded()

    def _snapshot_tree_expanded(self):
        if not self.variables_tree:
            return

        expanded = set()

        def walk(item, path_parts):
            path = path_parts + [item.text(0)]
            if item.isExpanded():
                expanded.add(" > ".join(path))
            for i in range(item.childCount()):
                walk(item.child(i), path)

        for i in range(self.variables_tree.topLevelItemCount()):
            walk(self.variables_tree.topLevelItem(i), [])

        self.expanded_items = expanded

    def _restore_tree_expanded(self):
        if not self.variables_tree or not self.expanded_items:
            return

        def walk(item, path_parts):
            path = path_parts + [item.text(0)]
            if " > ".join(path) in self.expanded_items:
                item.setExpanded(True)
            for i in range(item.childCount()):
                walk(item.child(i), path)

        for i in range(self.variables_tree.topLevelItemCount()):
            walk(self.variables_tree.topLevelItem(i), [])

    def _clear_layout(self, layout: QVBoxLayout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _setup_tracked_functions(self):
        """Populate tracked functions checkboxes from child calls"""
        if not self.sessions_data or self.session is None or not self.tracked_content:
            return

        self._clear_layout(self.tracked_content_layout)
        self.tracked_vars.clear()

        all_tracked_functions = set()
        for session_data in self.sessions_data.values():
            for call in session_data['calls']:
                child_calls = call.get_child_calls(self.session)
                for child_call in child_calls:
                    if child_call.function != self.tracked_function and child_call.function != "mock_func":
                        all_tracked_functions.add(child_call.function)

        for func_name in sorted(all_tracked_functions):
            cb = QCheckBox(func_name)
            cb.setChecked(True)
            self.tracked_vars[func_name] = cb
            self.tracked_content_layout.addWidget(cb)

        self.tracked_content_layout.addStretch()

    def _update_code_editor(self, call: FunctionCall):
        """Update the code editor with source code from the current function call"""
        if not self.code_editor:
            return

        file_path = call.file
        line_number = call.line

        if not file_path and call.code_definition_id and self.session:
            try:
                from spacetimepy.core.models import CodeDefinition
                code_def = self.session.query(CodeDefinition).filter(
                    CodeDefinition.id == call.code_definition_id
                ).first()

                if code_def:
                    module_path = code_def.module_path
                    if module_path and not module_path.endswith('.py'):
                        possible_paths = [
                            module_path + '.py',
                            module_path.replace('.', '/') + '.py',
                            module_path.replace('.', os.sep) + '.py'
                        ]
                        for path in possible_paths:
                            if os.path.exists(path):
                                file_path = path
                                break
                    else:
                        file_path = module_path

                    if code_def.first_line_no:
                        line_number = code_def.first_line_no
            except Exception as e:
                print(f"Error getting code definition: {e}")

        if file_path and file_path != self.current_source_file:
            self._load_source_code(file_path, line_number)
        elif file_path and line_number and line_number != self.current_highlighted_line:
            self._highlight_line(line_number)
        elif not file_path:
            self.code_editor.setPlainText("No source code available for this function call")
            self.current_source_file = None
            self.current_highlighted_line = None
            self._update_code_editor_title()

    def _load_source_code(self, file_path: str, highlight_line: int | None = None):
        """Load source code from file into the code editor and optionally highlight a line"""
        if not self.code_editor:
            return

        try:
            if file_path and os.path.exists(file_path):
                with open(file_path, encoding='utf-8') as f:
                    content = f.read()
                
                # Use Pygments for syntax highlighting if available
                if PYGMENTS_AVAILABLE:
                    # Generate HTML with syntax highlighting
                    formatter = HtmlFormatter(style='default', linenos=False, full=False)
                    highlighted_html = highlight(content, PythonLexer(), formatter)
                    
                    # Get CSS for styling
                    css = formatter.get_style_defs('.highlight')
                    
                    # Wrap in HTML with CSS
                    html = f"""
                    <style>
                    {css}
                    .highlight {{ font-family: Courier, monospace; font-size: 10pt; }}
                    </style>
                    <div class="highlight">{highlighted_html}</div>
                    """
                    
                    self.code_editor.setHtml(html)
                else:
                    self.code_editor.setPlainText(content)
                
                if highlight_line is not None:
                    self._highlight_line(highlight_line)
                self.current_source_file = file_path
            else:
                self.code_editor.setPlainText(f"Source file not found: {file_path or 'Unknown'}")
                self.current_source_file = None
            self.current_highlighted_line = None
            self.file_modified = False
            self._update_code_editor_title()
        except Exception as e:
            print(f"Error loading source code from {file_path}: {e}")
            self.code_editor.setPlainText(f"Error loading source code: {e}")
            self.current_source_file = None
            self.current_highlighted_line = None
            self.file_modified = False
            self._update_code_editor_title()

    def _highlight_line(self, line_number: int):
        """Highlight a specific line in the code editor"""
        if not self.code_editor:
            return

        try:
            cursor = self.code_editor.textCursor()
            cursor.movePosition(QTextCursor.Start)
            for _ in range(max(0, line_number - 1)):
                cursor.movePosition(QTextCursor.Down)
            cursor.select(QTextCursor.LineUnderCursor)

            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            selection.format.setBackground(QColor('#ffff88'))
            selection.format.setProperty(QTextFormat.FullWidthSelection, True)

            self.code_editor.setExtraSelections([selection])
            self.code_editor.setTextCursor(cursor)
            self.code_editor.ensureCursorVisible()
            self.current_highlighted_line = line_number
        except Exception as e:
            print(f"Error highlighting line {line_number}: {e}")

    def _update_code_editor_title(self):
        """Update the code editor frame title"""
        if not self.code_editor_frame:
            return

        if self.current_source_file:
            filename = os.path.basename(self.current_source_file)
            title = f"Code Editor - {filename}"
        else:
            title = "Code Editor"
        self.code_editor_frame.setTitle(title)

    def eventFilter(self, obj, event):
        if obj == self.image_label and event.type() == QEvent.Resize:
            self._fit_image_to_label()
        return super().eventFilter(obj, event)

    def _fit_image_to_label(self):
        if not self.current_pixmap or not self.image_label:
            return

        target_w = max(1, int(self.image_label.width() * self.image_scale))
        target_h = max(1, int(self.image_label.height() * self.image_scale))
        scaled = self.current_pixmap.scaled(
            target_w,
            target_h,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )
        self.image_label.setPixmap(scaled)
    
    def _replay_all(self):
        """Replay entire current session"""
        try:
            if not self.current_session_id:
                self.status_label.setText("No session selected")
                return
                
            session_id = self.current_session_id
            session_data = self.sessions_data.get(session_id)
            if not session_data:
                return

            calls = session_data['calls']
            if not calls:
                return

            first_call_id = calls[0].id
            mocked_functions = self._get_mocked_functions()
            
            self.status_label.setText(f"Replaying session {session_id}...")
            QApplication.processEvents()

            init_monitoring(db_path=self.db_path, custom_picklers=["pygame"])
            new_session_id = start_session(f"Replay of session {session_id}")
            if not new_session_id:
                self.status_label.setText("Failed to start session")
                return
            
            # Replay using core functionality
            replay_session_sequence(first_call_id, self.db_path, mock_functions=mocked_functions)
            
            # Close pygame screen after successful replay
            try:
                import pygame
                pygame.quit()
                print("Pygame screen closed after replay")
            except Exception as pygame_err:
                print(f"Warning: Could not close pygame screen: {pygame_err}")
            
            self.status_label.setText(f"Replay of session {session_id} complete")
            
            # Refresh the database to see new calls
            self._refresh_database()
        except Exception as e:
            self.status_label.setText(f"Error replaying: {e}")
            print(f"Error replaying session: {e}")
            import traceback
            traceback.print_exc()
        finally:
            with contextlib.suppress(Exception):
                end_session()
    
    def _replay_from_here(self):
        """Replay from current position"""
        try:
            if not self.current_session_id:
                self.status_label.setText("No session selected")
                return
                
            session_id = self.current_session_id
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
            mocked_functions = self._get_mocked_functions()
            
            # Replay subsequence
            init_monitoring(db_path=self.db_path, custom_picklers=["pygame"])
            new_session_id = start_session(f"Replay from session {session_id} call {start_index + 1}")
            if not new_session_id:
                self.status_label.setText("Failed to start session")
                return

            replay_session_sequence(start_call_id, self.db_path, mock_functions=mocked_functions)
            
            # Close pygame screen after successful replay
            try:
                import pygame
                pygame.quit()
                print("Pygame screen closed after replay")
            except Exception as pygame_err:
                print(f"Warning: Could not close pygame screen: {pygame_err}")
            
            self.status_label.setText(f"Replay complete")
            
            # Refresh the database
            self._refresh_database()
        except Exception as e:
            self.status_label.setText(f"Error replaying: {e}")
            print(f"Error replaying from here: {e}")
            import traceback
            traceback.print_exc()
        finally:
            with contextlib.suppress(Exception):
                end_session()
    
    def _replay_subsequence(self):
        """Replay selected subsequence"""
        try:
            if not self.current_session_id:
                self.status_label.setText("No session selected")
                return
                
            session_id = self.current_session_id
            start_idx = self.range_start.get(session_id, 0)
            end_idx = self.range_end.get(session_id, 0)
            
            session_data = self.sessions_data.get(session_id)
            if not session_data:
                return
            
            calls = session_data['calls']
            if start_idx >= len(calls) or end_idx >= len(calls):
                return

            if start_idx > end_idx:
                start_idx, end_idx = end_idx, start_idx
            
            self.status_label.setText(f"Replaying calls {start_idx} to {end_idx}...")
            QApplication.processEvents()
            
            start_call_id = calls[start_idx].id
            end_call_id = calls[end_idx].id

            mocked_functions = self._get_mocked_functions()

            init_monitoring(db_path=self.db_path, custom_picklers=["pygame"])
            new_session_id = start_session(
                f"Replay subsequence session {session_id} calls {start_idx + 1}-{end_idx + 1}"
            )
            if not new_session_id:
                self.status_label.setText("Failed to start session")
                return
            
            replay_session_subsequence(
                start_call_id,
                end_call_id,
                self.db_path,
                mock_functions=mocked_functions,
            )
            
            # Close pygame screen after successful replay
            try:
                import pygame
                pygame.quit()
                print("Pygame screen closed after replay")
            except Exception as pygame_err:
                print(f"Warning: Could not close pygame screen: {pygame_err}")
            
            self.status_label.setText(f"Replay complete")
            
            # Refresh the database
            self._refresh_database()
        except Exception as e:
            self.status_label.setText(f"Error replaying: {e}")
            print(f"Error replaying subsequence: {e}")
            import traceback
            traceback.print_exc()
        finally:
            with contextlib.suppress(Exception):
                end_session()

    def _get_mocked_functions(self) -> list[str]:
        """Get list of functions that should be mocked (checked)"""
        return [name for name, cb in self.tracked_vars.items() if cb.isChecked()]
    
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
