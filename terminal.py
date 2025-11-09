import json
import os
import signal
import pty
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import (
    QDir,
    QPoint,
    QSortFilterProxyModel,
    Qt,
    QThread,
    pyqtSignal,
    QEvent,
)
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFileSystemModel,
    QKeyEvent,
    QKeySequence,
    QTextCursor,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedLayout,
    QTabWidget,
    QTabBar,
    QTextEdit,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from dialogs.editor import EditorDialog
from dialogs.photo_viewer import PhotoViewerDialog
from dialogs.video_player import VideoPlayerDialog
from dialogs.zip_viewer import ZipViewerDialog


CONFIG_PATH = Path.home() / ".dirshell.json"
DEFAULT_FOREGROUND = "#f8f8f2"
DEFAULT_BACKGROUND = "#1e1e1e"
DEFAULT_APP_BACKGROUND = "#2b2b2b"
DEFAULT_APP_TEXT_COLOR = DEFAULT_FOREGROUND

OPEN_WINDOWS: list["TerminalApp"] = []


class TerminalReader(QThread):
    output = pyqtSignal(str)

    def __init__(self, fd: int):
        super().__init__()
        self.fd = fd
        self.running = True

    def run(self) -> None:
        while self.running:
            try:
                data = os.read(self.fd, 1024).decode(errors="ignore")
                self.output.emit(data)
            except OSError:
                break


