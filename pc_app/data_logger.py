"""CSV 数据记录模块。

这个模块负责两类事情：
1. 在内存中保存已经解析好的完整帧
2. 按用户当前选择，把需要的通道导出或实时写入 CSV

这里特意把“完整缓存”和“导出过滤”分开：
- 内部始终保留完整 3 路采样
- 显示哪些通道、保存哪些通道，由上层单独控制
"""

from __future__ import annotations

import csv
from math import isfinite
from pathlib import Path
from typing import Iterable, TextIO

from pc_app.protocol import AdcFrame, CHANNEL_SPECS, vbat_source_voltage_from_adc_voltage_v
from pc_app.vtem_processor import AdcProcessingResult

BASE_CSV_COLUMNS = [
    "session_index",
    "pc_recv_time",
    "frame_id",
    "timestamp_ms",
]

AGGREGATION_CSV_COLUMNS = [
    "aggregation_method",
    "aggregation_sample_count",
    "frame_id_start",
    "frame_id_end",
    "timestamp_start_ms",
    "timestamp_end_ms",
]

REALTIME_AGGREGATION_SAMPLE_COUNT = 10
REALTIME_AGGREGATION_METHOD = "trimmed_mean_drop_min_max_10"
CONFIG_VALUE_SUFFIXES = (
    "_spike_filter_mode",
    "_median_filter_enabled",
    "_filter_cutoff_hz",
    "_wire_compensation_ohm",
)


