from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QHBoxLayout,
)


class ZipViewerDialog(QDialog):
    """Dialog that allows browsing and extracting entries from a zip archive."""

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._path = Path(path)
        self._temp_dirs: list[str] = []

        try:
            self._archive = zipfile.ZipFile(self._path)
        except (OSError, zipfile.BadZipFile) as exc:
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to open zip file:\n{exc}",
            )
            self.setResult(QDialog.DialogCode.Rejected)
            self.close()
            return

        self.setWindowTitle(f"Zip Contents - {self._path.name}")

        layout = QVBoxLayout(self)

        self._list = QListWidget()
        self._list.addItems(self._archive.namelist())
        self._list.itemDoubleClicked.connect(self._open_item)
        layout.addWidget(self._list, stretch=1)

        button_row = QHBoxLayout()

        self._open_button = QPushButton("Open Selected")
        self._open_button.clicked.connect(self._open_selected)
        button_row.addWidget(self._open_button)

        self._extract_selected_button = QPushButton("Extract Selected…")
        self._extract_selected_button.clicked.connect(self._extract_selected)
        button_row.addWidget(self._extract_selected_button)

        self._extract_all_button = QPushButton("Extract All…")
        self._extract_all_button.clicked.connect(self._extract_all)
        button_row.addWidget(self._extract_all_button)

        button_row.addStretch(1)

        layout.addLayout(button_row)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self.resize(600, 400)

    def _selected_items(self) -> list[str]:
        items = self._list.selectedItems()
        if items:
            return [item.text() for item in items]
        current = self._list.currentItem()
        return [current.text()] if isinstance(current, QListWidgetItem) else []

    def _open_selected(self) -> None:
        entries = self._selected_items()
        if not entries:
            return
        for entry in entries:
            self._open_entry(entry)

    def _open_item(self, item: QListWidgetItem) -> None:
        self._open_entry(item.text())

    def _open_entry(self, entry: str) -> None:
        if entry.endswith("/"):
            return  # Directories cannot be opened directly
        try:
            temp_dir = tempfile.mkdtemp(prefix="dirshell_zip_")
            self._temp_dirs.append(temp_dir)
            target_path = os.path.join(temp_dir, Path(entry).name)
            with self._archive.open(entry) as source, open(target_path, "wb") as fh:
                fh.write(source.read())
        except OSError as exc:
            QMessageBox.critical(self, "Error", f"Failed to open entry:\n{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(target_path))

    def _extract_selected(self) -> None:
        entries = self._selected_items()
        if not entries:
            return
        destination = QFileDialog.getExistingDirectory(
            self,
            "Extract Selected Files",
            str(self._path.parent),
        )
        if destination:
            self._extract_entries(entries, destination)

    def _extract_all(self) -> None:
        destination = QFileDialog.getExistingDirectory(
            self,
            "Extract All Files",
            str(self._path.parent),
        )
        if destination:
            self._extract_entries(self._archive.namelist(), destination)

    def _extract_entries(self, entries: list[str], destination: str) -> None:
        try:
            for entry in entries:
                self._archive.extract(entry, path=destination)
        except (OSError, zipfile.BadZipFile) as exc:
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to extract files:\n{exc}",
            )
            return
        QMessageBox.information(
            self,
            "Extraction Complete",
            f"Extracted {len(entries)} item(s) to '{destination}'.",
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().closeEvent(event)
        for temp_dir in self._temp_dirs:
            try:
                for entry in os.listdir(temp_dir):
                    os.remove(os.path.join(temp_dir, entry))
                os.rmdir(temp_dir)
            except OSError:
                pass
        self._archive.close()
