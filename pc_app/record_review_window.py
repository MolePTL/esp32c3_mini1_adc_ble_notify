"""实时记录 CSV 回看窗口。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget

from pc_app.csv_importer import ImportedCsvSeries, load_imported_csv_series
from pc_app.plot_widget import RealtimePlotWidget
from pc_app.protocol import CHANNEL_SPECS


class RecordReviewWindow(QMainWindow):
    """只读查看实时记录 CSV 中已经落盘的 10Hz 聚合数据。"""

    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowTitle("记录回看")
        self.resize(1100, 720)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        self._csv_paths: list[Path] = []
        self._has_loaded_series = False

        central = QWidget(self)
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        toolbar = QHBoxLayout()

        self.refresh_button = QPushButton("刷新")
        self.show_all_button = QPushButton("显示全部")
        self.status_label = QLabel("记录回看：暂无记录文件。")
        self.status_label.setWordWrap(True)

        toolbar.addWidget(self.refresh_button)
        toolbar.addWidget(self.show_all_button)
        toolbar.addWidget(self.status_label, stretch=1)

        self.plot_widget = RealtimePlotWidget(max_points=200000)
        self.plot_widget.set_refresh_interval_ms(100)

        layout.addLayout(toolbar)
        layout.addWidget(self.plot_widget, stretch=1)

        self.refresh_button.clicked.connect(lambda: self.refresh_now(show_all=False))
        self.show_all_button.clicked.connect(lambda: self.refresh_now(show_all=True))

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(lambda: self.refresh_now(show_all=False))
        self._refresh_timer.start()

    def set_csv_paths(self, csv_paths: list[Path], reset_view: bool = False) -> None:
        normalized_paths = [Path(path) for path in csv_paths]
        if normalized_paths == self._csv_paths and not reset_view:
            return

        self._csv_paths = normalized_paths
        if reset_view:
            self._has_loaded_series = False
            self.plot_widget.clear_data()

        self.refresh_now(show_all=(reset_view or not self._has_loaded_series))

    def refresh_now(self, show_all: bool = False) -> None:
        if not self._csv_paths:
            self._has_loaded_series = False
            self.plot_widget.clear_data()
            self.status_label.setText("记录回看：暂无记录文件。")
            return

        try:
            series = load_imported_csv_series(self._csv_paths, prefer_filtered=True)
        except ValueError as exc:
            if not self._has_loaded_series:
                self.plot_widget.clear_data()
            self.status_label.setText(f"记录回看：{exc}")
            return
        except OSError as exc:
            self.status_label.setText(f"记录回看：读取 CSV 失败：{exc}")
            return

        self._apply_series(series, show_all=(show_all or not self._has_loaded_series))
        self._has_loaded_series = True

    def _apply_series(self, series: ImportedCsvSeries, show_all: bool) -> None:
        self.plot_widget.load_sample_series(
            series.x_values,
            series.raw_channels,
            series.plot_channels,
            series.frame_ids,
            series.timestamp_ms_values,
            series.pc_recv_time_texts,
            show_all=show_all,
        )

        available_channels = set(series.available_channels)
        for channel_key, _, _ in CHANNEL_SPECS:
            self.plot_widget.set_channel_visibility(channel_key, channel_key in available_channels)

        self.status_label.setText(
            "记录回看：文件 {files} 个，绘制 {rows} 行，跳过 {skipped} 行，重复 {duplicates} 行，"
            "横轴：{axis}，数值：{mode}。".format(
                files=series.file_count,
                rows=series.imported_row_count,
                skipped=series.skipped_row_count,
                duplicates=series.duplicate_row_count,
                axis=series.time_axis_label,
                mode=series.value_mode_label,
            )
        )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._refresh_timer.stop()
        self.closed.emit()
        super().closeEvent(event)