class DataLogger:
    """简单的内存型数据记录器。"""

    def __init__(self) -> None:
        self._records: list[tuple[int, AdcFrame, AdcProcessingResult | None]] = []
        self._active_session_index: int | None = None
        self._next_session_index = 1

        self._realtime_save_path: Path | None = None
        self._realtime_save_file: TextIO | None = None
        self._realtime_save_writer: csv.DictWriter | None = None
        self._realtime_save_channels: tuple[str, ...] = ()
        self._realtime_save_buffer: list[dict[str, object]] = []

    @property
    def frame_count(self) -> int:
        return len(self._records)

    @property
    def session_count(self) -> int:
        return len({session_index for session_index, _, _ in self._records})

    @property
    def is_realtime_save_active(self) -> bool:
        return self._realtime_save_writer is not None and self._realtime_save_file is not None

    @property
    def realtime_save_path(self) -> Path | None:
        return self._realtime_save_path

    def start_new_session(self) -> int:
        self._active_session_index = self._next_session_index
        self._next_session_index += 1
        return self._active_session_index

    def end_session(self) -> None:
        self._active_session_index = None

    def append(self, frame: AdcFrame, processing_result: AdcProcessingResult | None = None) -> None:
        if self._active_session_index is None:
            self.start_new_session()

        session_index = self._active_session_index
        self._records.append((session_index, frame, processing_result))

        if self.is_realtime_save_active:
            row = self._build_row(session_index, frame, self._realtime_save_channels, processing_result)
            self._append_realtime_save_row(row)

    def extend(self, frames: Iterable[AdcFrame]) -> None:
        for frame in frames:
            self.append(frame)

    def clear(self, reset_session_counter: bool = False) -> None:
        """清空内存缓存。

        默认不重置当前会话计数，避免在正在采集或实时保存时把会话号打乱。
        """
        self._records.clear()
        if reset_session_counter:
            self._active_session_index = None
            self._next_session_index = 1

    def save_csv(self, file_path: str | Path, channels_to_save: Iterable[str] | None = None) -> Path:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        selected_channels = self._normalize_channels(channels_to_save)
        fieldnames = self._build_fieldnames(selected_channels, include_aggregation=False)

        with path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()

            for session_index, frame, processing_result in self._records:
                writer.writerow(self._build_row(session_index, frame, selected_channels, processing_result))

        return path

    def start_realtime_save(
        self,
        file_path: str | Path,
        channels_to_save: Iterable[str] | None = None,
    ) -> Path:
        if self.is_realtime_save_active:
            self.stop_realtime_save()

        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        selected_channels = self._normalize_channels(channels_to_save)
        fieldnames = self._build_fieldnames(selected_channels, include_aggregation=True)

        realtime_file = path.open("w", newline="", encoding="utf-8")
        writer = csv.DictWriter(realtime_file, fieldnames=fieldnames)
        writer.writeheader()
        realtime_file.flush()

        self._realtime_save_path = path
        self._realtime_save_file = realtime_file
        self._realtime_save_writer = writer
        self._realtime_save_channels = selected_channels
        self._realtime_save_buffer.clear()
        return path

    def stop_realtime_save(self) -> Path | None:
        path = self._realtime_save_path

        if self._realtime_save_file is not None:
            self._realtime_save_file.close()

        self._realtime_save_path = None
        self._realtime_save_file = None
        self._realtime_save_writer = None
        self._realtime_save_channels = ()
        self._realtime_save_buffer.clear()
        return path

    def _normalize_channels(self, channels_to_save: Iterable[str] | None) -> tuple[str, ...]:
        ordered_keys = [key for key, _, _ in CHANNEL_SPECS]
        if channels_to_save is None:
            return tuple(ordered_keys)

        selected_set = {channel for channel in channels_to_save if channel in ordered_keys}
        return tuple(key for key in ordered_keys if key in selected_set)

    def _build_fieldnames(self, selected_channels: tuple[str, ...], include_aggregation: bool = False) -> list[str]:
        fieldnames = list(BASE_CSV_COLUMNS)
        if include_aggregation:
            fieldnames.extend(AGGREGATION_CSV_COLUMNS)
        for channel_key in selected_channels:
            if channel_key == "vtem":
                fieldnames.extend(
                    [
                        "vtem_voltage_v",
                        "vtem_resistance_ohm",
                        "vtem_temperature_c",
                        "vtem_spike_filter_mode",
                        "vtem_median_filter_enabled",
                        "vtem_filter_cutoff_hz",
                        "vtem_wire_compensation_ohm",
                        "vtem_voltage_filtered_v",
                        "vtem_resistance_filtered_ohm",
                        "vtem_resistance_compensated_ohm",
                        "vtem_temperature_compensated_c",
                    ]
                )
            elif channel_key == "va201":
                fieldnames.extend(
                    [
                        "va201_voltage_v",
                        "va201_spike_filter_mode",
                        "va201_filter_cutoff_hz",
                        "va201_voltage_filtered_v",
                        "va201_resistance_ohm",
                        "va201_resistance_filtered_ohm",
                    ]
                )
            elif channel_key == "vbat":
                fieldnames.extend(
                    [
                        "vbat_voltage_v",
                        "vbat_source_voltage_v",
                        "vbat_spike_filter_mode",
                        "vbat_filter_cutoff_hz",
                        "vbat_voltage_filtered_v",
                        "vbat_source_voltage_filtered_v",
                    ]
                )
        return fieldnames

    def _append_realtime_save_row(self, row: dict[str, object]) -> None:
        self._realtime_save_buffer.append(row)
        if len(self._realtime_save_buffer) < REALTIME_AGGREGATION_SAMPLE_COUNT:
            return

        aggregated_row = self._build_realtime_aggregation_row(self._realtime_save_buffer)
        if self._realtime_save_writer is not None and self._realtime_save_file is not None:
            self._realtime_save_writer.writerow(aggregated_row)
            self._realtime_save_file.flush()
        self._realtime_save_buffer.clear()

    def _build_realtime_aggregation_row(self, rows: list[dict[str, object]]) -> dict[str, object]:
        first_row = rows[0]
        last_row = rows[-1]
        aggregated_row: dict[str, object] = {}

        for column_name, last_value in last_row.items():
            if column_name in BASE_CSV_COLUMNS or self._is_config_value_column(column_name):
                aggregated_row[column_name] = last_value
                continue

            numeric_values = [self._coerce_finite_float(row.get(column_name)) for row in rows]
            if all(value is not None for value in numeric_values):
                aggregated_row[column_name] = self._trimmed_mean([value for value in numeric_values if value is not None])
            else:
                aggregated_row[column_name] = last_value

        aggregated_row.update(
            {
                "aggregation_method": REALTIME_AGGREGATION_METHOD,
                "aggregation_sample_count": len(rows),
                "frame_id_start": first_row.get("frame_id", ""),
                "frame_id_end": last_row.get("frame_id", ""),
                "timestamp_start_ms": first_row.get("timestamp_ms", ""),
                "timestamp_end_ms": last_row.get("timestamp_ms", ""),
            }
        )
        return aggregated_row

    def _build_row(
        self,
        session_index: int,
        frame: AdcFrame,
        selected_channels: tuple[str, ...],
        processing_result: AdcProcessingResult | None,
    ) -> dict[str, object]:
        row: dict[str, object] = {
            "session_index": session_index,
            "pc_recv_time": frame.pc_recv_time.isoformat(timespec="milliseconds"),
            "frame_id": frame.frame_id,
            "timestamp_ms": frame.timestamp_ms,
        }

        if "vtem" in selected_channels:
            vtem_result = None if processing_result is None else processing_result.vtem
            resistance_ohm, temperature_c = frame.try_vtem_pt1000_metrics()
            row["vtem_voltage_v"] = frame.vtem_mv / 1000.0
            row["vtem_resistance_ohm"] = "" if resistance_ohm is None else resistance_ohm
            row["vtem_temperature_c"] = "" if temperature_c is None else temperature_c
            row["vtem_spike_filter_mode"] = "" if vtem_result is None else vtem_result.spike_filter_mode
            row["vtem_median_filter_enabled"] = "" if vtem_result is None else vtem_result.median_filter_enabled
            row["vtem_filter_cutoff_hz"] = "" if vtem_result is None else vtem_result.filter_cutoff_hz or ""
            row["vtem_wire_compensation_ohm"] = (
                "" if vtem_result is None else vtem_result.wire_compensation_ohm
            )
            row["vtem_voltage_filtered_v"] = "" if vtem_result is None else vtem_result.filtered_voltage_v
            row["vtem_resistance_filtered_ohm"] = self._blank_if_none(
                None if vtem_result is None else vtem_result.filtered_resistance_ohm
            )
            row["vtem_resistance_compensated_ohm"] = self._blank_if_none(
                None if vtem_result is None else vtem_result.compensated_resistance_ohm
            )
            row["vtem_temperature_compensated_c"] = self._blank_if_none(
                None if vtem_result is None else vtem_result.compensated_temperature_c
            )

        if "va201" in selected_channels:
            resistance_ohm = frame.try_va201_resistance_ohm()
            filtered_resistance_ohm = (
                None if processing_result is None else processing_result.try_va201_resistance_ohm()
            )
            row["va201_voltage_v"] = frame.va201_mv / 1000.0
            row["va201_spike_filter_mode"] = self._spike_mode_or_blank(processing_result, "va201")
            row["va201_filter_cutoff_hz"] = self._cutoff_or_blank(processing_result, "va201")
            row["va201_voltage_filtered_v"] = self._filtered_voltage_or_blank(processing_result, "va201")
            row["va201_resistance_ohm"] = self._blank_if_none(resistance_ohm)
            row["va201_resistance_filtered_ohm"] = self._blank_if_none(filtered_resistance_ohm)

        if "vbat" in selected_channels:
            filtered_vbat_adc_v = None if processing_result is None else processing_result.filtered_voltage_v("vbat")
            row["vbat_voltage_v"] = frame.vbat_mv / 1000.0
            row["vbat_source_voltage_v"] = vbat_source_voltage_from_adc_voltage_v(frame.vbat_mv / 1000.0)
            row["vbat_spike_filter_mode"] = self._spike_mode_or_blank(processing_result, "vbat")
            row["vbat_filter_cutoff_hz"] = self._cutoff_or_blank(processing_result, "vbat")
            row["vbat_voltage_filtered_v"] = self._filtered_voltage_or_blank(processing_result, "vbat")
            row["vbat_source_voltage_filtered_v"] = (
                "" if filtered_vbat_adc_v is None else vbat_source_voltage_from_adc_voltage_v(filtered_vbat_adc_v)
            )

        return row

    @staticmethod
    def _filtered_voltage_or_blank(processing_result: AdcProcessingResult | None, channel_key: str) -> object:
        return "" if processing_result is None else processing_result.filtered_voltage_v(channel_key)

    @staticmethod
    def _spike_mode_or_blank(processing_result: AdcProcessingResult | None, channel_key: str) -> object:
        return "" if processing_result is None else processing_result.spike_filter_mode(channel_key)

    @staticmethod
    def _cutoff_or_blank(processing_result: AdcProcessingResult | None, channel_key: str) -> object:
        cutoff_hz = None if processing_result is None else processing_result.ema_cutoff_hz(channel_key)
        return "" if cutoff_hz is None else cutoff_hz

    @staticmethod
    def _blank_if_none(value: float | None) -> object:
        return "" if value is None else value

    @staticmethod
    def _is_config_value_column(column_name: str) -> bool:
        return column_name.endswith(CONFIG_VALUE_SUFFIXES)

    @staticmethod
    def _coerce_finite_float(value: object) -> float | None:
        if isinstance(value, bool) or value is None:
            return None

        if isinstance(value, (int, float)):
            numeric_value = float(value)
        else:
            text = str(value).strip()
            if not text:
                return None
            try:
                numeric_value = float(text)
            except ValueError:
                return None

        return numeric_value if isfinite(numeric_value) else None

    @staticmethod
    def _trimmed_mean(values: list[float]) -> float:
        if len(values) <= 2:
            return sum(values) / len(values)

        trimmed_values = sorted(values)[1:-1]
        return sum(trimmed_values) / len(trimmed_values)
