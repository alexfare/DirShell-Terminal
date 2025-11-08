from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class PhotoViewerDialog(QDialog):
    """Dialog that displays an image scaled to the available window space."""

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._path = Path(path)
        self._pixmap = QPixmap(str(self._path))

        if self._pixmap.isNull():
            QMessageBox.warning(
                self,
                "Unable to Open Image",
                f"The image '{self._path.name}' could not be loaded.",
            )
            self.setResult(QDialog.DialogCode.Rejected)
            self.close()
            return

        self.setWindowTitle(self._path.name)

        layout = QVBoxLayout(self)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        layout.addWidget(self._scroll_area, stretch=1)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)

        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        container_layout.addWidget(self._image_label)

        self._scroll_area.setWidget(container)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self.resize(
            min(self._pixmap.width() + 50, 1000),
            min(self._pixmap.height() + 50, 800),
        )
        self._update_pixmap()

    def _update_pixmap(self) -> None:
        if self._pixmap.isNull():
            return
        viewport = self._scroll_area.viewport().size()
        scaled = self._pixmap.scaled(
            viewport,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(scaled)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._update_pixmap()
