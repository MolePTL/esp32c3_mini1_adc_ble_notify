"""CSV import helpers for offline waveform review."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from math import isfinite, nan
from pathlib import Path
from typing import Iterable

from pc_app.protocol import CHANNEL_SPECS


@dataclass(slots=True)
class ImportedCsvSeries:
    """Data loaded from one or more exported CSV files."""

    x_values: list[float]
    raw_channels: dict[str, list[float]]
    plot_channels: dict[str, list[float]]
    frame_ids: list[int]
    timestamp_ms_values: list[int]
    pc_recv_time_texts: list[str]
    available_channels: tuple[str, ...]
    file_count: int
    input_row_count: int
    imported_row_count: int
    skipped_row_count: int
    duplicate_row_count: int
    value_mode_label: str
    time_axis_label: str


@dataclass(slots=True)
class _CsvFileSummary:
    path: Path
    first_sort_value: int


def load_imported_csv_series(
    file_paths: Iterable[str | Path],
    prefer_filtered: bool = True,
) -> ImportedCsvSeries:
    """Load exported ADC CSV files into arrays suitable for offline plotting."""

    paths = _unique_existing_csv_paths(file_paths)
    if not paths:
        raise ValueError("没有找到可导入的 CSV 文件。")

    summaries = [_summarize_file(path) for path in paths]
    summaries = [summary for summary in summaries if summary is not None]
    if not summaries:
        raise ValueError("CSV 文件中没有找到可绘制的数据行。")

    raw_channels = {key: [] for key, _, _ in CHANNEL_SPECS}
    plot_channels = {key: [] for key, _, _ in CHANNEL_SPECS}
    frame_ids: list[int] = []
    timestamp_ms_values: list[int] = []
    pc_recv_time_texts: list[str] = []
    x_values: list[float] = []

    seen_keys: set[tuple[str, int, int]] = set()
    available_channels: set[str] = set()
    input_row_count = 0
    skipped_row_count = 0
    duplicate_row_count = 0
    base_sort_value: int | None = None
    last_sort_value: int | None = None
    used_filtered_column = False
    used_pc_time_axis = False

    for summary in sorted(summaries, key=lambda item: (item.first_sort_value, item.path.name)):
        with summary.path.open("r", newline="", encoding="utf-8-sig") as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                input_row_count += 1
                parsed = _parse_row(row, prefer_filtered)
                if parsed is None:
                    skipped_row_count += 1
                    continue

                (
                    sort_value,
                    sort_uses_pc_time,
                    pc_recv_time_text,
                    frame_id,
                    timestamp_ms,
                    raw_values,
                    plot_values,
                    row_used_filtered,
                ) = parsed

                dedupe_key = (pc_recv_time_text, timestamp_ms, frame_id)
                if dedupe_key in seen_keys:
                    duplicate_row_count += 1
                    continue

                if last_sort_value is not None and sort_value < last_sort_value:
                    skipped_row_count += 1
                    continue

                seen_keys.add(dedupe_key)
                if base_sort_value is None:
                    base_sort_value = sort_value

                x_values.append((sort_value - base_sort_value) / 1_000_000.0)
                frame_ids.append(frame_id)
                timestamp_ms_values.append(timestamp_ms)
                pc_recv_time_texts.append(pc_recv_time_text)

                for index, (channel_key, _, _) in enumerate(CHANNEL_SPECS):
                    raw_value = raw_values[index]
                    plot_value = plot_values[index]
                    raw_channels[channel_key].append(raw_value)
                    plot_channels[channel_key].append(plot_value)
                    if isfinite(plot_value):
                        available_channels.add(channel_key)

                used_filtered_column = used_filtered_column or row_used_filtered
                used_pc_time_axis = used_pc_time_axis or sort_uses_pc_time
                last_sort_value = sort_value if last_sort_value is None else max(last_sort_value, sort_value)

    if not x_values:
        raise ValueError("CSV 文件中没有可绘制的数据行。")

    ordered_available_channels = tuple(
        channel_key for channel_key, _, _ in CHANNEL_SPECS if channel_key in available_channels
    )
    return ImportedCsvSeries(
        x_values=x_values,
        raw_channels=raw_channels,
        plot_channels=plot_channels,
        frame_ids=frame_ids,
        timestamp_ms_values=timestamp_ms_values,
        pc_recv_time_texts=pc_recv_time_texts,
        available_channels=ordered_available_channels,
        file_count=len(summaries),
        input_row_count=input_row_count,
        imported_row_count=len(x_values),
        skipped_row_count=skipped_row_count,
        duplicate_row_count=duplicate_row_count,
        value_mode_label="滤波电压优先" if used_filtered_column else "原始电压",
        time_axis_label="PC 接收时间" if used_pc_time_axis else "设备 timestamp_ms",
    )


def _unique_existing_csv_paths(file_paths: Iterable[str | Path]) -> list[Path]:
    unique_paths: dict[Path, None] = {}
    for file_path in file_paths:
        path = Path(file_path)
        if path.is_file() and path.suffix.lower() == ".csv":
            unique_paths[path.resolve()] = None
    return list(unique_paths)


def _summarize_file(path: Path) -> _CsvFileSummary | None:
    with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            parsed_time = _parse_sort_value(row)
            if parsed_time is not None and _row_has_any_channel_value(row):
                return _CsvFileSummary(path=path, first_sort_value=parsed_time[0])
    return None


def _parse_row(
    row: dict[str, str],
    prefer_filtered: bool,
) -> tuple[int, bool, str, int, int, tuple[float, ...], tuple[float, ...], bool] | None:
    parsed_time = _parse_sort_value(row)
    if parsed_time is None:
        return None

    sort_value, sort_uses_pc_time = parsed_time
    timestamp_ms = _parse_int(row.get("timestamp_ms"), default=0)
    frame_id = _parse_int(row.get("frame_id"), default=0)
    pc_recv_time_text = (row.get("pc_recv_time") or "").strip()

    raw_values: list[float] = []
    plot_values: list[float] = []
    used_filtered = False

    for channel_key, _, _ in CHANNEL_SPECS:
        raw_value = _parse_float(row.get(f"{channel_key}_voltage_v"))
        filtered_value = _parse_float(row.get(f"{channel_key}_voltage_filtered_v"))

        if prefer_filtered and isfinite(filtered_value):
            plot_value = filtered_value
            used_filtered = True
        else:
            plot_value = raw_value

        raw_values.append(raw_value)
        plot_values.append(plot_value)

    if not any(isfinite(value) for value in plot_values):
        return None

    return (
        sort_value,
        sort_uses_pc_time,
        pc_recv_time_text,
        frame_id,
        timestamp_ms,
        tuple(raw_values),
        tuple(plot_values),
        used_filtered,
    )


def _parse_sort_value(row: dict[str, str]) -> tuple[int, bool] | None:
    pc_recv_time_text = (row.get("pc_recv_time") or "").strip()
    if pc_recv_time_text:
        try:
            pc_recv_time = datetime.fromisoformat(pc_recv_time_text)
        except ValueError:
            pass
        else:
            return int(pc_recv_time.timestamp() * 1_000_000), True

    timestamp_ms = _parse_int(row.get("timestamp_ms"), default=None)
    if timestamp_ms is None:
        return None
    return timestamp_ms * 1000, False


def _row_has_any_channel_value(row: dict[str, str]) -> bool:
    return any(
        isfinite(_parse_float(row.get(f"{channel_key}_voltage_v")))
        or isfinite(_parse_float(row.get(f"{channel_key}_voltage_filtered_v")))
        for channel_key, _, _ in CHANNEL_SPECS
    )


def _parse_float(value: str | None) -> float:
    if value is None:
        return nan
    text = str(value).strip()
    if not text:
        return nan
    try:
        return float(text)
    except ValueError:
        return nan


def _parse_int(value: str | None, default: int | None) -> int | None:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default
