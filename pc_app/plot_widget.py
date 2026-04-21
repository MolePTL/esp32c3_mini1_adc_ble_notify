"""实时波形绘图控件模块。

这个文件负责把已经解析好的数据帧画成实时波形。
它不负责 BLE 通信，也不负责协议解析；它只关心一件事：
“给我结构化数据，我把它尽可能流畅地画出来”。

从桌面端整体链路看，它位于：

    BLE bytes
        -> protocol.parse_frame()
        -> AdcFrame
        -> MainWindow._handle_frame()
        -> RealtimePlotWidget.append_frame()
        -> 定时刷新真正绘制

除了最基础的实时显示，这个版本还补上了更像“示波器”的交互：
1. 自动跟随最新窗口
2. 自动/手动量程切换
3. 通道单独隐藏
4. 鼠标悬停读点、单击锁定读点
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import deque
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from pc_app.protocol import AdcFrame, CHANNEL_SPECS
from pc_app.vtem_processor import AdcProcessingResult


class RealtimePlotWidget(QWidget):
    """4 路电压波形显示控件。"""

    auto_follow_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None, max_points: int = 6000) -> None:
        super().__init__(parent)

        self._channel_labels = {key: label for key, label, _ in CHANNEL_SPECS}
        self._channel_attr_names = {key: attr_name for key, _, attr_name in CHANNEL_SPECS}

        # 为了支持 60s 级别窗口，缓存长度比原来的 1000 点更大。
        self._max_points = max_points
        self._x_values: deque[float] = deque(maxlen=max_points)
        self._frames: deque[AdcFrame] = deque(maxlen=max_points)
        self._processing_results: deque[AdcProcessingResult | None] = deque(maxlen=max_points)
        self._channels = {
            key: deque(maxlen=max_points) for key, _, _ in CHANNEL_SPECS
        }

        self._base_timestamp_ms: int | None = None
        self._last_timestamp_ms: int | None = None
        self._dirty = False
        self._live_updates_enabled = True

        self._visible_channels = {key: True for key, _, _ in CHANNEL_SPECS}
        self._window_duration_s = 10.0
        self._default_y_range = (0.0, 3.6)
        self._manual_y_range = self._default_y_range
        self._auto_follow_enabled = True
        self._auto_range_enabled = False
        self._suspend_manual_range_signal = False

        self._cursor_locked = False
        self._locked_frame_id: int | None = None

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.showGrid(x=True, y=True, alpha=0.25)
        self.plot_widget.addLegend()
        self.plot_widget.setLabel("left", "电压", units="V")
        self.plot_widget.setLabel("bottom", "时间", units="s")

        colors = {
            "vtem": "#E41A1C",
            "vm": "#377EB8",
            "va201": "#4DAF4A",
            "vbat": "#FF7F00",
        }
        self._curves = {
            key: self.plot_widget.plot(
                name=self._channel_labels[key],
                pen=pg.mkPen(color=colors[key], width=3),
            )
            for key, _, _ in CHANNEL_SPECS
        }

        self._cursor_line = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=pg.mkPen(color="#555555", width=1, style=Qt.DashLine),
        )
        self.plot_widget.addItem(self._cursor_line)
        self._cursor_line.hide()

        self._cursor_markers = {}
        for key, _, _ in CHANNEL_SPECS:
            marker = pg.ScatterPlotItem(
                size=9,
                brush=pg.mkBrush(colors[key]),
                pen=pg.mkPen(color="#202020", width=1),
            )
            self.plot_widget.addItem(marker)
            marker.hide()
            self._cursor_markers[key] = marker

        self._raw_readout_labels: dict[str, QLabel] = {}
        self._converted_readout_labels: dict[str, QLabel] = {}
        self._readout_status = QLabel("读点：暂无数据。")
        self._readout_panel = self._build_readout_panel()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot_widget)
        layout.addWidget(self._readout_panel)
        layout.addWidget(self._readout_status)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(33)
        self._refresh_timer.timeout.connect(self._refresh_plot)
        self._refresh_timer.start()

        view_box = self.plot_widget.getPlotItem().vb
        view_box.sigRangeChangedManually.connect(self._handle_manual_range_change)

        self._mouse_proxy = pg.SignalProxy(
            self.plot_widget.scene().sigMouseMoved,
            rateLimit=60,
            slot=self._handle_mouse_moved,
        )
        self.plot_widget.scene().sigMouseClicked.connect(self._handle_mouse_clicked)

        self._apply_manual_y_range()

    def append_frame(self, frame: AdcFrame, processing_result: AdcProcessingResult | None = None) -> None:
        """把一帧新数据追加到内部缓存。"""
        if self._last_timestamp_ms is not None and frame.timestamp_ms < self._last_timestamp_ms:
            self._reset_buffers()

        if self._base_timestamp_ms is None:
            self._base_timestamp_ms = frame.timestamp_ms

        x_value = (frame.timestamp_ms - self._base_timestamp_ms) / 1000.0
        self._x_values.append(x_value)
        self._frames.append(frame)
        self._processing_results.append(processing_result)

        for key, _, attr_name in CHANNEL_SPECS:
            if processing_result is not None:
                self._channels[key].append(processing_result.filtered_voltage_v(key))
            else:
                self._channels[key].append(getattr(frame, attr_name) / 1000.0)

        self._last_timestamp_ms = frame.timestamp_ms

        if self._cursor_locked and self._locked_frame_id is not None:
            locked_index = self._find_frame_index(self._locked_frame_id)
            if locked_index is None:
                self._cursor_locked = False
                self._locked_frame_id = None
                self._clear_cursor()
                self._readout_status.setText("读点：已锁定的采样点已滑出当前窗口。")
            else:
                self._show_cursor_at_index(locked_index, locked=True)
        elif not self._cursor_locked:
            self._update_readout_panel(frame, processing_result)
            self._readout_status.setText(f"实时：帧={frame.frame_id}，时间戳={frame.timestamp_ms} ms")

        self._dirty = True

    def set_live_updates_enabled(self, enabled: bool) -> None:
        """控制是否允许实时刷新图形。"""
        self._live_updates_enabled = enabled
        if enabled and self._dirty:
            self._refresh_plot(force=True)

    def set_channel_visibility(self, channel_key: str, visible: bool) -> None:
        """切换指定通道是否显示在图上。"""
        if channel_key not in self._visible_channels:
            return

        self._visible_channels[channel_key] = visible
        self._curves[channel_key].setVisible(visible)
        if not visible:
            self._cursor_markers[channel_key].hide()

        self._dirty = True
        self._refresh_plot(force=True)

    def set_window_duration_seconds(self, seconds: float) -> None:
        """设置自动跟随时的时间窗口长度。"""
        self._window_duration_s = max(0.5, float(seconds))
        if self._auto_follow_enabled:
            self._refresh_plot(force=True)

    def set_auto_follow_enabled(self, enabled: bool) -> None:
        """设置是否自动跟随最新数据。"""
        enabled = bool(enabled)
        if self._auto_follow_enabled == enabled:
            if enabled:
                self._refresh_plot(force=True)
            return

        self._auto_follow_enabled = enabled
        self.auto_follow_changed.emit(enabled)
        if enabled:
            self._refresh_plot(force=True)

    def set_auto_range_enabled(self, enabled: bool) -> None:
        """设置是否根据当前可见通道自动调整量程。"""
        self._auto_range_enabled = bool(enabled)
        self._refresh_plot(force=True)

    def set_manual_y_range(self, min_y: float, max_y: float) -> None:
        """设置手动量程。"""
        if min_y == max_y:
            max_y += 0.1

        self._manual_y_range = (min(min_y, max_y), max(min_y, max_y))
        if not self._auto_range_enabled:
            self._refresh_plot(force=True)

    def reset_manual_y_range(self) -> tuple[float, float]:
        """恢复默认量程，并返回默认值。"""
        self._manual_y_range = self._default_y_range
        if not self._auto_range_enabled:
            self._refresh_plot(force=True)
        return self._manual_y_range

    def show_all_data(self) -> None:
        """显示当前缓存中的全部时间范围。"""
        if not self._x_values:
            return

        if self._auto_follow_enabled:
            self._auto_follow_enabled = False
            self.auto_follow_changed.emit(False)

        self._apply_view_ranges(force_show_all=True)

    def clear_data(self) -> None:
        """清空当前波形缓存并立即刷新显示。"""
        self._reset_buffers()
        self._dirty = True
        self._refresh_plot(force=True)

    def _reset_buffers(self) -> None:
        """重置内部缓存与时间基准。"""
        self._x_values.clear()
        self._frames.clear()
        self._processing_results.clear()
        for channel in self._channels.values():
            channel.clear()

        self._base_timestamp_ms = None
        self._last_timestamp_ms = None
        self._cursor_locked = False
        self._locked_frame_id = None
        self._clear_cursor()
        self._clear_readout_panel()
        self._readout_status.setText("读点：暂无数据。")

    def _refresh_plot(self, force: bool = False) -> None:
        """把缓存中的数据真正更新到图上。"""
        if not self._dirty and not force:
            return

        if not self._live_updates_enabled and not force:
            return

        x_data = list(self._x_values)
        for key, curve in self._curves.items():
            curve.setVisible(self._visible_channels[key])
            if self._visible_channels[key]:
                curve.setData(x_data, list(self._channels[key]))
            else:
                curve.setData([], [])

        self._apply_view_ranges()
        self._dirty = False

    def _apply_view_ranges(self, force_show_all: bool = False) -> None:
        """根据当前模式更新 X/Y 轴范围。"""
        if not self._x_values:
            self._apply_manual_y_range()
            return

        if force_show_all:
            x_min = self._x_values[0]
            x_max = self._x_values[-1]
            if x_min == x_max:
                x_max = x_min + 0.1
            self._set_x_range(x_min, x_max)
        elif self._auto_follow_enabled:
            x_max = self._x_values[-1]
            x_min = max(self._x_values[0], x_max - self._window_duration_s)
            if x_min == x_max:
                x_max = x_min + 0.1
            self._set_x_range(x_min, x_max)

        if self._auto_range_enabled:
            self._apply_auto_y_range()
        else:
            self._apply_manual_y_range()

    def _apply_manual_y_range(self) -> None:
        """应用手动量程。"""
        y_min, y_max = self._manual_y_range
        if y_min == y_max:
            y_max = y_min + 0.1
        self._set_y_range(y_min, y_max)

    def _apply_auto_y_range(self) -> None:
        """根据当前 X 轴可见范围和通道可见性自动计算 Y 轴范围。"""
        visible_keys = [key for key, visible in self._visible_channels.items() if visible]
        if not visible_keys or not self._x_values:
            self._apply_manual_y_range()
            return

        view_box = self.plot_widget.getPlotItem().vb
        x_min, x_max = view_box.viewRange()[0]
        x_values = list(self._x_values)
        left = bisect_left(x_values, x_min)
        right = bisect_right(x_values, x_max)
        if right <= left:
            left = 0
            right = len(x_values)

        values_in_range: list[float] = []
        for key in visible_keys:
            values_in_range.extend(list(self._channels[key])[left:right])

        if not values_in_range:
            self._apply_manual_y_range()
            return

        y_min = min(values_in_range)
        y_max = max(values_in_range)
        span = y_max - y_min
        padding = max(0.05, span * 0.1)
        self._set_y_range(y_min - padding, y_max + padding)

    def _set_x_range(self, x_min: float, x_max: float) -> None:
        """在不触发“手动拖动”副作用的前提下更新 X 轴。"""
        self._suspend_manual_range_signal = True
        try:
            self.plot_widget.getPlotItem().vb.setXRange(x_min, x_max, padding=0.02)
        finally:
            self._suspend_manual_range_signal = False

    def _set_y_range(self, y_min: float, y_max: float) -> None:
        """在不触发“手动拖动”副作用的前提下更新 Y 轴。"""
        self._suspend_manual_range_signal = True
        try:
            self.plot_widget.getPlotItem().vb.setYRange(y_min, y_max, padding=0.02)
        finally:
            self._suspend_manual_range_signal = False

    def _handle_manual_range_change(self, _mask: object) -> None:
        """用户手动拖动/缩放时，关闭自动跟随。"""
        if self._suspend_manual_range_signal:
            return

        if self._auto_follow_enabled:
            self._auto_follow_enabled = False
            self.auto_follow_changed.emit(False)

        if self._auto_range_enabled:
            self._apply_auto_y_range()

    def _handle_mouse_moved(self, event: tuple[Any, ...]) -> None:
        """鼠标悬停时显示最近点读数。"""
        if self._cursor_locked or not self._frames:
            return

        scene_pos = event[0]
        view_box = self.plot_widget.getPlotItem().vb
        if not view_box.sceneBoundingRect().contains(scene_pos):
            self._clear_cursor()
            self._readout_status.setText("读点：鼠标悬停查看，单击锁定/解锁。")
            return

        plot_pos = view_box.mapSceneToView(scene_pos)
        nearest_index = self._find_nearest_index(plot_pos.x())
        if nearest_index is not None:
            self._show_cursor_at_index(nearest_index, locked=False)

    def _handle_mouse_clicked(self, event: Any) -> None:
        """左键单击锁定或解锁当前读点。"""
        if event.button() != Qt.LeftButton or not self._frames:
            return

        view_box = self.plot_widget.getPlotItem().vb
        scene_pos = event.scenePos()
        if not view_box.sceneBoundingRect().contains(scene_pos):
            return

        plot_pos = view_box.mapSceneToView(scene_pos)
        nearest_index = self._find_nearest_index(plot_pos.x())
        if nearest_index is None:
            return

        if self._cursor_locked:
            self._cursor_locked = False
            self._locked_frame_id = None
            self._show_cursor_at_index(nearest_index, locked=False)
            return

        self._cursor_locked = True
        self._locked_frame_id = self._frames[nearest_index].frame_id
        self._show_cursor_at_index(nearest_index, locked=True)

    def _find_nearest_index(self, x_value: float) -> int | None:
        """找到离指定 x 坐标最近的采样点。"""
        if not self._x_values:
            return None

        x_values = list(self._x_values)
        insert_at = bisect_left(x_values, x_value)
        if insert_at <= 0:
            return 0
        if insert_at >= len(x_values):
            return len(x_values) - 1

        left_index = insert_at - 1
        right_index = insert_at
        left_distance = abs(x_values[left_index] - x_value)
        right_distance = abs(x_values[right_index] - x_value)
        return left_index if left_distance <= right_distance else right_index

    def _find_frame_index(self, frame_id: int) -> int | None:
        """在当前窗口里查找某个帧号。"""
        for index, frame in enumerate(self._frames):
            if frame.frame_id == frame_id:
                return index
        return None

    def _show_cursor_at_index(self, index: int, locked: bool) -> None:
        """把读点光标移动到指定位置。"""
        if index < 0 or index >= len(self._frames):
            return

        x_value = self._x_values[index]
        frame = self._frames[index]
        processing_result = self._processing_results[index]
        self._cursor_line.setPos(x_value)
        self._cursor_line.show()

        for key, _, _ in CHANNEL_SPECS:
            if self._visible_channels[key]:
                self._cursor_markers[key].setData([x_value], [self._channels[key][index]])
                self._cursor_markers[key].show()
            else:
                self._cursor_markers[key].hide()

        state_text = "已锁定" if locked else "悬停"
        self._update_readout_panel(frame, processing_result)
        self._readout_status.setText(
            f"读点[{state_text}]：t={x_value:.3f} s，帧={frame.frame_id}，时间戳={frame.timestamp_ms} ms"
        )

    def _build_readout_panel(self) -> QWidget:
        panel = QWidget()
        layout = QGridLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(4)
        layout.addWidget(QLabel("通道"), 0, 0)
        layout.addWidget(QLabel("原始电压"), 0, 1)
        layout.addWidget(QLabel("转换/处理值"), 0, 2)

        for row, (channel_key, label, _) in enumerate(CHANNEL_SPECS, start=1):
            raw_label = QLabel("-")
            converted_label = QLabel("-")
            raw_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            converted_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            raw_label.setFixedWidth(140)
            converted_label.setFixedWidth(180)
            layout.addWidget(QLabel(label), row, 0)
            layout.addWidget(raw_label, row, 1)
            layout.addWidget(converted_label, row, 2)
            self._raw_readout_labels[channel_key] = raw_label
            self._converted_readout_labels[channel_key] = converted_label

        layout.setColumnStretch(3, 1)
        self._clear_readout_panel()
        return panel

    def _clear_readout_panel(self) -> None:
        for label in self._raw_readout_labels.values():
            label.setText("-")
        for label in self._converted_readout_labels.values():
            label.setText("-")

    def _update_readout_panel(
        self,
        frame: AdcFrame,
        processing_result: AdcProcessingResult | None,
    ) -> None:
        raw_values = {
            "vtem": frame.vtem_mv / 1000.0,
            "vm": frame.vm_mv / 1000.0,
            "va201": frame.va201_mv / 1000.0,
            "vbat": frame.vbat_mv / 1000.0,
        }

        for channel_key, voltage_v in raw_values.items():
            self._raw_readout_labels[channel_key].setText(self._format_voltage(voltage_v))

        if processing_result is None:
            _resistance_ohm, temperature_c = frame.try_vtem_pt1000_metrics()
            va201_resistance_ohm = frame.try_va201_resistance_ohm()
            vm_voltage_v = frame.vm_mv / 1000.0
            vbat_voltage_v = frame.vbat_source_voltage_v
        else:
            temperature_c = processing_result.vtem.compensated_temperature_c
            va201_resistance_ohm = processing_result.try_va201_resistance_ohm()
            vm_voltage_v = processing_result.filtered_voltage_v("vm")
            vbat_voltage_v = processing_result.vbat_source_voltage_v()

        self._converted_readout_labels["vtem"].setText(self._format_temperature(temperature_c))
        self._converted_readout_labels["vm"].setText(self._format_voltage(vm_voltage_v))
        self._converted_readout_labels["va201"].setText(self._format_resistance(va201_resistance_ohm))
        self._converted_readout_labels["vbat"].setText(self._format_voltage(vbat_voltage_v))

    @staticmethod
    def _format_voltage(voltage_v: float | None) -> str:
        return "-" if voltage_v is None else f"{voltage_v:8.3f} V"

    @staticmethod
    def _format_temperature(temperature_c: float | None) -> str:
        return "-" if temperature_c is None else f"{temperature_c:8.2f} °C"

    def _format_resistance(self, resistance_ohm: float | None) -> str:
        if resistance_ohm is None:
            return "-"
        if resistance_ohm >= 1_000_000.0:
            return f"{resistance_ohm / 1_000_000.0:.3f} MΩ"
        if resistance_ohm >= 1_000.0:
            return f"{resistance_ohm / 1_000.0:.3f} kΩ"
        return f"{resistance_ohm:.1f} Ω"

    def _clear_cursor(self) -> None:
        """隐藏当前读点光标。"""
        self._cursor_line.hide()
        for marker in self._cursor_markers.values():
            marker.hide()
