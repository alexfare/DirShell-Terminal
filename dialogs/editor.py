from __future__ import annotations
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QKeySequence, QTextCursor, QTextDocument, QAction, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QPlainTextEdit,
)


class FindBar(QWidget):
    find_next_requested = pyqtSignal(str, bool)
    find_previous_requested = pyqtSignal(str, bool)
    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel("Find:"))

        self.query_edit = QLineEdit()
        layout.addWidget(self.query_edit, stretch=1)

        self.case_sensitive = QCheckBox("Case sensitive")
        layout.addWidget(self.case_sensitive)

        self.previous_button = QPushButton("◀")
        self.previous_button.clicked.connect(self._find_previous)
        layout.addWidget(self.previous_button)

        self.next_button = QPushButton("▶")
        self.next_button.clicked.connect(self._find_next)
        layout.addWidget(self.next_button)

        close_button = QPushButton("✕")
        close_button.clicked.connect(self._close)
        layout.addWidget(close_button)

        self.query_edit.returnPressed.connect(self._find_next)

    def focus_on_query(self) -> None:
        self.query_edit.setFocus()
        self.query_edit.selectAll()

    def _close(self) -> None:
        self.hide()
        self.closed.emit()

    def _find_next(self) -> None:
        self.find_next_requested.emit(
            self.query_edit.text(), self.case_sensitive.isChecked()
        )

    def _find_previous(self) -> None:
        self.find_previous_requested.emit(
            self.query_edit.text(), self.case_sensitive.isChecked()
        )


class EditorDialog(QDialog):
    def __init__(
        self,
        path: str,
        content: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.path = Path(path)

        self.editor = QPlainTextEdit()
        self.editor.setPlainText(content)
        self.editor.document().setModified(False)
        self.editor.textChanged.connect(self._update_window_title)

        self.setWindowTitle(self._dialog_title())

        layout = QVBoxLayout(self)

        self.toolbar = QToolBar()
        layout.addWidget(self.toolbar)

        self.find_bar = FindBar()
        self.find_bar.hide()
        self.find_bar.find_next_requested.connect(self._find_next)
        self.find_bar.find_previous_requested.connect(self._find_previous)
        self.find_bar.closed.connect(self._on_find_bar_closed)

        layout.addWidget(self.find_bar)
        layout.addWidget(self.editor, stretch=1)

        self.resize(1000, 700)

        self._create_actions()
        self._create_shortcuts()

    def _create_actions(self) -> None:
        save_action = QAction("Save", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.save)
        self.toolbar.addAction(save_action)

        save_as_action = QAction("Save As…", self)
        save_as_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        save_as_action.triggered.connect(self.save_as)
        self.toolbar.addAction(save_as_action)

        find_action = QAction("Find", self)
        find_action.setShortcut(QKeySequence.StandardKey.Find)
        find_action.triggered.connect(self.show_find_bar)
        self.toolbar.addAction(find_action)

    def _create_shortcuts(self) -> None:
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.show_find_bar)
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.save)
        QShortcut(QKeySequence("Ctrl+Shift+S"), self, activated=self.save_as)
        QShortcut(QKeySequence.StandardKey.Close, self, activated=self.close)

    def _dialog_title(self) -> str:
        name = self.path.name
        if self.editor.document().isModified():
            name = f"*{name}"
        return f"Editor - {name}"

    def _update_window_title(self) -> None:
        self.setWindowTitle(self._dialog_title())

    def show_find_bar(self) -> None:
        self.find_bar.show()
        self.find_bar.focus_on_query()

    def _on_find_bar_closed(self) -> None:
        self.editor.setFocus()

    def _find_next(self, text: str, case_sensitive: bool) -> None:
        self._find(text, backwards=False, case_sensitive=case_sensitive)

    def _find_previous(self, text: str, case_sensitive: bool) -> None:
        self._find(text, backwards=True, case_sensitive=case_sensitive)

    def _find(self, text: str, backwards: bool, case_sensitive: bool) -> None:
        if not text:
            return

        flags = QTextDocument.FindFlag(0)
        if backwards:
            flags |= QTextDocument.FindFlag.FindBackward
        if case_sensitive:
            flags |= QTextDocument.FindFlag.FindCaseSensitively

        if not self.editor.find(text, flags):
            cursor = self.editor.textCursor()
            move_op = (
                QTextCursor.MoveOperation.End
                if not backwards
                else QTextCursor.MoveOperation.Start
            )
            cursor.movePosition(move_op)
            self.editor.setTextCursor(cursor)
            self.editor.find(text, flags)

    def save(self) -> None:
        if self._write_to_file(self.path):
            self.editor.document().setModified(False)
            self._update_window_title()

    def save_as(self) -> None:
        directory = str(self.path.parent)
        new_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save File As",
            directory,
        )
        if new_path:
            self.path = Path(new_path)
            if self._write_to_file(self.path):
                self.editor.document().setModified(False)
                self._update_window_title()

    def _write_to_file(self, path: Path) -> bool:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.editor.toPlainText())
        except OSError as exc:
            QMessageBox.critical(self, "Error", f"Failed to save file:\n{exc}")
            return False
        return True

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._maybe_save_changes():
            super().closeEvent(event)
        else:
            event.ignore()

    def reject(self) -> None:  # noqa: D401 - Qt override without docstring
        if self._maybe_save_changes():
            super().reject()

    def _maybe_save_changes(self) -> bool:
        if not self.editor.document().isModified():
            return True

        response = QMessageBox.question(
            self,
            "Unsaved Changes",
            "The document has unsaved changes. Save before closing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )

        if response == QMessageBox.StandardButton.Save:
            self.save()
            return not self.editor.document().isModified()
        if response == QMessageBox.StandardButton.Discard:
            return True
        return False
