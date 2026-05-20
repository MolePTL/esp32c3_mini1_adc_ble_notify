"""Publication-style image export for imported CSV waveform data."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

from pc_app.csv_importer import ImportedCsvSeries, ImportedCsvValueColumn


@dataclass(frozen=True, slots=True)
class TimeUnitOption:
    key: str
    label: str
    axis_label: str
    seconds_per_unit: float


@dataclass(frozen=True, slots=True)
class ImageExportOptions:
    value_column_key: str
    time_unit_key: str
    width_cm: float
    height_cm: float
    dpi: int


TIME_UNIT_OPTIONS: tuple[TimeUnitOption, ...] = (
    TimeUnitOption("s", "秒 (s)", "时间 / s", 1.0),
    TimeUnitOption("min", "分钟 (min)", "时间 / min", 60.0),
    TimeUnitOption("h", "小时 (h)", "时间 / h", 3600.0),
)

DEFAULT_IMAGE_WIDTH_CM = 16.0
DEFAULT_IMAGE_HEIGHT_CM = 6.0
DEFAULT_IMAGE_DPI = 300


def export_imported_csv_series_plot(
    series: ImportedCsvSeries,
    output_path: str | Path,
    options: ImageExportOptions,
    visible_x_range_s: tuple[float, float],
) -> Path:
    """Export one selected CSV value column as a high-DPI PNG figure."""

    column = _find_export_column(series.export_columns, options.value_column_key)
    time_unit = _find_time_unit(options.time_unit_key)
    x_min_s, x_max_s = _normalize_x_range(visible_x_range_s, series.x_values)

    x_values: list[float] = []
    y_values: list[float] = []
    for x_value_s, y_value in zip(series.x_values, column.values):
        if x_value_s < x_min_s or x_value_s > x_max_s:
            continue
        if not isfinite(y_value):
            continue
        x_values.append(x_value_s / time_unit.seconds_per_unit)
        y_values.append(y_value)

    if not x_values:
        raise ValueError("当前视图范围内没有可导出的有效数据。")

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    plt = _load_pyplot()
    _configure_matplotlib_fonts(plt)

    width_inch = max(1.0, options.width_cm / 2.54)
    height_inch = max(1.0, options.height_cm / 2.54)
    dpi = max(72, int(options.dpi))

    fig, ax = plt.subplots(figsize=(width_inch, height_inch), dpi=dpi)
    try:
        ax.plot(x_values, y_values, color="#1f77b4", linewidth=1.8)
        ax.set_xlabel(time_unit.axis_label, fontsize=11)
        ax.set_ylabel(column.y_axis_label, fontsize=11)
        ax.grid(True, color="#b0b0b0", linewidth=0.8, alpha=0.55)
        ax.set_axisbelow(True)

        for spine in ax.spines.values():
            spine.set_color("#000000")
            spine.set_linewidth(1.0)

        ax.tick_params(axis="both", which="major", labelsize=10, width=1.0, colors="#000000")
        ax.set_xlim(x_min_s / time_unit.seconds_per_unit, x_max_s / time_unit.seconds_per_unit)
        _apply_y_range(ax, y_values)

        fig.tight_layout(pad=0.7)
        fig.savefig(path, dpi=dpi, facecolor="white")
    finally:
        plt.close(fig)

    return path


def _find_export_column(
    columns: tuple[ImportedCsvValueColumn, ...],
    column_key: str,
) -> ImportedCsvValueColumn:
    for column in columns:
        if column.key == column_key:
            return column
    raise ValueError("没有找到所选纵轴数据列。")


def _find_time_unit(unit_key: str) -> TimeUnitOption:
    for unit in TIME_UNIT_OPTIONS:
        if unit.key == unit_key:
            return unit
    raise ValueError("不支持的横轴单位。")


def _normalize_x_range(
    visible_x_range_s: tuple[float, float],
    x_values: list[float],
) -> tuple[float, float]:
    if not x_values:
        raise ValueError("当前没有可导出的时间数据。")

    data_min = min(x_values)
    data_max = max(x_values)
    visible_min, visible_max = sorted((float(visible_x_range_s[0]), float(visible_x_range_s[1])))
    x_min = max(data_min, visible_min)
    x_max = min(data_max, visible_max)

    if x_min < x_max:
        return x_min, x_max

    if data_min < data_max:
        return data_min, data_max

    return data_min, data_min + 0.001


def _apply_y_range(ax: object, y_values: list[float]) -> None:
    y_min = min(y_values)
    y_max = max(y_values)
    span = y_max - y_min
    if span <= 0.0:
        padding = max(abs(y_min) * 0.02, 0.1)
    else:
        padding = max(span * 0.05, 0.02)

    ax.set_ylim(y_min - padding, y_max + padding)


def _load_pyplot() -> object:
    try:
        import matplotlib

        if "matplotlib.pyplot" not in sys.modules:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise ImportError("导出图片需要安装 matplotlib。请先安装 requirements.txt 中的依赖。") from exc

    return plt


def _configure_matplotlib_fonts(plt: object) -> None:
    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False
