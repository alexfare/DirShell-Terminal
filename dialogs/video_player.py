from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)


class VideoPlayerDialog(QDialog):
    """Dialog that plays video files using the Qt multimedia backend."""

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._path = Path(path)

        if not self._path.is_file():
            QMessageBox.warning(
                self,
                "Unable to Open Video",
                f"The file '{self._path.name}' could not be found.",
            )
            self.setResult(QDialog.DialogCode.Rejected)
            self.close()
            return

        self.setWindowTitle(self._path.name)

        layout = QVBoxLayout(self)

        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._video_widget = QVideoWidget(self)
        self._player.setVideoOutput(self._video_widget)

        layout.addWidget(self._video_widget, stretch=1)

        controls = QWidget(self)
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)

        self._play_button = QPushButton("Play")
        self._play_button.clicked.connect(self._toggle_playback)
        controls_layout.addWidget(self._play_button)

        self._position_slider = QSlider(Qt.Orientation.Horizontal)
        self._position_slider.setRange(0, 0)
        self._position_slider.sliderMoved.connect(self._player.setPosition)
        controls_layout.addWidget(self._position_slider, stretch=1)

        self._time_label = QLabel("00:00 / 00:00")
        controls_layout.addWidget(self._time_label)

        layout.addWidget(controls)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self.resize(960, 540)

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.errorOccurred.connect(self._on_error)

        self._player.setSource(QUrl.fromLocalFile(str(self._path)))
        self._player.play()

    def _toggle_playback(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _format_time(self, milliseconds: int) -> str:
        seconds = max(milliseconds // 1000, 0)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _update_time_label(self, position: int, duration: int) -> None:
        current_text = self._format_time(position)
        total_text = self._format_time(duration)
        self._time_label.setText(f"{current_text} / {total_text}")

    def _on_position_changed(self, position: int) -> None:
        if not self._position_slider.isSliderDown():
            self._position_slider.setValue(position)
        self._update_time_label(position, self._player.duration())

    def _on_duration_changed(self, duration: int) -> None:
        self._position_slider.setRange(0, duration)
        self._update_time_label(self._player.position(), duration)

    def _on_playback_state_changed(
        self, state: QMediaPlayer.PlaybackState
    ) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._play_button.setText("Pause")
        else:
            self._play_button.setText("Play")

    def _on_error(self, error: QMediaPlayer.Error, error_string: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        QMessageBox.critical(
            self,
            "Playback Error",
            f"Could not play '{self._path.name}':\n{error_string}",
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._player.stop()
        super().closeEvent(event)
