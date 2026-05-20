"""Dialog for configuring imported CSV waveform image export."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pc_app.csv_importer import ImportedCsvValueColumn
from pc_app.plot_image_exporter import (
    DEFAULT_IMAGE_DPI,
    DEFAULT_IMAGE_HEIGHT_CM,
    DEFAULT_IMAGE_WIDTH_CM,
    TIME_UNIT_OPTIONS,
    ImageExportOptions,
)


class ImageExportDialog(QDialog):
    """Collect publication image export options from the user."""

    _SIZE_PRESETS: tuple[tuple[str, float, float], ...] = (
        ("Word 通栏 16.0 x 6.0 cm", 16.0, 6.0),
        ("Word 半栏 8.0 x 5.0 cm", 8.0, 5.0),
        ("A4 页面宽 18.0 x 7.0 cm", 18.0, 7.0),
        ("自定义", DEFAULT_IMAGE_WIDTH_CM, DEFAULT_IMAGE_HEIGHT_CM),
    )

    def __init__(
        self,
        export_columns: tuple[ImportedCsvValueColumn, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self.setWindowTitle("导出图片设置")
        self.setModal(True)
        self.resize(420, 220)

        self.value_column_combo = QComboBox()
        for column in export_columns:
            self.value_column_combo.addItem(column.label, column.key)
        self._select_preferred_value_column()

        self.time_unit_combo = QComboBox()
        for unit in TIME_UNIT_OPTIONS:
            self.time_unit_combo.addItem(unit.label, unit.key)
        self._set_combo_by_data(self.time_unit_combo, "min")

        self.size_preset_combo = QComboBox()
        for label, width_cm, height_cm in self._SIZE_PRESETS:
            self.size_preset_combo.addItem(label, (width_cm, height_cm))

        self.width_spin = QDoubleSpinBox()
        self.width_spin.setRange(2.0, 60.0)
        self.width_spin.setDecimals(1)
        self.width_spin.setSingleStep(0.5)
        self.width_spin.setSuffix(" cm")
        self.width_spin.setValue(DEFAULT_IMAGE_WIDTH_CM)

        self.height_spin = QDoubleSpinBox()
        self.height_spin.setRange(2.0, 40.0)
        self.height_spin.setDecimals(1)
        self.height_spin.setSingleStep(0.5)
        self.height_spin.setSuffix(" cm")
        self.height_spin.setValue(DEFAULT_IMAGE_HEIGHT_CM)

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(72, 1200)
        self.dpi_spin.setSingleStep(50)
        self.dpi_spin.setValue(DEFAULT_IMAGE_DPI)

        size_row = QHBoxLayout()
        size_row.addWidget(self.width_spin)
        size_row.addWidget(QLabel("x"))
        size_row.addWidget(self.height_spin)

        form = QFormLayout()
        form.addRow("纵轴数据", self.value_column_combo)
        form.addRow("横轴单位", self.time_unit_combo)
        form.addRow("尺寸预设", self.size_preset_combo)
        form.addRow("图片尺寸", size_row)
        form.addRow("DPI", self.dpi_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.size_preset_combo.currentIndexChanged.connect(self._apply_size_preset)
        self._apply_size_preset()

    def selected_options(self) -> ImageExportOptions:
        return ImageExportOptions(
            value_column_key=str(self.value_column_combo.currentData()),
            time_unit_key=str(self.time_unit_combo.currentData()),
            width_cm=float(self.width_spin.value()),
            height_cm=float(self.height_spin.value()),
            dpi=int(self.dpi_spin.value()),
        )

    def selected_value_label(self) -> str:
        return self.value_column_combo.currentText()

    def _apply_size_preset(self, _index: int | None = None) -> None:
        width_cm, height_cm = self.size_preset_combo.currentData()
        self.width_spin.setValue(float(width_cm))
        self.height_spin.setValue(float(height_cm))

    def _select_preferred_value_column(self) -> None:
        for preferred_key in (
            "vtem_temperature_compensated_c",
            "vtem_temperature_c",
            "vtem_voltage_filtered_v",
        ):
            if self._set_combo_by_data(self.value_column_combo, preferred_key):
                return

    @staticmethod
    def _set_combo_by_data(combo_box: QComboBox, target_data: str) -> bool:
        for index in range(combo_box.count()):
            if combo_box.itemData(index) == target_data:
                combo_box.setCurrentIndex(index)
                return True
        return False