class TerminalWidget(QTextEdit):
    """Interactive terminal widget for DirShell."""

    command_executed = pyqtSignal(str)

    def __init__(
        self,
        fd: int,
        foreground: str,
        background: str,
        welcome_message: str | None = None,
    ) -> None:
        super().__init__(None)  # prevent Qt from misreading fd as a QWidget parent
        self.fd = fd
        self._foreground = foreground
        self._background = background
        self.apply_colors(foreground, background)
        self.setReadOnly(False)
        self.setCursorWidth(8)
        self.setUndoRedoEnabled(False)

        if welcome_message:
            self.insertPlainText(welcome_message.rstrip("\n") + "\n")

        self._cursor = self.textCursor()
        self._input_buffer = ""

    # --- Appearance ---
    def apply_colors(self, foreground: str, background: str) -> None:
        """Apply terminal foreground/background colors."""
        self._foreground = foreground
        self._background = background
        self.setStyleSheet(
            f"background-color: {background}; color: {foreground}; font-family: monospace;"
        )

    # --- Input handling ---
    def send_command(self, command: str, add_newline: bool = True) -> None:
        """Send a command string to the PTY."""
        text = command + ("\n" if add_newline and not command.endswith("\n") else "")
        os.write(self.fd, text.encode())
        if add_newline:
            self._record_command(command)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Handle keyboard input and terminal emulation."""
        special_mappings = {
            Qt.Key.Key_Backspace: b"\x08",
            Qt.Key.Key_Tab: b"\t",
            Qt.Key.Key_Left: b"\x1b[D",
            Qt.Key.Key_Right: b"\x1b[C",
            Qt.Key.Key_Up: b"\x1b[A",
            Qt.Key.Key_Down: b"\x1b[B",
            Qt.Key.Key_Home: b"\x1b[H",
            Qt.Key.Key_End: b"\x1b[F",
            Qt.Key.Key_PageUp: b"\x1b[5~",
            Qt.Key.Key_PageDown: b"\x1b[6~",
            Qt.Key.Key_Delete: b"\x1b[3~",
            Qt.Key.Key_Insert: b"\x1b[2~",
            Qt.Key.Key_Escape: b"\x1b",
        }

        # Copy
        if event.matches(QKeySequence.StandardKey.Copy):
            if self.textCursor().hasSelection():
                self.copy()
                return
            os.write(self.fd, b"\x03")
            self._input_buffer = ""
            return

        # Paste
        if event.matches(QKeySequence.StandardKey.Paste):
            clipboard = QApplication.clipboard()
            text = clipboard.text()
            if text:
                os.write(self.fd, text.encode())
                self._input_buffer += text
            return

        key = event.key()

        # Enter/Return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            os.write(self.fd, b"\r")
            self._record_command(self._input_buffer)
            self._input_buffer = ""
            return

        # Backspace
        if key == Qt.Key.Key_Backspace:
            if self._input_buffer:
                self._input_buffer = self._input_buffer[:-1]
            os.write(self.fd, b"\x08")
            return

        # Special keys (arrows, etc.)
        if key in special_mappings:
            os.write(self.fd, special_mappings[key])
            return

        # Printable characters
        text = event.text()
        if text:
            os.write(self.fd, text.encode())
            self._input_buffer += text
        else:
            super().keyPressEvent(event)

    # --- Command logging ---
    def _record_command(self, command: str) -> None:
        """Emit a signal when a command is executed."""
        command = command.strip()
        if command:
            self.command_executed.emit(command)

    # --- Output handling ---
    def handle_output(self, text: str) -> None:
        """Render incoming text from PTY into the QTextEdit."""
        cursor = self._cursor
        i = 0

        while i < len(text):
            char = text[i]

            if char == "\r":
                cursor = self._cursor_to_line_start(cursor)
                i += 1
                continue
            if char == "\n":
                cursor = self._cursor_to_line_end(cursor)
                cursor.insertBlock()
                i += 1
                continue
            if char in ("\b", "\x7f"):
                if cursor.position() > 0:
                    cursor.deletePreviousChar()
                if i + 2 < len(text) and text[i + 1] == " " and text[i + 2] in ("\b", "\x7f"):
                    i += 3
                    continue
                i += 1
                continue
            if char == "\a":  # Bell character
                i += 1
                continue
            if char == "\x1b":  # Escape sequence
                i += 1
                if i >= len(text):
                    break

                next_char = text[i]
                if next_char == "[":
                    i += 1
                    params = ""
                    while i < len(text) and not text[i].isalpha():
                        params += text[i]
                        i += 1
                    if i >= len(text):
                        break
                    command = text[i]
                    
                    # Handle ANSI erase/clear commands
                    if command == "K":
                        mode = params or "0"
                        temp_cursor = QTextCursor(cursor)
                        if mode == "0":
                            temp_cursor.movePosition(
                                QTextCursor.MoveOperation.EndOfBlock,
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                        elif mode == "1":
                            temp_cursor.movePosition(
                                QTextCursor.MoveOperation.StartOfBlock,
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                        elif mode == "2":
                            temp_cursor.movePosition(
                                QTextCursor.MoveOperation.StartOfBlock,
                                QTextCursor.MoveMode.MoveAnchor,
                            )
                            temp_cursor.movePosition(
                                QTextCursor.MoveOperation.EndOfBlock,
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                        temp_cursor.removeSelectedText()
                        cursor = temp_cursor
                    elif command == "J":
                        mode = params or "0"
                        if mode in ("0", ""):
                            temp_cursor = QTextCursor(cursor)
                            temp_cursor.movePosition(
                                QTextCursor.MoveOperation.End,
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                            temp_cursor.removeSelectedText()
                            cursor = temp_cursor
                        elif mode == "1":
                            temp_cursor = QTextCursor(cursor)
                            temp_cursor.movePosition(
                                QTextCursor.MoveOperation.Start,
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                            temp_cursor.removeSelectedText()
                            cursor = temp_cursor
                        elif mode in ("2", "3"):
                            self.clear()
                            cursor = self.textCursor()
                    elif command in ("H", "f"):
                        row, col = 1, 1
                        if params:
                            parts = params.split(";")
                            if len(parts) == 2:
                                try:
                                    row = max(int(parts[0]), 1)
                                    col = max(int(parts[1]), 1)
                                except ValueError:
                                    row, col = 1, 1

                        document = self.document()
                        while document.blockCount() < row:
                            cursor = QTextCursor(document)
                            cursor.movePosition(QTextCursor.MoveOperation.End)
                            cursor.insertBlock()

                        block = document.findBlockByNumber(row - 1)
                        cursor = QTextCursor(block)
                        cursor.movePosition(
                            QTextCursor.MoveOperation.StartOfBlock
                        )
                        cursor.movePosition(
                            QTextCursor.MoveOperation.Right,
                            QTextCursor.MoveMode.MoveAnchor,
                            col - 1,
                        )
                    
                    i += 1
                    continue
                elif next_char == "]":
                    i += 1
                    while i < len(text):
                        if text[i] == "\a":
                            i += 1
                            break
                        if text[i] == "\x1b" and i + 1 < len(text) and text[i + 1] == "\\":
                            i += 2
                            break
                        i += 1
                    continue
                i += 1
                continue

            # Normal printable characters
            preview_cursor = QTextCursor(cursor)
            if preview_cursor.movePosition(
                QTextCursor.MoveOperation.Right,
                QTextCursor.MoveMode.KeepAnchor,
            ):
                if preview_cursor.selectedText() != "\u2029":
                    preview_cursor.removeSelectedText()
            cursor.insertText(char)
            i += 1

        self._cursor = cursor
        self.setTextCursor(cursor)
        self.ensureCursorVisible()


    # --- Cursor helpers ---
    def _cursor_to_line_end(self, cursor: QTextCursor) -> QTextCursor:
        temp_cursor = QTextCursor(cursor)
        temp_cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        return temp_cursor

    def _cursor_to_line_start(self, cursor: QTextCursor) -> QTextCursor:
        temp_cursor = QTextCursor(cursor)
        temp_cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        return temp_cursor


class _BreadcrumbLineEdit(QLineEdit):
    escapePressed = pyqtSignal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self.escapePressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class BreadcrumbLocationBar(QWidget):
    path_submitted = pyqtSignal(str)
    path_selected = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_path = ""

        self._line_edit = _BreadcrumbLineEdit()
        self._line_edit.returnPressed.connect(self._emit_submitted)
        self._line_edit.editingFinished.connect(self._show_breadcrumbs)
        self._line_edit.escapePressed.connect(self._cancel_edit)

        self._breadcrumb_container = QWidget()
        self._breadcrumb_layout = QHBoxLayout(self._breadcrumb_container)
        self._breadcrumb_layout.setContentsMargins(6, 2, 6, 2)
        self._breadcrumb_layout.setSpacing(4)
        self._breadcrumb_container.setObjectName("BreadcrumbContainer")
        self._breadcrumb_container.installEventFilter(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._breadcrumb_container)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(scroll)
        self._stack.addWidget(self._line_edit)
        self._stack.setCurrentWidget(scroll)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setObjectName("BreadcrumbLocationBar")
        self.setSizePolicy(self._line_edit.sizePolicy())
        self.setStyleSheet(
            "#BreadcrumbLocationBar {"
            " border: 1px solid palette(Mid);"
            " border-radius: 4px;"
            " background: palette(Base);"
            "}"
            "#BreadcrumbLocationBar QPushButton {"
            " border: none;"
            " padding: 2px 4px;"
            " text-align: left;"
            "}"
            "#BreadcrumbLocationBar QPushButton:hover {"
            " text-decoration: underline;"
            "}"
            "#BreadcrumbLocationBar QLabel {"
            " color: palette(Mid);"
            "}"
        )
        self._update_breadcrumbs()

    def set_colors(self, background: str, text_color: str) -> None:
        style = (
            "#BreadcrumbLocationBar {"
            f" border: 1px solid palette(Mid);"
            f" border-radius: 4px;"
            f" background: {background};"
            f" color: {text_color};"
            "}"
            "#BreadcrumbLocationBar QPushButton {"
            " border: none;"
            " padding: 2px 4px;"
            " text-align: left;"
            f" color: {text_color};"
            "}"
            "#BreadcrumbLocationBar QPushButton:hover {"
            " text-decoration: underline;"
            "}"
            "#BreadcrumbLocationBar QLabel {"
            f" color: {text_color};"
            "}"
        )
        self.setStyleSheet(style)
        self._line_edit.setStyleSheet(
            f"background-color: {background}; color: {text_color}; border: none;"
        )
        self._breadcrumb_container.setStyleSheet(
            f"background-color: {background}; color: {text_color};"
        )

    def eventFilter(self, source, event):  # noqa: N802 - Qt override
        if source is self._breadcrumb_container:
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self.start_editing()
                return True
            if event.type() == QEvent.Type.MouseButtonDblClick:
                self.start_editing()
                return True
        return super().eventFilter(source, event)

    def set_placeholder_text(self, text: str) -> None:
        self._line_edit.setPlaceholderText(text)

    def set_path(self, path: str) -> None:
        self._current_path = path
        self._line_edit.setText(path)
        self._update_breadcrumbs()
        self._show_breadcrumbs()

    def text(self) -> str:
        return self._line_edit.text()

    def current_path(self) -> str:
        return self._current_path

    def start_editing(self) -> None:
        self._line_edit.setText(self._current_path)
        self._stack.setCurrentWidget(self._line_edit)
        self._line_edit.setFocus()
        self._line_edit.selectAll()

    def focusInEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().focusInEvent(event)
        self.start_editing()

    def _emit_submitted(self) -> None:
        text = self._line_edit.text()
        self.path_submitted.emit(text)

    def _cancel_edit(self) -> None:
        self._line_edit.setText(self._current_path)
        self._show_breadcrumbs()

    def _show_breadcrumbs(self) -> None:
        self._stack.setCurrentIndex(0)

    def _update_breadcrumbs(self) -> None:
        while self._breadcrumb_layout.count():
            item = self._breadcrumb_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        path = os.path.abspath(self._current_path) if self._current_path else ""
        segments = self._build_segments(path)
        if not segments:
            placeholder = QLabel("No path selected")
            placeholder.setEnabled(False)
            self._breadcrumb_layout.addWidget(placeholder)
            self._breadcrumb_layout.addStretch(1)
            return

        for index, (label, target_path) in enumerate(segments):
            button = QPushButton(label)
            button.setFlat(True)
            button.clicked.connect(
                lambda _=False, p=target_path: self.path_selected.emit(p)
            )
            self._breadcrumb_layout.addWidget(button)
            if index < len(segments) - 1:
                arrow = QLabel("›")
                self._breadcrumb_layout.addWidget(arrow)

        self._breadcrumb_layout.addStretch(1)

    def _build_segments(self, path: str) -> list[tuple[str, str]]:
        if not path:
            return []

        path = os.path.abspath(path)
        segments: list[tuple[str, str]] = []
        home = str(Path.home())
        try:
            common = os.path.commonpath([path, home])
        except ValueError:
            common = ""

        if common == home:
            segments.append((Path(home).name, home))
            relative = os.path.relpath(path, home)
            if relative != ".":
                current = home
                for part in relative.split(os.sep):
                    current = os.path.join(current, part)
                    segments.append((part, current))
            return segments

        drive, tail = os.path.splitdrive(path)
        if drive:
            current = drive + os.sep
            segments.append((drive, current))
            tail = tail.lstrip(os.sep)
        else:
            current = os.sep
            segments.append((os.sep, current))
            tail = path.lstrip(os.sep)

        if tail:
            for part in tail.split(os.sep):
                current = os.path.join(current, part)
                segments.append((part, current))

        if segments and segments[0][0] == os.sep and len(segments) > 1:
            segments = segments[1:]

        return segments


class HistoryDialog(QDialog):
    command_run = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Command History")

        layout = QVBoxLayout(self)
        self._list = QListWidget()
        self._list.itemClicked.connect(self._copy_command)
        self._list.itemDoubleClicked.connect(self._run_command)
        layout.addWidget(self._list)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self.resize(480, 360)

    def set_history(self, history: list[str]) -> None:
        self._list.clear()
        for command in reversed(history):
            QListWidgetItem(command, self._list)

    def _copy_command(self, item: QListWidgetItem) -> None:
        QApplication.clipboard().setText(item.text())

    def _run_command(self, item: QListWidgetItem) -> None:
        command = item.text()
        self.command_run.emit(command)
        self.accept()

    def _cursor_to_line_end(self, cursor: QTextCursor) -> QTextCursor:
        temp_cursor = QTextCursor(cursor)
        temp_cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        return temp_cursor

    def _cursor_to_line_start(self, cursor: QTextCursor) -> QTextCursor:
        temp_cursor = QTextCursor(cursor)
        temp_cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        return temp_cursor

    def handle_output(self, text: str) -> None:
        cursor = self._cursor
        i = 0

        while i < len(text):
            char = text[i]

            if char == "\r":
                cursor = self._cursor_to_line_start(cursor)
                i += 1
                continue
            if char == "\n":
                cursor = self._cursor_to_line_end(cursor)
                cursor.insertBlock()
                i += 1
                continue
            if char in ("\b", "\x7f"):
                if cursor.position() > 0:
                    cursor.deletePreviousChar()

                if (
                    i + 2 < len(text)
                    and text[i + 1] == " "
                    and text[i + 2] in ("\b", "\x7f")
                ):
                    i += 3
                    continue

                i += 1
                continue
            if char == "\a":
                i += 1
                continue
            if char == "\x1b":
                i += 1
                if i >= len(text):
                    break

                next_char = text[i]
                if next_char == "[":
                    i += 1
                    params = ""
                    while i < len(text) and not text[i].isalpha():
                        params += text[i]
                        i += 1

                    if i >= len(text):
                        break

                    command = text[i]

                    if command == "K":
                        mode = params or "0"
                        temp_cursor = QTextCursor(cursor)
                        if mode == "0":
                            temp_cursor.movePosition(
                                QTextCursor.MoveOperation.EndOfBlock,
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                        elif mode == "1":
                            temp_cursor.movePosition(
                                QTextCursor.MoveOperation.StartOfBlock,
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                        elif mode == "2":
                            temp_cursor.movePosition(
                                QTextCursor.MoveOperation.StartOfBlock,
                                QTextCursor.MoveMode.MoveAnchor,
                            )
                            temp_cursor.movePosition(
                                QTextCursor.MoveOperation.EndOfBlock,
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                        temp_cursor.removeSelectedText()
                        cursor = temp_cursor
                    elif command == "J":
                        mode = params or "0"
                        if mode in ("0", ""):
                            temp_cursor = QTextCursor(cursor)
                            temp_cursor.movePosition(
                                QTextCursor.MoveOperation.End,
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                            temp_cursor.removeSelectedText()
                            cursor = temp_cursor
                        elif mode == "1":
                            temp_cursor = QTextCursor(cursor)
                            temp_cursor.movePosition(
                                QTextCursor.MoveOperation.Start,
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                            temp_cursor.removeSelectedText()
                            cursor = temp_cursor
                        elif mode in ("2", "3"):
                            self.clear()
                            cursor = self.textCursor()
                    elif command in ("H", "f"):
                        row, col = 1, 1
                        if params:
                            parts = params.split(";")
                            if len(parts) == 2:
                                try:
                                    row = max(int(parts[0]), 1)
                                    col = max(int(parts[1]), 1)
                                except ValueError:
                                    row, col = 1, 1

                        document = self.document()
                        while document.blockCount() < row:
                            cursor = QTextCursor(document)
                            cursor.movePosition(QTextCursor.MoveOperation.End)
                            cursor.insertBlock()

                        block = document.findBlockByNumber(row - 1)
                        cursor = QTextCursor(block)
                        cursor.movePosition(
                            QTextCursor.MoveOperation.StartOfBlock
                        )
                        cursor.movePosition(
                            QTextCursor.MoveOperation.Right,
                            QTextCursor.MoveMode.MoveAnchor,
                            col - 1,
                        )

                    i += 1
                    continue
                elif next_char == "]":
                    i += 1
                    while i < len(text):
                        if text[i] == "\a":
                            i += 1
                            break
                        if (
                            text[i] == "\x1b"
                            and i + 1 < len(text)
                            and text[i + 1] == "\\"
                        ):
                            i += 2
                            break
                        i += 1
                    continue

                i += 1
                continue

            preview_cursor = QTextCursor(cursor)
            if preview_cursor.movePosition(
                QTextCursor.MoveOperation.Right,
                QTextCursor.MoveMode.KeepAnchor,
            ):
                if preview_cursor.selectedText() != "\u2029":
                    preview_cursor.removeSelectedText()
            cursor.insertText(char)
            i += 1

        self._cursor = cursor
        self.setTextCursor(cursor)
        self.ensureCursorVisible()


class TerminalTab(QWidget):
    OSC_DIR_PREFIX = "\x1b]1337;CurrentDir="

    directory_changed = pyqtSignal(str)

    IMAGE_EXTENSIONS = {
        ".png",
        ".jpg",
        ".jpeg",
        ".bmp",
        ".gif",
        ".webp",
    }

    VIDEO_EXTENSIONS = {
        ".mp4",
        ".mkv",
        ".mov",
        ".avi",
        ".wmv",
        ".flv",
        ".webm",
        ".m4v",
    }

    ZIP_EXTENSIONS = {".zip"}

    def __init__(
        self,
        foreground: str,
        background: str,
        welcome_message: str | None,
        prompt: str,
        app_background: str,
        app_text_color: str,
        git_commands_enabled: bool,
        history_toolbar_enabled: bool,
    ) -> None:
        super().__init__()

        self.master, self.slave = pty.openpty()
        env = os.environ.copy()
        env["PROMPT_COMMAND"] = "printf '\\033]1337;CurrentDir=%s\\007' \"$PWD\""
        env["PS1"] = prompt
        self.process = subprocess.Popen(
            ["/bin/bash"],
            preexec_fn=os.setsid,
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            text=True,
            env=env,
        )

        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.model = QFileSystemModel()
        self.model.setFilter(
            QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.AllDirs
        )
        self.model.setRootPath(QDir.rootPath())
        self.proxy_model = QSortFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.model)
        self.proxy_model.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy_model.setFilterKeyColumn(0)
        self.proxy_model.setRecursiveFilteringEnabled(True)
        self.proxy_model.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        self.tree = QTreeView()
        self.tree.setModel(self.proxy_model)
        self.tree.doubleClicked.connect(self.on_item_double_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_context_menu)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)

        initial_index = self.model.index(os.getcwd())
        self.tree.setRootIndex(self.proxy_model.mapFromSource(initial_index))

        self.history: list[str] = []
        self.history_index = -1

        self.back_button = QPushButton("◀")
        self.back_button.setEnabled(False)
        self.back_button.clicked.connect(self.go_back)

        self.forward_button = QPushButton("▶")
        self.forward_button.setEnabled(False)
        self.forward_button.clicked.connect(self.go_forward)

        self.home_button = QPushButton("⌂")
        self.home_button.clicked.connect(self.go_home)

        self.history_button = QPushButton("History")
        self.history_button.setVisible(False)
        self.history_button.clicked.connect(self.show_history_dialog)

        self.location_bar = BreadcrumbLocationBar()
        self.location_bar.set_placeholder_text("Enter path and press Enter")
        self.location_bar.path_submitted.connect(self._on_location_entered)
        self.location_bar.path_selected.connect(self._on_breadcrumb_selected)
        self.location_display = self.location_bar

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by name…")
        self.filter_edit.textChanged.connect(self._on_filter_text_changed)

        self.sort_combo = QComboBox()
        self.sort_combo.addItem("Name", 0)
        self.sort_combo.addItem("Size", 1)
        self.sort_combo.addItem("Type", 2)
        self.sort_combo.addItem("Modified", 3)
        self.sort_combo.currentIndexChanged.connect(self._apply_sort)

        self._sort_order = Qt.SortOrder.AscendingOrder
        self.sort_order_button = QToolButton()
        self.sort_order_button.setText("↑")
        self.sort_order_button.clicked.connect(self._toggle_sort_order)

        nav_widget = QWidget()
        nav_layout = QVBoxLayout(nav_widget)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(4)

        nav_buttons_layout = QHBoxLayout()
        nav_buttons_layout.setContentsMargins(0, 0, 0, 0)
        nav_buttons_layout.setSpacing(4)
        nav_buttons_layout.addWidget(self.back_button)
        nav_buttons_layout.addWidget(self.forward_button)
        nav_buttons_layout.addWidget(self.home_button)
        nav_buttons_layout.addWidget(self.history_button)

        nav_layout.addLayout(nav_buttons_layout)
        nav_layout.addWidget(self.location_bar)
        self.location_bar_widget = nav_widget

        self._git_commands_enabled = git_commands_enabled
        self._history_toolbar_enabled = history_toolbar_enabled
        self._app_text_color = app_text_color
        self._copied_path: str | None = None

        sort_widget = QWidget()
        sort_layout = QHBoxLayout(sort_widget)
        sort_layout.setContentsMargins(0, 0, 0, 0)
        sort_layout.addWidget(QLabel("Sort by:"))
        sort_layout.addWidget(self.sort_combo)
        sort_layout.addWidget(self.sort_order_button)
        sort_layout.addSpacing(8)
        sort_layout.addWidget(QLabel("Filter:"))
        sort_layout.addWidget(self.filter_edit, 1)
        self.sort_widget = sort_widget

        explorer_panel = QWidget()
        explorer_layout = QVBoxLayout(explorer_panel)
        explorer_layout.setContentsMargins(0, 0, 0, 0)
        explorer_layout.addWidget(nav_widget)
        explorer_layout.addWidget(sort_widget)
        explorer_layout.addWidget(self.tree)
        self.explorer_panel = explorer_panel

        self.terminal = TerminalWidget(
            self.master,
            foreground,
            background,
            welcome_message,
        )
        self.terminal.command_executed.connect(self._on_command_executed)

        self.command_history: list[str] = []
        self._history_dialog: HistoryDialog | None = None
        self.history_button.setVisible(self._history_toolbar_enabled)

        self._edit_location_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self._edit_location_shortcut.activated.connect(self.location_bar.start_editing)

        splitter.addWidget(explorer_panel)
        splitter.addWidget(self.terminal)
        layout.addWidget(splitter)

        self.reader = TerminalReader(self.master)
        self.reader.output.connect(self.handle_output)
        self.reader.start()

        self.current_directory = os.getcwd()
        self.set_directory(self.current_directory, record_history=True, update_terminal=False)
        self.apply_app_background(app_background, app_text_color)

    def handle_output(self, text: str) -> None:
        self.terminal.handle_output(text)

        for directory in self._extract_directories(text):
            if os.path.isdir(directory):
                self.set_directory(
                    directory, record_history=True, update_terminal=False
                )

    def _extract_directories(self, text: str) -> list[str]:
        directories: list[str] = []
        marker = self.OSC_DIR_PREFIX
        index = 0
        while True:
            start = text.find(marker, index)
            if start == -1:
                break
            start += len(marker)
            end, terminator_length = self._find_osc_terminator(text, start)
            if end == -1:
                break
            directory = text[start:end]
            directories.append(directory)
            index = end + terminator_length
        return directories

    @staticmethod
    def _find_osc_terminator(text: str, start: int) -> tuple[int, int]:
        for i in range(start, len(text)):
            if text[i] == "\a":
                return i, 1
            if text[i] == "\x1b" and i + 1 < len(text) and text[i + 1] == "\\":
                return i, 2
        return -1, 0

    def set_directory(
        self,
        path: str,
        record_history: bool = True,
        update_terminal: bool = True,
        show_error: bool = False,
    ) -> bool:
        directory = os.path.abspath(path)
        if not os.path.isdir(directory):
            if show_error:
                QMessageBox.warning(
                    self,
                    "Invalid Directory",
                    f"The path '{path}' is not a valid directory.",
                )
                current = (
                    self.history[self.history_index]
                    if 0 <= self.history_index < len(self.history)
                    else os.getcwd()
                )
                self.location_bar.set_path(current)
            return False

        if record_history:
            if (
                self.history_index >= 0
                and self.history[: self.history_index + 1]
                and self.history[self.history_index] == directory
            ):
                pass
            else:
                self.history = self.history[: self.history_index + 1]
                self.history.append(directory)
                self.history_index += 1
        self.update_history_buttons()

        self._set_tree_root(directory)
        self.location_bar.set_path(directory)
        self.directory_changed.emit(directory)

        if update_terminal:
            self.terminal.send_command(f"cd {shlex.quote(directory)}")

        self.current_directory = directory
        return True

    def _on_location_entered(self) -> None:
        path = self.location_bar.text()
        if not path:
            return
        if not self.set_directory(path, record_history=True, update_terminal=True, show_error=True):
            current = self.history[self.history_index] if self.history else os.getcwd()
            self.location_bar.set_path(current)

    def update_history_buttons(self) -> None:
        self.back_button.setEnabled(self.history_index > 0)
        self.forward_button.setEnabled(self.history_index < len(self.history) - 1)

    def go_back(self) -> None:
        if self.history_index > 0:
            self.history_index -= 1
            path = self.history[self.history_index]
            self.update_history_buttons()
            self.set_directory(path, record_history=False, update_terminal=True)

    def go_forward(self) -> None:
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            path = self.history[self.history_index]
            self.update_history_buttons()
            self.set_directory(path, record_history=False, update_terminal=True)

    def go_home(self) -> None:
        home = str(Path.home())
        self.set_directory(home, record_history=True, update_terminal=True)

    def refresh_current_directory(self) -> None:
        current_path = self.location_bar.text()
        if current_path:
            self.set_directory(
                current_path, record_history=False, update_terminal=False
            )
            self.terminal.send_command("pwd")

    def _on_breadcrumb_selected(self, path: str) -> None:
        self.set_directory(path, record_history=True, update_terminal=True)

    def show_history_dialog(self) -> None:
        if self._history_dialog is None:
            self._history_dialog = HistoryDialog(self)
            self._history_dialog.command_run.connect(self._run_history_command)
        self._history_dialog.set_history(self.command_history)
        self._history_dialog.show()
        self._history_dialog.raise_()
        self._history_dialog.activateWindow()

    def _run_history_command(self, command: str) -> None:
        if not command:
            return
        self.terminal.send_command(command)

    def _on_command_executed(self, command: str) -> None:
        self.command_history.append(command)
        if self._history_dialog is not None and self._history_dialog.isVisible():
            self._history_dialog.set_history(self.command_history)

    def set_git_commands_enabled(self, enabled: bool) -> None:
        self._git_commands_enabled = enabled

    def set_history_toolbar_enabled(self, enabled: bool) -> None:
        self._history_toolbar_enabled = enabled
        self.history_button.setVisible(enabled)
        if not enabled and self._history_dialog is not None:
            self._history_dialog.close()

    def _git_root_for_path(self, path: str) -> str | None:
        candidate = os.path.abspath(path)
        if os.path.isfile(candidate):
            candidate = os.path.dirname(candidate)

        while True:
            if os.path.isdir(os.path.join(candidate, ".git")):
                return candidate
            parent = os.path.dirname(candidate)
            if parent == candidate:
                break
            candidate = parent
        return None

    def _run_git_command(self, directory: str, command: str) -> None:
        full_command = f"cd {shlex.quote(directory)} && {command}"
        self.terminal.send_command(full_command)

    def _git_commit(self, directory: str) -> None:
        message, ok = QInputDialog.getText(
            self,
            "Git Commit",
            "Enter commit message:",
        )
        if not ok:
            return
        message = message.strip()
        if not message:
            QMessageBox.information(
                self,
                "Git Commit",
                "Commit message cannot be empty.",
            )
            return
        self._run_git_command(directory, f"git commit -m {shlex.quote(message)}")

    def _git_checkout(self, directory: str) -> None:
        branch, ok = QInputDialog.getText(
            self,
            "Git Checkout",
            "Enter branch name:",
        )
        if not ok:
            return
        branch = branch.strip()
        if not branch:
            QMessageBox.information(
                self,
                "Git Checkout",
                "Branch name cannot be empty.",
            )
            return
        self._run_git_command(directory, f"git checkout {shlex.quote(branch)}")

    def on_item_double_clicked(self, index) -> None:
        if not index.isValid():
            return
        source_index = self.proxy_model.mapToSource(index)
        path = self.model.filePath(source_index)
        self.open_path(path)

    def show_context_menu(self, point: QPoint) -> None:
        index = self.tree.indexAt(point)
        if not index.isValid():
            return

        source_index = self.proxy_model.mapToSource(index)
        path = self.model.filePath(source_index)
        target_directory = path if os.path.isdir(path) else os.path.dirname(path)
        if target_directory:
            target_directory = os.path.abspath(target_directory)
        menu = QMenu(self)

        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self.refresh_current_directory)
        menu.addAction(refresh_action)

        if os.path.isfile(path):
            open_action = QAction("Open", self)
            open_action.triggered.connect(lambda: self.open_path(path))
            menu.addAction(open_action)

            editor_action = QAction("Open in Editor", self)
            editor_action.triggered.connect(lambda: self.open_in_editor(path))
            menu.addAction(editor_action)

            run_command = self._command_for_script(path)
            if run_command:
                run_action = QAction("Run Script", self)
                run_action.triggered.connect(lambda: self.run_script_file(path))
                menu.addAction(run_action)
        else:
            open_action = QAction("Open", self)
            open_action.triggered.connect(lambda: self.open_path(path))
            menu.addAction(open_action)

        copy_action = QAction("Copy", self)
        copy_action.triggered.connect(lambda: self.copy_path(path))
        menu.addAction(copy_action)

        paste_action = QAction("Paste", self)
        paste_action.triggered.connect(
            lambda: self.paste_into_directory(target_directory)
        )
        can_paste = (
            bool(self._copied_path)
            and os.path.isdir(target_directory)
            and os.path.exists(self._copied_path or "")
        )
        paste_action.setEnabled(can_paste)
        menu.addAction(paste_action)

        new_folder_action = QAction("New Folder…", self)
        new_folder_action.triggered.connect(
            lambda: self.create_new_folder(target_directory)
        )
        menu.addAction(new_folder_action)

        menu.addSeparator()

        delete_action = QAction("Delete", self)
        delete_action.triggered.connect(lambda: self.delete_path(path))
        menu.addAction(delete_action)

        permissions_menu = menu.addMenu("Permissions")
        permission_presets = [
            ("Read/Write (Owner)", 0o600),
            ("Read/Write for Everyone", 0o666),
            ("Executable (Owner)", 0o700),
            ("Executable for Everyone", 0o755),
            ("Read-Only", 0o444),
        ]
        for label, mode in permission_presets:
            action = QAction(label, self)
            action.triggered.connect(
                lambda _=False, m=mode: self.apply_permission_preset(path, m)
            )
            permissions_menu.addAction(action)

        git_root = self._git_root_for_path(path) if self._git_commands_enabled else None
        if git_root:
            git_menu = menu.addMenu("Git Commands")

            status_action = QAction("Status", self)
            status_action.triggered.connect(
                lambda _=False, root=git_root: self._run_git_command(root, "git status")
            )
            git_menu.addAction(status_action)

            commit_action = QAction("Commit…", self)
            commit_action.triggered.connect(lambda _=False, root=git_root: self._git_commit(root))
            git_menu.addAction(commit_action)

            checkout_action = QAction("Checkout Branch…", self)
            checkout_action.triggered.connect(
                lambda _=False, root=git_root: self._git_checkout(root)
            )
            git_menu.addAction(checkout_action)

            pull_action = QAction("Pull", self)
            pull_action.triggered.connect(
                lambda _=False, root=git_root: self._run_git_command(root, "git pull")
            )
            git_menu.addAction(pull_action)

            push_action = QAction("Push", self)
            push_action.triggered.connect(
                lambda _=False, root=git_root: self._run_git_command(root, "git push")
            )
            git_menu.addAction(push_action)

        menu.exec(self.tree.viewport().mapToGlobal(point))

    def open_path(self, path: str) -> None:
        if os.path.isdir(path):
            self.set_directory(path, record_history=True, update_terminal=True)
            return

        suffix = Path(path).suffix.lower()
        if suffix in self.IMAGE_EXTENSIONS:
            self.open_image(path)
        elif suffix in self.VIDEO_EXTENSIONS:
            self.open_video(path)
        elif suffix in self.ZIP_EXTENSIONS:
            self.open_zip(path)
        else:
            self.open_in_editor(path)

    def open_in_editor(self, path: str) -> None:
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            QMessageBox.critical(self, "Error", f"Failed to open file:\n{exc}")
            return

        dialog = EditorDialog(path, content, parent=self)
        dialog.exec()

    def open_image(self, path: str) -> None:
        if not os.path.isfile(path):
            return
        dialog = PhotoViewerDialog(path, parent=self)
        dialog.exec()

    def open_video(self, path: str) -> None:
        if not os.path.isfile(path):
            return
        dialog = VideoPlayerDialog(path, parent=self)
        dialog.exec()

    def open_zip(self, path: str) -> None:
        if not os.path.isfile(path):
            return
        dialog = ZipViewerDialog(path, parent=self)
        dialog.exec()

    def run_script_file(self, path: str) -> None:
        command = self._command_for_script(path)
        if not command:
            QMessageBox.information(
                self,
                "Run Script",
                "Unable to detect a compatible interpreter for this file.",
            )
            return
        self.terminal.send_command(command)

    def copy_path(self, path: str) -> None:
        if os.path.exists(path):
            self._copied_path = os.path.abspath(path)

    def paste_into_directory(self, directory: str) -> None:
        if not directory or not os.path.isdir(directory):
            return
        if not self._copied_path or not os.path.exists(self._copied_path):
            QMessageBox.information(
                self,
                "Paste",
                "There is nothing to paste.",
            )
            self._copied_path = None
            return

        source = Path(self._copied_path)
        target_dir = Path(os.path.abspath(directory))
        destination = self._unique_destination(target_dir, source.name)

        try:
            if source.is_dir() and not source.is_symlink():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        except OSError as exc:
            QMessageBox.critical(self, "Paste Failed", f"Could not paste item:\n{exc}")
            return

        self.refresh_current_directory()

    def create_new_folder(self, directory: str) -> None:
        if not directory or not os.path.isdir(directory):
            return
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.information(
                self,
                "New Folder",
                "Folder name cannot be empty.",
            )
            return
        if any(sep and sep in name for sep in (os.sep, os.altsep)):
            QMessageBox.information(
                self,
                "New Folder",
                "Folder name cannot contain path separators.",
            )
            return

        new_path = Path(directory) / name
        if new_path.exists():
            QMessageBox.information(
                self,
                "New Folder",
                "A folder with that name already exists.",
            )
            return

        try:
            new_path.mkdir()
        except OSError as exc:
            QMessageBox.critical(self, "New Folder", f"Could not create folder:\n{exc}")
            return

        self.refresh_current_directory()

    def _unique_destination(self, directory: Path, name: str) -> Path:
        candidate = directory / name
        if not candidate.exists():
            return candidate

        source_path = Path(name)
        suffixes = "".join(source_path.suffixes)
        stem = source_path.name[: -len(suffixes)] if suffixes else source_path.name
        counter = 1
        while True:
            new_name = f"{stem} ({counter}){suffixes}"
            candidate = directory / new_name
            if not candidate.exists():
                return candidate
            counter += 1

    def apply_colors(self, foreground: str, background: str) -> None:
        self.terminal.apply_colors(foreground, background)

    def apply_app_background(self, color: str, text_color: str | None = None) -> None:
        if text_color is not None:
            self._app_text_color = text_color

        base_style = f"background-color: {color}; color: {self._app_text_color};"
        self.explorer_panel.setStyleSheet(base_style)
        self.location_bar_widget.setStyleSheet(base_style)
        self.sort_widget.setStyleSheet(base_style)
        self.location_bar.set_colors(color, self._app_text_color)
        control_style = f"color: {self._app_text_color}; background-color: {color};"
        self.filter_edit.setStyleSheet(control_style)
        self.sort_combo.setStyleSheet(control_style)
        button_style = f"color: {self._app_text_color}; background-color: {color};"
        for button in (
            self.back_button,
            self.forward_button,
            self.home_button,
            self.history_button,
            self.sort_order_button,
        ):
            button.setStyleSheet(button_style)
        tree_style = (
            "QTreeView {"
            f" background-color: {color};"
            f" color: {self._app_text_color};"
            "}"
            "QHeaderView::section {"
            f" background-color: {color};"
            f" color: {self._app_text_color};"
            "}"
        )
        self.tree.setStyleSheet(tree_style)

    def _set_tree_root(self, directory: str) -> None:
        source_index = self.model.index(directory)
        if source_index.isValid():
            proxy_index = self.proxy_model.mapFromSource(source_index)
            self.tree.setRootIndex(proxy_index)

    def _on_filter_text_changed(self, text: str) -> None:
        pattern = f"*{text}*" if text else "*"
        self.proxy_model.setFilterWildcard(pattern)

    def _apply_sort(self) -> None:
        column = self.sort_combo.currentData()
        self.tree.sortByColumn(column, self._sort_order)

    def _toggle_sort_order(self) -> None:
        if self._sort_order == Qt.SortOrder.AscendingOrder:
            self._sort_order = Qt.SortOrder.DescendingOrder
            self.sort_order_button.setText("↓")
        else:
            self._sort_order = Qt.SortOrder.AscendingOrder
            self.sort_order_button.setText("↑")
        self._apply_sort()

    def _command_for_script(self, path: str) -> str | None:
        if not os.path.isfile(path):
            return None

        suffix = Path(path).suffix.lower()
        commands: dict[str, str] = {
            ".py": f"{shlex.quote(sys.executable)} {shlex.quote(path)}",
            ".sh": f"bash {shlex.quote(path)}",
            ".bash": f"bash {shlex.quote(path)}",
            ".zsh": f"zsh {shlex.quote(path)}",
            ".ksh": f"ksh {shlex.quote(path)}",
            ".csh": f"csh {shlex.quote(path)}",
            ".tcsh": f"tcsh {shlex.quote(path)}",
            ".pl": f"perl {shlex.quote(path)}",
            ".rb": f"ruby {shlex.quote(path)}",
            ".php": f"php {shlex.quote(path)}",
        }

        if suffix in commands:
            return commands[suffix]

        try:
            with open(path, "rb") as fh:
                first_line = fh.readline().decode("utf-8", "ignore").strip()
        except OSError:
            first_line = ""

        if first_line.startswith("#!"):
            interpreter = first_line[2:]
            if "python" in interpreter:
                return f"{shlex.quote(sys.executable)} {shlex.quote(path)}"
            if interpreter:
                return f"{interpreter} {shlex.quote(path)}"

        if os.access(path, os.X_OK):
            return shlex.quote(path)

        return None

    def delete_path(self, path: str) -> None:
        reply = QMessageBox.question(
            self,
            "Delete",
            f"Are you sure you want to delete '{os.path.basename(path)}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as exc:
            QMessageBox.critical(self, "Delete Failed", f"Could not delete item:\n{exc}")
            return

        self.refresh_current_directory()

    def apply_permission_preset(self, path: str, mode: int) -> None:
        if os.path.islink(path):
            QMessageBox.information(
                self,
                "Permissions",
                "Changing permissions on symbolic links is not supported.",
            )
            return

        try:
            current_mode = os.stat(path, follow_symlinks=False).st_mode
            new_mode = (current_mode & ~0o777) | mode
            os.chmod(path, new_mode, follow_symlinks=False)
        except OSError as exc:
            QMessageBox.critical(self, "Permissions", f"Failed to change permissions:\n{exc}")
            return

        QMessageBox.information(
            self,
            "Permissions",
            "Permissions updated successfully.",
        )


class PreferencesDialog(QDialog):
    def __init__(
        self,
        foreground: str,
        background: str,
        app_background: str,
        app_text_color: str,
        presets: list[dict[str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self._foreground = foreground
        self._background = background
        self._app_background = app_background
        self._app_text_color = app_text_color
        self._presets = [preset.copy() for preset in presets]
        self._suspend_preset_signal = False

        layout = QVBoxLayout(self)

        presets_group = QGroupBox("Presets")
        presets_layout = QHBoxLayout(presets_group)
        self._preset_combo = QComboBox()
        self._preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        presets_layout.addWidget(self._preset_combo, 1)
        self._save_preset_button = QPushButton("Save Preset…")
        self._save_preset_button.clicked.connect(self._save_preset)
        presets_layout.addWidget(self._save_preset_button)
        self._delete_preset_button = QPushButton("Delete Preset")
        self._delete_preset_button.clicked.connect(self._delete_preset)
        presets_layout.addWidget(self._delete_preset_button)
        layout.addWidget(presets_group)

        terminal_group = QGroupBox("Terminal Appearance")
        terminal_layout = QVBoxLayout(terminal_group)

        self._preview = QTextEdit()
        self._preview.setReadOnly(True)
        terminal_layout.addWidget(self._preview)

        button_row = QHBoxLayout()

        self._text_color_button = QPushButton("Text Color…")
        self._text_color_button.clicked.connect(self._choose_text_color)
        button_row.addWidget(self._text_color_button)

        self._background_button = QPushButton("Background Color…")
        self._background_button.clicked.connect(self._choose_background_color)
        button_row.addWidget(self._background_button)

        button_row.addStretch(1)
        terminal_layout.addLayout(button_row)
        layout.addWidget(terminal_group)

        app_group = QGroupBox("Application Appearance")
        app_layout = QVBoxLayout(app_group)
        self._app_preview = QTextEdit()
        self._app_preview.setReadOnly(True)
        self._app_preview.setFixedHeight(60)
        app_layout.addWidget(self._app_preview)

        app_button_row = QHBoxLayout()
        self._app_text_button = QPushButton("Text Color…")
        self._app_text_button.clicked.connect(self._choose_app_text_color)
        app_button_row.addWidget(self._app_text_button)

        self._app_background_button = QPushButton("Background Color…")
        self._app_background_button.clicked.connect(self._choose_app_background_color)
        app_button_row.addWidget(self._app_background_button)
        app_button_row.addStretch(1)
        app_layout.addLayout(app_button_row)
        layout.addWidget(app_group)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self.resize(500, 320)
        self._update_preview()
        self._update_app_preview()
        self._populate_presets()

    def _choose_text_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._foreground), self, "Select Text Color")
        if color.isValid():
            self._foreground = color.name()
            self._update_preview()

    def _choose_background_color(self) -> None:
        color = QColorDialog.getColor(
            QColor(self._background), self, "Select Background Color"
        )
        if color.isValid():
            self._background = color.name()
            self._update_preview()

    def _choose_app_background_color(self) -> None:
        color = QColorDialog.getColor(
            QColor(self._app_background), self, "Select Application Background"
        )
        if color.isValid():
            self._app_background = color.name()
            self._update_app_preview()

    def _choose_app_text_color(self) -> None:
        color = QColorDialog.getColor(
            QColor(self._app_text_color), self, "Select Application Text Color"
        )
        if color.isValid():
            self._app_text_color = color.name()
            self._update_app_preview()

    def _update_preview(self) -> None:
        self._preview.setStyleSheet(
            f"background-color: {self._background}; color: {self._foreground}; font-family: monospace;"
        )
        self._preview.setPlainText(
            "Preview of the terminal colors.\n"
            "Username and host colors are controlled by the prompt."
        )

    def _update_app_preview(self) -> None:
        self._app_preview.setStyleSheet(
            f"background-color: {self._app_background};"
            f" color: {self._app_text_color};"
            " border: 1px solid #444;"
        )
        self._app_preview.setPlainText(
            "Preview of the application panels.\n"
            "Tree, buttons, and breadcrumbs use this palette."
        )

    def _populate_presets(self) -> None:
        self._suspend_preset_signal = True
        self._preset_combo.clear()
        self._preset_combo.addItem("Select preset…", None)
        for preset in self._presets:
            self._preset_combo.addItem(preset.get("name", "Unnamed"), preset)
        self._preset_combo.setCurrentIndex(0)
        self._delete_preset_button.setEnabled(False)
        self._suspend_preset_signal = False

    def _on_preset_selected(self, index: int) -> None:
        if self._suspend_preset_signal:
            return
        preset = self._preset_combo.itemData(index)
        self._delete_preset_button.setEnabled(preset is not None)
        if not preset:
            return
        self._foreground = preset.get("foreground", self._foreground)
        self._background = preset.get("background", self._background)
        self._app_background = preset.get("app_background", self._app_background)
        self._app_text_color = preset.get("app_text_color", self._app_text_color)
        self._update_preview()
        self._update_app_preview()

    def _save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Preset Name", "Enter a name for this preset:")
        if not ok or not name.strip():
            return
        name = name.strip()
        preset = {
            "name": name,
            "foreground": self._foreground,
            "background": self._background,
            "app_background": self._app_background,
            "app_text_color": self._app_text_color,
        }
        existing_index = next(
            (i for i, item in enumerate(self._presets) if item.get("name") == name),
            None,
        )
        if existing_index is not None:
            self._presets[existing_index] = preset
        else:
            self._presets.append(preset)
        self._populate_presets()
        new_index = next(
            (i for i in range(self._preset_combo.count()) if self._preset_combo.itemText(i) == name),
            0,
        )
        self._preset_combo.setCurrentIndex(new_index)

    def _delete_preset(self) -> None:
        index = self._preset_combo.currentIndex()
        preset = self._preset_combo.itemData(index)
        if not preset:
            return
        self._presets = [p for p in self._presets if p is not preset]
        self._populate_presets()

    def colors(self) -> tuple[str, str, str, str]:
        return (
            self._foreground,
            self._background,
            self._app_background,
            self._app_text_color,
        )

    def presets(self) -> list[dict[str, str]]:
        return [preset.copy() for preset in self._presets]


class TerminalApp(QMainWindow):
    APP_NAME = "DirShell"

    PROMPT = (
        "\\[\\e[1;36m\\]\\u\\[\\e[0m\\]@"
        "\\[\\e[1;35m\\]\\h\\[\\e[0m\\]:"
        "\\[\\e[1;34m\\]\\w\\[\\e[0m\\]$ "
    )

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(self.APP_NAME)
        OPEN_WINDOWS.append(self)

        config = self._load_config()
        self._foreground = config.get("foreground", DEFAULT_FOREGROUND)
        self._background = config.get("background", DEFAULT_BACKGROUND)
        self._app_background = config.get("app_background", DEFAULT_APP_BACKGROUND)
        self._app_text_color = config.get("app_text_color", DEFAULT_APP_TEXT_COLOR)
        presets = config.get("presets", [])
        if not isinstance(presets, list):
            presets = []
        self._presets = [p for p in presets if isinstance(p, dict)]
        addons = config.get("addons", {})
        if not isinstance(addons, dict):
            addons = {}
        self._addons = {
            "git_commands": bool(addons.get("git_commands", False)),
            "history_toolbar": bool(addons.get("history_toolbar", False)),
        }
        self._welcome_message = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")

        new_window_action = QAction("New Window", self)
        new_window_action.triggered.connect(self.open_new_window)
        file_menu.addAction(new_window_action)

        file_menu.addSeparator()

        preferences_action = QAction("Preferences…", self)
        preferences_action.triggered.connect(self.open_preferences)
        file_menu.addAction(preferences_action)

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        options_menu = menu_bar.addMenu("Options")
        addons_menu = options_menu.addMenu("Add-ons")

        self._git_addon_action = QAction("Git Commands", self)
        self._git_addon_action.setCheckable(True)
        self._git_addon_action.setChecked(self._addons["git_commands"])
        self._git_addon_action.toggled.connect(self._set_git_addon_enabled)
        addons_menu.addAction(self._git_addon_action)

        self._history_addon_action = QAction("Tool Bar History", self)
        self._history_addon_action.setCheckable(True)
        self._history_addon_action.setChecked(self._addons["history_toolbar"])
        self._history_addon_action.toggled.connect(self._set_history_addon_enabled)
        addons_menu.addAction(self._history_addon_action)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        layout.addWidget(self.tabs)

        self._new_tab_placeholder = QWidget()
        self.tabs.blockSignals(True)
        self.tabs.addTab(self._new_tab_placeholder, "+")
        self.tabs.blockSignals(False)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.new_tab()
        self._apply_app_background()
        self._apply_addons_to_tabs()

    def new_tab(self) -> None:
        placeholder_index = self._placeholder_index()
        if placeholder_index == -1:
            self._new_tab_placeholder = QWidget()
            placeholder_index = self.tabs.addTab(self._new_tab_placeholder, "+")

        tab = TerminalTab(
            self._foreground,
            self._background,
            self._welcome_message,
            self.PROMPT,
            self._app_background,
            self._app_text_color,
            self._addons["git_commands"],
            self._addons["history_toolbar"],
        )
        index = self.tabs.insertTab(placeholder_index, tab, self._tab_title(tab))
        self.tabs.setCurrentIndex(index)
        tab.directory_changed.connect(
            lambda path, t=tab: self._on_directory_changed(t, path)
        )
        tab.terminal.setFocus()
        tab.apply_colors(self._foreground, self._background)
        tab.apply_app_background(self._app_background, self._app_text_color)
        self._apply_addons_to_tabs()
        self._update_tab_close_buttons()

    def _placeholder_index(self) -> int:
        if not hasattr(self, "_new_tab_placeholder"):
            return -1
        return self.tabs.indexOf(self._new_tab_placeholder)

    def _actual_tab_count(self) -> int:
        count = self.tabs.count()
        placeholder_index = self._placeholder_index()
        if placeholder_index != -1:
            count -= 1
        return count

    def _update_tab_close_buttons(self) -> None:
        allow_close = self._actual_tab_count() > 1
        self.tabs.setTabsClosable(allow_close)
        placeholder_index = self._placeholder_index()
        if placeholder_index != -1:
            tab_bar = self.tabs.tabBar()
            tab_bar.setTabButton(
                placeholder_index, QTabBar.ButtonPosition.RightSide, None
            )

    def _on_tab_changed(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is self._new_tab_placeholder:
            self.new_tab()

    def open_new_window(self) -> None:
        window = TerminalApp()
        window.setGeometry(self.geometry())
        window.show()

    def open_preferences(self) -> None:
        dialog = PreferencesDialog(
            self._foreground,
            self._background,
            self._app_background,
            self._app_text_color,
            self._presets,
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            foreground, background, app_background, app_text_color = dialog.colors()
            self._foreground = foreground
            self._background = background
            self._app_background = app_background
            self._app_text_color = app_text_color
            self._presets = dialog.presets()
            self._apply_colors_to_tabs()
            self._apply_app_background()
            self._save_config()

    def _apply_colors_to_tabs(self) -> None:
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, TerminalTab):
                widget.apply_colors(self._foreground, self._background)
                widget.apply_app_background(self._app_background, self._app_text_color)

    def _apply_app_background(self) -> None:
        central = self.centralWidget()
        if central is not None:
            central.setStyleSheet(
                f"background-color: {self._app_background};"
                f" color: {self._app_text_color};"
            )
        self.tabs.setStyleSheet(
            "QTabWidget::pane {"
            f" background: {self._app_background};"
            "}"
            "QTabBar::tab {"
            f" color: {self._app_text_color};"
            "}"
        )
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, TerminalTab):
                widget.apply_app_background(
                    self._app_background, self._app_text_color
                )

    def _apply_addons_to_tabs(self) -> None:
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, TerminalTab):
                widget.set_git_commands_enabled(self._addons["git_commands"])
                widget.set_history_toolbar_enabled(self._addons["history_toolbar"])

    def _set_git_addon_enabled(self, enabled: bool) -> None:
        self._addons["git_commands"] = enabled
        self._apply_addons_to_tabs()
        self._save_config()

    def _set_history_addon_enabled(self, enabled: bool) -> None:
        self._addons["history_toolbar"] = enabled
        self._apply_addons_to_tabs()
        self._save_config()

    def _shutdown_tab(self, tab: TerminalTab) -> None:
        tab.reader.running = False
        try:
            os.close(tab.master)
        except OSError:
            pass

        tab.reader.wait(200)

        try:
            if tab.process.poll() is None:
                try:
                    pgid = os.getpgid(tab.process.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except Exception:
                    try:
                        tab.process.terminate()
                    except Exception:
                        pass

                try:
                    tab.process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    try:
                        pgid = os.getpgid(tab.process.pid)
                        os.killpg(pgid, signal.SIGKILL)
                    except Exception:
                        try:
                            tab.process.kill()
                        except Exception:
                            pass
        except Exception:
            pass

    def _tab_title(self, tab: TerminalTab) -> str:
        path = tab.location_display.text()
        name = os.path.basename(path) or path
        return name or "Terminal"

    def _on_directory_changed(self, tab: TerminalTab, path: str) -> None:
        index = self.tabs.indexOf(tab)
        if index != -1:
            name = os.path.basename(path) or path
            self.tabs.setTabText(index, name or "Terminal")

    def close_tab(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is self._new_tab_placeholder:
            return
        actual_count = self._actual_tab_count()
        if actual_count <= 1:
            return

        if isinstance(widget, TerminalTab):
            # Determine the current index of the widget (defensive: index may be stale)
            real_index = self.tabs.indexOf(widget)
            self.tabs.blockSignals(True)
            try:
                if real_index != -1:
                    self.tabs.removeTab(real_index)
                else:
                    # Fallback to the provided index
                    try:
                        self.tabs.removeTab(index)
                    except Exception:
                        pass

                for j in range(self.tabs.count()):
                    w = self.tabs.widget(j)
                    if isinstance(w, TerminalTab):
                        self.tabs.setCurrentIndex(j)
                        break
            finally:
                self.tabs.blockSignals(False)

            threading.Thread(target=self._shutdown_tab, args=(widget,), daemon=True).start()
            widget.deleteLater()
        else:
            self.tabs.blockSignals(True)
            try:
                try:
                    self.tabs.removeTab(index)
                except Exception:
                    pass
                for j in range(self.tabs.count()):
                    w = self.tabs.widget(j)
                    if isinstance(w, TerminalTab):
                        self.tabs.setCurrentIndex(j)
                        break
            finally:
                self.tabs.blockSignals(False)

        self._update_tab_close_buttons()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Shutdown each tab asynchronously so the GUI can close without
        # blocking on process/thread teardown. Each call runs in a daemon
        # thread and performs best-effort termination of the shell and
        # reader thread.
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, TerminalTab):
                threading.Thread(
                    target=self._shutdown_tab, args=(widget,), daemon=True
                ).start()

        super().closeEvent(event)
        self._save_config()
        if self in OPEN_WINDOWS:
            OPEN_WINDOWS.remove(self)

    def _load_config(self) -> dict:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_config(self) -> None:
        data = {
            "foreground": self._foreground,
            "background": self._background,
            "app_background": self._app_background,
            "app_text_color": self._app_text_color,
            "presets": self._presets,
            "addons": self._addons.copy(),
        }
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError:
            pass


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = TerminalApp()
    win.resize(1000, 600)
    win.show()
    try:
        screen = app.primaryScreen()
        if screen is not None:
            geom = screen.availableGeometry()
            x = geom.x() + (geom.width() - win.width()) // 2
            y = geom.y() + (geom.height() - win.height()) // 2
            win.move(x, y)
    except Exception:
        # Best-effort; don't crash startup if something unusual occurs.
        pass

    sys.exit(app.exec())
