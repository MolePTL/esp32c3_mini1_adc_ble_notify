"""主窗口模块。

这个文件是整个桌面上位机工程的“调度中心”。
当前版本除了原有的 BLE 接收、状态显示、CSV 导出之外，
还把示波器交互升级成了更接近仪器工具的结构：
- 自动跟随 / 显示全部
- 自动量程 / 手动量程
- 通道单独显示
- 通道单独导出
- 实时保存
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from pc_app.ble_client import BleClientBridge
from pc_app.csv_importer import ImportedCsvSeries, load_imported_csv_series
from pc_app.data_logger import DataLogger
from pc_app.plot_widget import RealtimePlotWidget
from pc_app.protocol import (
    CHANNEL_SPECS,
    DEFAULT_CHARACTERISTIC_UUID,
    DEFAULT_DEVICE_NAME,
    DEFAULT_SERVICE_UUID,
    EXPECTED_PROTOCOL_VERSION,
    PT1000_DIVIDER_SERIES_OHM,
    PT1000_DIVIDER_SUPPLY_V,
    AdcFrame,
)
from pc_app.vtem_processor import (
    SPIKE_FILTER_HOLD,
    SPIKE_FILTER_LABELS,
    SPIKE_FILTER_MEDIAN3,
    SPIKE_FILTER_MEDIAN5,
    SPIKE_FILTER_NONE,
    AdcProcessor,
)


class MainWindow(QMainWindow):
    """桌面上位机主窗口类。"""

    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle("ESP32-C3 BLE 采集上位机")
        self.resize(1460, 900)

        self._display_enabled = True
        self._is_connected = False
        self._devices: list[dict] = []

        self.logger = DataLogger()
        self.ble = BleClientBridge(self)
        self.adc_processor = AdcProcessor(default_ema_cutoff_hz=5.0)
        self.realtime_save_timer = QTimer(self)
        self._realtime_save_segment_index = 0
        self._realtime_save_channels: tuple[str, ...] = ()

        self.display_channel_checkboxes: dict[str, QCheckBox] = {}
        self.save_channel_checkboxes: dict[str, QCheckBox] = {}
        self.filter_channel_combos: dict[str, QComboBox] = {}

        self._build_ui()
        self._connect_signals()
        self._apply_initial_scope_settings()

        self._set_connected(False, "", "")
        self._update_stats(
            {
                "connected": False,
                "device_name": "",
                "device_address": "",
                "valid_frames": 0,
                "invalid_frames": 0,
                "frame_rate": 0.0,
                "last_frame_id": None,
                "last_timestamp_ms": None,
                "last_error": "",
            }
        )

        self._append_log("上位机已启动。")
        self._append_log(f"默认设备名: {DEFAULT_DEVICE_NAME}")
        self._append_log(f"Service UUID: {DEFAULT_SERVICE_UUID}")
        self._append_log(f"Notify UUID: {DEFAULT_CHARACTERISTIC_UUID}")
        self._append_log(f"协议版本: 0x{EXPECTED_PROTOCOL_VERSION:02X}（通道数据单位为 mV）")
        self._append_log(
            f"VTEM 温度换算: {PT1000_DIVIDER_SUPPLY_V:.1f}V -> {PT1000_DIVIDER_SERIES_OHM:.0f}Ω -> VTEM -> PT1000 -> GND"
        )

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        root_layout = QVBoxLayout(central)
        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)

        scan_group = QGroupBox("设备扫描")
        scan_layout = QVBoxLayout(scan_group)
        self.scan_button = QPushButton("扫描设备")
        self.connect_button = QPushButton("连接")
        self.disconnect_button = QPushButton("断开连接")
        self.device_list = QListWidget()

        scan_buttons = QHBoxLayout()
        scan_buttons.addWidget(self.scan_button)
        scan_buttons.addWidget(self.connect_button)
        scan_buttons.addWidget(self.disconnect_button)
        scan_layout.addLayout(scan_buttons)
        scan_layout.addWidget(self.device_list)

        status_group = QGroupBox("运行状态")
        status_layout = QFormLayout(status_group)
        self.device_name_label = QLabel("-")
        self.device_address_label = QLabel("-")
        self.connected_label = QLabel("否")
        self.last_frame_label = QLabel("-")
        self.last_timestamp_label = QLabel("-")
        self.frame_rate_label = QLabel("0.0 帧/秒")
        self.valid_frames_label = QLabel("0")
        self.invalid_frames_label = QLabel("0")
        self.last_error_label = QLabel("-")
        self.last_error_label.setWordWrap(True)

        status_layout.addRow("设备名", self.device_name_label)
        status_layout.addRow("设备地址", self.device_address_label)
        status_layout.addRow("已连接", self.connected_label)
        status_layout.addRow("最近帧序号", self.last_frame_label)
        status_layout.addRow("最近时间戳", self.last_timestamp_label)
        status_layout.addRow("接收速率", self.frame_rate_label)
        status_layout.addRow("有效帧数", self.valid_frames_label)
        status_layout.addRow("无效帧数", self.invalid_frames_label)
        status_layout.addRow("最近错误", self.last_error_label)

        control_group = QGroupBox("数据操作")
        control_layout = QVBoxLayout(control_group)
        top_row = QHBoxLayout()
        bottom_row = QHBoxLayout()
        import_row = QHBoxLayout()
        self.record_button = QPushButton("开始记录")
        self.save_csv_button = QPushButton("导出 CSV")
        self.import_csv_button = QPushButton("导入 CSV")
        self.import_folder_button = QPushButton("导入文件夹")
        self.import_prefer_filtered_checkbox = QCheckBox("导入优先滤波列")
        self.import_prefer_filtered_checkbox.setChecked(True)
        self.clear_display_button = QPushButton("清空显示")
        self.clear_cache_button = QPushButton("清空缓存")
        self.exit_button = QPushButton("安全退出")
        self.record_status_label = QLabel("记录状态：未开启。")
        self.record_status_label.setWordWrap(True)
        self.save_interval_spin = QDoubleSpinBox()
        self.save_interval_spin.setRange(1.0, 3600.0)
        self.save_interval_spin.setDecimals(0)
        self.save_interval_spin.setSingleStep(10.0)
        self.save_interval_spin.setValue(60.0)
        self.save_interval_spin.setSuffix(" s")

        top_row.addWidget(self.record_button)
        top_row.addWidget(self.save_csv_button)
        import_row.addWidget(self.import_csv_button)
        import_row.addWidget(self.import_folder_button)
        import_row.addWidget(self.import_prefer_filtered_checkbox)
        import_row.addStretch(1)
        bottom_row.addWidget(self.clear_display_button)
        bottom_row.addWidget(self.clear_cache_button)
        bottom_row.addWidget(self.exit_button)
        save_interval_row = QHBoxLayout()
        save_interval_row.addWidget(QLabel("定时保存间隔"))
        save_interval_row.addWidget(self.save_interval_spin)
        save_interval_row.addStretch(1)
        control_layout.addLayout(top_row)
        control_layout.addLayout(import_row)
        control_layout.addLayout(bottom_row)
        control_layout.addLayout(save_interval_row)
        control_layout.addWidget(self.record_status_label)

        log_group = QGroupBox("调试日志")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        log_layout.addWidget(self.log_view)

        left_layout.addWidget(scan_group)
        left_layout.addWidget(status_group)
        left_layout.addWidget(control_group)
        left_layout.addWidget(log_group, stretch=1)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        scope_group = QGroupBox("示波器控制")
        scope_layout = QVBoxLayout(scope_group)

        view_row = QHBoxLayout()
        self.toggle_display_button = QPushButton("暂停刷新")
        self.auto_follow_checkbox = QCheckBox("自动跟随")
        self.auto_follow_checkbox.setChecked(True)
        self.auto_range_checkbox = QCheckBox("自动量程")
        self.show_all_button = QPushButton("显示全部")
        view_row.addWidget(self.toggle_display_button)
        view_row.addWidget(self.auto_follow_checkbox)
        view_row.addWidget(self.auto_range_checkbox)
        view_row.addWidget(self.show_all_button)
        view_row.addStretch(1)

        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("时间窗口"))
        self.window_length_combo = QComboBox()
        self.window_length_combo.addItem("5 秒", 5.0)
        self.window_length_combo.addItem("10 秒", 10.0)
        self.window_length_combo.addItem("30 秒", 30.0)
        self.window_length_combo.addItem("60 秒", 60.0)
        self.window_length_combo.setCurrentIndex(1)
        range_row.addWidget(self.window_length_combo)
        range_row.addSpacing(12)
        range_row.addWidget(QLabel("Y 最小值"))
        self.y_min_spin = QDoubleSpinBox()
        self.y_min_spin.setRange(-100.0, 100.0)
        self.y_min_spin.setDecimals(3)
        self.y_min_spin.setSingleStep(0.1)
        self.y_min_spin.setValue(0.0)
        range_row.addWidget(self.y_min_spin)
        range_row.addWidget(QLabel("Y 最大值"))
        self.y_max_spin = QDoubleSpinBox()
        self.y_max_spin.setRange(-100.0, 100.0)
        self.y_max_spin.setDecimals(3)
        self.y_max_spin.setSingleStep(0.1)
        self.y_max_spin.setValue(3.6)
        range_row.addWidget(self.y_max_spin)
        self.restore_y_range_button = QPushButton("恢复默认量程")
        range_row.addWidget(self.restore_y_range_button)
        range_row.addStretch(1)

        display_row = QHBoxLayout()
        display_row.addWidget(QLabel("显示通道"))
        for channel_key, label, _ in CHANNEL_SPECS:
            checkbox = QCheckBox(label)
            checkbox.setChecked(True)
            self.display_channel_checkboxes[channel_key] = checkbox
            display_row.addWidget(checkbox)
        self.show_all_channels_button = QPushButton("全选显示")
        self.hide_all_channels_button = QPushButton("全部隐藏")
        display_row.addWidget(self.show_all_channels_button)
        display_row.addWidget(self.hide_all_channels_button)
        display_row.addStretch(1)

        save_row = QHBoxLayout()
        save_row.addWidget(QLabel("保存通道"))
        for channel_key, label, _ in CHANNEL_SPECS:
            checkbox = QCheckBox(label)
            checkbox.setChecked(True)
            self.save_channel_checkboxes[channel_key] = checkbox
            save_row.addWidget(checkbox)
        self.save_follow_display_checkbox = QCheckBox("保存跟随当前显示")
        self.save_follow_display_checkbox.setChecked(True)
        save_row.addWidget(self.save_follow_display_checkbox)
        save_row.addStretch(1)

        filter_grid = QGridLayout()
        filter_grid.addWidget(QLabel("去尖峰"), 0, 0)
        self.spike_filter_combo = QComboBox()
        for mode in (
            SPIKE_FILTER_NONE,
            SPIKE_FILTER_MEDIAN3,
            SPIKE_FILTER_MEDIAN5,
            SPIKE_FILTER_HOLD,
        ):
            self.spike_filter_combo.addItem(SPIKE_FILTER_LABELS[mode], mode)
        self.spike_filter_combo.setCurrentIndex(1)
        filter_grid.addWidget(self.spike_filter_combo, 0, 1)

        for column, (channel_key, label, _) in enumerate(CHANNEL_SPECS):
            filter_grid.addWidget(QLabel(f"{label} EMA"), 1, column * 2)
            combo = QComboBox()
            combo.addItem("关闭", None)
            combo.addItem("2 Hz", 2.0)
            combo.addItem("5 Hz", 5.0)
            combo.addItem("10 Hz", 10.0)
            combo.setCurrentIndex(2)
            self.filter_channel_combos[channel_key] = combo
            filter_grid.addWidget(combo, 1, column * 2 + 1)

        vtem_row = QHBoxLayout()
        self.vtem_wire_comp_checkbox = QCheckBox("导线补偿")
        self.vtem_wire_comp_spin = QDoubleSpinBox()
        self.vtem_wire_comp_spin.setRange(0.0, 100.0)
        self.vtem_wire_comp_spin.setDecimals(3)
        self.vtem_wire_comp_spin.setSingleStep(0.1)
        self.vtem_wire_comp_spin.setSuffix(" Ω")
        self.vtem_wire_comp_spin.setEnabled(False)
        vtem_row.addWidget(self.vtem_wire_comp_checkbox)
        vtem_row.addWidget(QLabel("导线总电阻"))
        vtem_row.addWidget(self.vtem_wire_comp_spin)
        vtem_row.addStretch(1)

        scope_layout.addLayout(view_row)
        scope_layout.addLayout(range_row)
        scope_layout.addLayout(display_row)
        scope_layout.addLayout(save_row)
        scope_layout.addLayout(filter_grid)
        scope_layout.addLayout(vtem_row)

        self.plot_widget = RealtimePlotWidget()
        right_layout.addWidget(scope_group)
        right_layout.addWidget(self.plot_widget, stretch=1)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([460, 980])

    def _connect_signals(self) -> None:
        self.scan_button.clicked.connect(self.ble.scan_devices)
        self.connect_button.clicked.connect(self._connect_selected_device)
        self.disconnect_button.clicked.connect(self.ble.disconnect_device)

        self.record_button.clicked.connect(self._toggle_realtime_save)
        self.save_csv_button.clicked.connect(self._save_csv)
        self.import_csv_button.clicked.connect(self._import_csv_files)
        self.import_folder_button.clicked.connect(self._import_csv_folder)
        self.clear_display_button.clicked.connect(self._clear_display)
        self.clear_cache_button.clicked.connect(self._clear_unsaved_cache)
        self.exit_button.clicked.connect(self._request_safe_exit)
        self.realtime_save_timer.timeout.connect(self._rotate_realtime_save_file)

        self.toggle_display_button.clicked.connect(self._toggle_display)
        self.auto_follow_checkbox.toggled.connect(self.plot_widget.set_auto_follow_enabled)
        self.auto_range_checkbox.toggled.connect(self._handle_auto_range_toggled)
        self.show_all_button.clicked.connect(self._show_all_data)
        self.window_length_combo.currentIndexChanged.connect(self._handle_window_length_changed)
        self.y_min_spin.valueChanged.connect(self._handle_manual_y_range_changed)
        self.y_max_spin.valueChanged.connect(self._handle_manual_y_range_changed)
        self.restore_y_range_button.clicked.connect(self._restore_default_y_range)
        self.show_all_channels_button.clicked.connect(self._show_all_channels)
        self.hide_all_channels_button.clicked.connect(self._hide_all_channels)
        self.save_follow_display_checkbox.toggled.connect(self._handle_save_follow_display_toggled)
        self.spike_filter_combo.currentIndexChanged.connect(self._handle_filter_settings_changed)
        self.vtem_wire_comp_checkbox.toggled.connect(self._handle_vtem_wire_compensation_changed)
        self.vtem_wire_comp_spin.valueChanged.connect(self._handle_vtem_wire_compensation_changed)
        self.plot_widget.auto_follow_changed.connect(self._sync_auto_follow_checkbox)

        for combo in self.filter_channel_combos.values():
            combo.currentIndexChanged.connect(self._handle_filter_settings_changed)

        for channel_key, checkbox in self.display_channel_checkboxes.items():
            checkbox.toggled.connect(
                lambda checked, key=channel_key: self._handle_display_channel_toggled(key, checked)
            )

        for channel_key, checkbox in self.save_channel_checkboxes.items():
            checkbox.toggled.connect(
                lambda _checked, key=channel_key: self._handle_save_channel_toggled(key)
            )

        self.ble.devices_updated.connect(self._update_device_list)
        self.ble.connection_state_changed.connect(self._set_connected)
        self.ble.frame_received.connect(self._handle_frame)
        self.ble.stats_updated.connect(self._update_stats)
        self.ble.log_message.connect(self._append_log)
        self.ble.error_occurred.connect(self._handle_error)

    def _apply_initial_scope_settings(self) -> None:
        self.plot_widget.set_window_duration_seconds(self.window_length_combo.currentData())
        self.plot_widget.set_auto_follow_enabled(self.auto_follow_checkbox.isChecked())
        self.plot_widget.set_auto_range_enabled(self.auto_range_checkbox.isChecked())
        for channel_key, checkbox in self.display_channel_checkboxes.items():
            self.plot_widget.set_channel_visibility(channel_key, checkbox.isChecked())
        self._sync_save_channels_with_display()
        self._apply_filter_settings(reset_filter=True)
        self._update_y_range_controls()
        self._update_record_ui()

    def _connect_selected_device(self) -> None:
        item = self.device_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "未选择设备", "请先在列表中选择一个 BLE 设备。")
            return

        address = item.data(Qt.UserRole)
        self.ble.connect_device(address)

    def _update_device_list(self, devices: list[dict]) -> None:
        self._devices = devices
        self.device_list.clear()

        for device in devices:
            rssi_text = f" RSSI={device['rssi']}" if device.get("rssi") is not None else ""
            label = f"{device['name']}  [{device['address']}]" + rssi_text
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, device["address"])
            self.device_list.addItem(item)

        if devices:
            self.device_list.setCurrentRow(0)

    def _set_connected(self, connected: bool, device_name: str, device_address: str) -> None:
        was_connected = self._is_connected
        self._is_connected = connected
        self.connected_label.setText("是" if connected else "否")
        self.device_name_label.setText(device_name or "-")
        self.device_address_label.setText(device_address or "-")

        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)

        if connected and not was_connected:
            self.adc_processor.reset()
            self.plot_widget.clear_data()
            self._set_display_enabled(True)
        elif not connected and was_connected:
            self.logger.end_session()
            self.plot_widget.clear_data()
            self._set_display_enabled(True)

    def _handle_frame(self, frame: AdcFrame) -> None:
        processing_result = self.adc_processor.process(frame)
        self.logger.append(frame, processing_result)
        self.plot_widget.append_frame(frame, processing_result)

    def _update_stats(self, stats: dict) -> None:
        self.frame_rate_label.setText(f"{stats.get('frame_rate', 0.0):.1f} 帧/秒")
        self.valid_frames_label.setText(str(stats.get("valid_frames", 0)))
        self.invalid_frames_label.setText(str(stats.get("invalid_frames", 0)))

        last_frame = stats.get("last_frame_id")
        last_timestamp = stats.get("last_timestamp_ms")
        self.last_frame_label.setText("-" if last_frame is None else str(last_frame))
        self.last_timestamp_label.setText("-" if last_timestamp is None else f"{last_timestamp} ms")

        last_error = stats.get("last_error") or "-"
        self.last_error_label.setText(last_error)

    def _toggle_display(self) -> None:
        self._set_display_enabled(not self._display_enabled)
        state = "已开启" if self._display_enabled else "已暂停"
        self._append_log(f"波形刷新{state}。")

    def _set_display_enabled(self, enabled: bool) -> None:
        self._display_enabled = enabled
        self.plot_widget.set_live_updates_enabled(enabled)
        self.toggle_display_button.setText("暂停刷新" if enabled else "恢复刷新")

    def _handle_auto_range_toggled(self, enabled: bool) -> None:
        self.plot_widget.set_auto_range_enabled(enabled)
        self._update_y_range_controls()
        self._append_log("波形量程已切换为自动模式。" if enabled else "波形量程已切换为手动模式。")

    def _handle_window_length_changed(self) -> None:
        seconds = self.window_length_combo.currentData()
        self.plot_widget.set_window_duration_seconds(seconds)

    def _handle_manual_y_range_changed(self) -> None:
        if self.auto_range_checkbox.isChecked():
            return

        min_value = self.y_min_spin.value()
        max_value = self.y_max_spin.value()
        if min_value >= max_value:
            if self.sender() is self.y_min_spin:
                max_value = min_value + 0.1
                self._set_spin_value(self.y_max_spin, max_value)
            else:
                min_value = max_value - 0.1
                self._set_spin_value(self.y_min_spin, min_value)

        self.plot_widget.set_manual_y_range(min_value, max_value)

    def _restore_default_y_range(self) -> None:
        min_value, max_value = self.plot_widget.reset_manual_y_range()
        self._set_spin_value(self.y_min_spin, min_value)
        self._set_spin_value(self.y_max_spin, max_value)

    def _show_all_channels(self) -> None:
        for checkbox in self.display_channel_checkboxes.values():
            checkbox.setChecked(True)

    def _hide_all_channels(self) -> None:
        for checkbox in self.display_channel_checkboxes.values():
            checkbox.setChecked(False)

    def _handle_display_channel_toggled(self, channel_key: str, checked: bool) -> None:
        self.plot_widget.set_channel_visibility(channel_key, checked)
        self._sync_save_channels_with_display()

    def _handle_save_channel_toggled(self, _channel_key: str) -> None:
        # 保存通道是“导出/实时保存过滤”配置，不需要即时刷新其他视图。
        return

    def _handle_save_follow_display_toggled(self, checked: bool) -> None:
        if checked:
            self._sync_save_channels_with_display()
        self._refresh_save_channel_controls()

    def _handle_filter_settings_changed(self) -> None:
        self._apply_filter_settings(reset_filter=True)
        self.plot_widget.clear_data()
        spike_mode = SPIKE_FILTER_LABELS[self.adc_processor.spike_filter_mode]
        self._append_log(f"滤波设置已切换：{spike_mode}，滤波状态已重置。")

    def _handle_vtem_wire_compensation_changed(self) -> None:
        self._apply_filter_settings(reset_filter=False)

    def _apply_filter_settings(self, reset_filter: bool) -> None:
        spike_mode = self.spike_filter_combo.currentData()
        wire_compensation_ohm = (
            self.vtem_wire_comp_spin.value() if self.vtem_wire_comp_checkbox.isChecked() else 0.0
        )

        if reset_filter or spike_mode != self.adc_processor.spike_filter_mode:
            self.adc_processor.set_spike_filter_mode(spike_mode)

        for channel_key, combo in self.filter_channel_combos.items():
            cutoff_hz = combo.currentData()
            if reset_filter or cutoff_hz != self.adc_processor.ema_cutoff_hz(channel_key):
                self.adc_processor.set_channel_ema_cutoff_hz(channel_key, cutoff_hz)

        self.adc_processor.set_wire_compensation_ohm(wire_compensation_ohm)
        self.vtem_wire_comp_spin.setEnabled(self.vtem_wire_comp_checkbox.isChecked())

    def _sync_auto_follow_checkbox(self, enabled: bool) -> None:
        previous = self.auto_follow_checkbox.blockSignals(True)
        self.auto_follow_checkbox.setChecked(enabled)
        self.auto_follow_checkbox.blockSignals(previous)

    def _sync_save_channels_with_display(self) -> None:
        if not self.save_follow_display_checkbox.isChecked():
            return

        for channel_key, checkbox in self.save_channel_checkboxes.items():
            previous = checkbox.blockSignals(True)
            checkbox.setChecked(self.display_channel_checkboxes[channel_key].isChecked())
            checkbox.blockSignals(previous)

    def _refresh_save_channel_controls(self) -> None:
        locked_by_recording = self.logger.is_realtime_save_active
        follow_display = self.save_follow_display_checkbox.isChecked()

        self.save_follow_display_checkbox.setEnabled(not locked_by_recording)
        for checkbox in self.save_channel_checkboxes.values():
            checkbox.setEnabled((not follow_display) and (not locked_by_recording))

    def _update_y_range_controls(self) -> None:
        enabled = not self.auto_range_checkbox.isChecked()
        self.y_min_spin.setEnabled(enabled)
        self.y_max_spin.setEnabled(enabled)
        self.restore_y_range_button.setEnabled(enabled)

    def _toggle_realtime_save(self) -> None:
        if self.logger.is_realtime_save_active:
            self.realtime_save_timer.stop()
            saved_path = self.logger.stop_realtime_save()
            self._update_record_ui()
            self._append_log(f"实时保存已停止：{saved_path}" if saved_path else "实时保存已停止。")
            return

        self._realtime_save_channels = self._selected_save_channels()
        self._realtime_save_segment_index = 1
        saved_path = self._start_realtime_save_segment()
        self.realtime_save_timer.start(int(self.save_interval_spin.value() * 1000))
        self._update_record_ui()
        self._append_log(
            "实时保存已开始：{path}，保存通道：{channels}，分段间隔：{interval:.0f} s".format(
                path=saved_path,
                channels=self._describe_channels(self._realtime_save_channels),
                interval=self.save_interval_spin.value(),
            )
        )

    def _start_realtime_save_segment(self) -> Path:
        file_path = self._make_data_file_path("adc_capture", self._realtime_save_segment_index)
        return self.logger.start_realtime_save(file_path, self._realtime_save_channels)

    def _rotate_realtime_save_file(self) -> None:
        if not self.logger.is_realtime_save_active:
            self.realtime_save_timer.stop()
            return

        old_path = self.logger.stop_realtime_save()
        self._realtime_save_segment_index += 1
        new_path = self._start_realtime_save_segment()
        self._update_record_ui()
        self._append_log(f"定时保存分段：{old_path} -> {new_path}")

    def _update_record_ui(self) -> None:
        active = self.logger.is_realtime_save_active
        self.record_button.setText("停止记录" if active else "开始记录")
        if active and self.logger.realtime_save_path is not None:
            self.record_status_label.setText(
                f"记录状态：进行中 -> {self.logger.realtime_save_path}，间隔 {self.save_interval_spin.value():.0f} s"
            )
        else:
            self.record_status_label.setText("记录状态：未开启。")
        self.save_interval_spin.setEnabled(not active)
        self._refresh_save_channel_controls()

    def _clear_display(self) -> None:
        self.plot_widget.clear_data()
        self._append_log("已清空当前波形显示。")

    def _clear_unsaved_cache(self) -> None:
        self.logger.clear(reset_session_counter=False)
        if self.logger.is_realtime_save_active:
            self._append_log("已清空未导出缓存；实时保存仍在继续。")
        else:
            self._append_log("已清空未导出缓存。")

    def _show_all_data(self) -> None:
        self.plot_widget.show_all_data()
        self._append_log("已切换到显示当前缓存的全部时间范围。")

    def _make_data_file_path(self, prefix: str, segment_index: int | None = None) -> Path:
        now = datetime.now()
        data_dir = Path.cwd() / "data" / now.strftime("%Y-%m-%d")
        suffix = "" if segment_index is None else f"_part{segment_index:03d}"
        return data_dir / f"{prefix}_{now.strftime('%Y%m%d_%H%M%S')}{suffix}.csv"

    def _save_csv(self) -> None:
        if self.logger.frame_count == 0:
            QMessageBox.information(self, "暂无数据", "当前还没有接收到可保存的数据。")
            return

        default_path = self._make_data_file_path("adc_export")
        default_path.parent.mkdir(parents=True, exist_ok=True)
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出 CSV",
            str(default_path),
            "CSV Files (*.csv)",
        )
        if not file_path:
            return

        selected_channels = self._selected_save_channels()
        saved_path = self.logger.save_csv(file_path, selected_channels)
        self._append_log(
            "已导出 {frame_count} 帧、{session_count} 个会话的数据到 {path}，保存通道：{channels}".format(
                frame_count=self.logger.frame_count,
                session_count=self.logger.session_count,
                path=saved_path,
                channels=self._describe_channels(selected_channels),
            )
        )

    def _import_csv_files(self) -> None:
        if not self._can_import_offline_data():
            return

        default_dir = Path.cwd() / "data"
        if not default_dir.exists():
            default_dir = Path.cwd()
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "导入 CSV",
            str(default_dir),
            "CSV Files (*.csv)",
        )
        if not file_paths:
            return

        self._load_imported_csv_paths([Path(file_path) for file_path in file_paths])

    def _import_csv_folder(self) -> None:
        if not self._can_import_offline_data():
            return

        default_dir = Path.cwd() / "data"
        if not default_dir.exists():
            default_dir = Path.cwd()
        folder_path = QFileDialog.getExistingDirectory(self, "导入 CSV 文件夹", str(default_dir))
        if not folder_path:
            return

        csv_paths = sorted(Path(folder_path).rglob("*.csv"))
        if not csv_paths:
            QMessageBox.information(self, "没有 CSV", "所选文件夹中没有找到 CSV 文件。")
            return

        self._load_imported_csv_paths(csv_paths)

    def _can_import_offline_data(self) -> bool:
        if self._is_connected:
            QMessageBox.warning(self, "正在连接", "导入历史 CSV 前请先断开 BLE 连接。")
            return False
        if self.logger.is_realtime_save_active:
            QMessageBox.warning(self, "正在记录", "导入历史 CSV 前请先停止实时保存。")
            return False
        return True

    def _load_imported_csv_paths(self, csv_paths: list[Path]) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            series = load_imported_csv_series(
                csv_paths,
                prefer_filtered=self.import_prefer_filtered_checkbox.isChecked(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        self._apply_imported_series(series)

    def _apply_imported_series(self, series: ImportedCsvSeries) -> None:
        self.plot_widget.load_sample_series(
            series.x_values,
            series.raw_channels,
            series.plot_channels,
            series.frame_ids,
            series.timestamp_ms_values,
            series.pc_recv_time_texts,
        )

        available_channels = set(series.available_channels)
        for channel_key, checkbox in self.display_channel_checkboxes.items():
            previous = checkbox.blockSignals(True)
            checkbox.setChecked(channel_key in available_channels)
            checkbox.blockSignals(previous)
            self.plot_widget.set_channel_visibility(channel_key, channel_key in available_channels)

        self._sync_save_channels_with_display()
        self.plot_widget.show_all_data()
        self._set_display_enabled(True)
        self._append_log(
            "已导入 {files} 个 CSV，输入 {input_rows} 行，绘制 {rows} 行；"
            "跳过 {skipped} 行，重复 {duplicates} 行；横轴：{axis}，数值：{mode}，通道：{channels}。".format(
                files=series.file_count,
                input_rows=series.input_row_count,
                rows=series.imported_row_count,
                skipped=series.skipped_row_count,
                duplicates=series.duplicate_row_count,
                axis=series.time_axis_label,
                mode=series.value_mode_label,
                channels=self._describe_channels(series.available_channels),
            )
        )

    def _selected_save_channels(self) -> tuple[str, ...]:
        return tuple(
            channel_key
            for channel_key, checkbox in self.save_channel_checkboxes.items()
            if checkbox.isChecked()
        )

    def _describe_channels(self, channels: tuple[str, ...]) -> str:
        if not channels:
            return "仅基础字段（时间/帧号）"

        label_map = {key: label for key, label, _ in CHANNEL_SPECS}
        return ", ".join(label_map[channel_key] for channel_key in channels)

    def _set_spin_value(self, spin_box: QDoubleSpinBox, value: float) -> None:
        previous = spin_box.blockSignals(True)
        spin_box.setValue(value)
        spin_box.blockSignals(previous)

    def _request_safe_exit(self) -> None:
        if self._is_connected:
            message = "当前 BLE 设备仍处于连接状态，退出时会先断开连接并关闭后台线程。\n确定要退出吗？"
        else:
            message = "确定要安全退出上位机吗？"

        reply = QMessageBox.question(
            self,
            "安全退出",
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._append_log("正在安全退出程序。")
            self.close()

    def _handle_error(self, message: str) -> None:
        self.last_error_label.setText(message)
        self._append_log(f"错误: {message}")

    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.logger.is_realtime_save_active:
            self.realtime_save_timer.stop()
            saved_path = self.logger.stop_realtime_save()
            self._append_log(f"退出前已停止实时保存：{saved_path}")
        self.ble.shutdown()
        super().closeEvent(event)
