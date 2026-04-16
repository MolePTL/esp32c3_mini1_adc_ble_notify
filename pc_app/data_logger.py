"""CSV 数据记录模块。

这个模块负责两类事情：
1. 在内存中保存已经解析好的完整帧
2. 按用户当前选择，把需要的通道导出或实时写入 CSV

这里特意把“完整缓存”和“导出过滤”分开：
- 内部始终保留完整 4 路采样
- 显示哪些通道、保存哪些通道，由上层单独控制
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, TextIO

from pc_app.protocol import AdcFrame, CHANNEL_SPECS

BASE_CSV_COLUMNS = [
    "session_index",
    "pc_recv_time",
    "frame_id",
    "timestamp_ms",
]


class DataLogger:
    """简单的内存型数据记录器。"""

    def __init__(self) -> None:
        self._records: list[tuple[int, AdcFrame]] = []
        self._active_session_index: int | None = None
        self._next_session_index = 1

        self._realtime_save_path: Path | None = None
        self._realtime_save_file: TextIO | None = None
        self._realtime_save_writer: csv.DictWriter | None = None
        self._realtime_save_channels: tuple[str, ...] = ()

    @property
    def frame_count(self) -> int:
        return len(self._records)

    @property
    def session_count(self) -> int:
        return len({session_index for session_index, _ in self._records})

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

    def append(self, frame: AdcFrame) -> None:
        if self._active_session_index is None:
            self.start_new_session()

        session_index = self._active_session_index
        self._records.append((session_index, frame))

        if self.is_realtime_save_active:
            row = self._build_row(session_index, frame, self._realtime_save_channels)
            self._realtime_save_writer.writerow(row)
            self._realtime_save_file.flush()

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
        fieldnames = self._build_fieldnames(selected_channels)

        with path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()

            for session_index, frame in self._records:
                writer.writerow(self._build_row(session_index, frame, selected_channels))

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
        fieldnames = self._build_fieldnames(selected_channels)

        realtime_file = path.open("w", newline="", encoding="utf-8")
        writer = csv.DictWriter(realtime_file, fieldnames=fieldnames)
        writer.writeheader()
        realtime_file.flush()

        self._realtime_save_path = path
        self._realtime_save_file = realtime_file
        self._realtime_save_writer = writer
        self._realtime_save_channels = selected_channels
        return path

    def stop_realtime_save(self) -> Path | None:
        path = self._realtime_save_path

        if self._realtime_save_file is not None:
            self._realtime_save_file.close()

        self._realtime_save_path = None
        self._realtime_save_file = None
        self._realtime_save_writer = None
        self._realtime_save_channels = ()
        return path

    def _normalize_channels(self, channels_to_save: Iterable[str] | None) -> tuple[str, ...]:
        ordered_keys = [key for key, _, _ in CHANNEL_SPECS]
        if channels_to_save is None:
            return tuple(ordered_keys)

        selected_set = {channel for channel in channels_to_save if channel in ordered_keys}
        return tuple(key for key in ordered_keys if key in selected_set)

    def _build_fieldnames(self, selected_channels: tuple[str, ...]) -> list[str]:
        fieldnames = list(BASE_CSV_COLUMNS)
        for channel_key in selected_channels:
            if channel_key == "vtem":
                fieldnames.extend(
                    [
                        "vtem_voltage_v",
                        "vtem_resistance_ohm",
                        "vtem_temperature_c",
                    ]
                )
            elif channel_key == "vm":
                fieldnames.append("vm_voltage_v")
            elif channel_key == "va201":
                fieldnames.append("va201_voltage_v")
            elif channel_key == "vbat":
                fieldnames.append("vbat_voltage_v")
        return fieldnames

    def _build_row(
        self,
        session_index: int,
        frame: AdcFrame,
        selected_channels: tuple[str, ...],
    ) -> dict[str, object]:
        row: dict[str, object] = {
            "session_index": session_index,
            "pc_recv_time": frame.pc_recv_time.isoformat(timespec="milliseconds"),
            "frame_id": frame.frame_id,
            "timestamp_ms": frame.timestamp_ms,
        }

        if "vtem" in selected_channels:
            resistance_ohm, temperature_c = frame.try_vtem_pt1000_metrics()
            row["vtem_voltage_v"] = frame.vtem_mv / 1000.0
            row["vtem_resistance_ohm"] = "" if resistance_ohm is None else resistance_ohm
            row["vtem_temperature_c"] = "" if temperature_c is None else temperature_c

        if "vm" in selected_channels:
            row["vm_voltage_v"] = frame.vm_mv / 1000.0

        if "va201" in selected_channels:
            row["va201_voltage_v"] = frame.va201_mv / 1000.0

        if "vbat" in selected_channels:
            row["vbat_voltage_v"] = frame.vbat_mv / 1000.0

        return row
