"""ADC channel filtering and derived-value helpers."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import exp, pi

from pc_app.protocol import (
    AdcFrame,
    CHANNEL_SPECS,
    a201_resistance_from_output_voltage_v,
    pt1000_resistance_from_divider_voltage_v,
    pt1000_temperature_from_resistance_ohm,
    vbat_source_voltage_from_adc_voltage_v,
)

SPIKE_FILTER_NONE = "none"
SPIKE_FILTER_MEDIAN3 = "median3"
SPIKE_FILTER_MEDIAN5 = "median5"
SPIKE_FILTER_HOLD = "hold"

SPIKE_FILTER_LABELS = {
    SPIKE_FILTER_NONE: "无去尖峰",
    SPIKE_FILTER_MEDIAN3: "3点中值去尖峰",
    SPIKE_FILTER_MEDIAN5: "5点中值去尖峰",
    SPIKE_FILTER_HOLD: "毛刺判定保持",
}

GLITCH_HOLD_THRESHOLD_V = 0.003


@dataclass(slots=True)
class ChannelProcessingResult:
    """Raw, despiked, and fully filtered voltage for one ADC channel."""

    raw_voltage_v: float
    despiked_voltage_v: float
    filtered_voltage_v: float
    spike_filter_mode: str
    ema_cutoff_hz: float | None


@dataclass(slots=True)
class VtemProcessingResult:
    """Derived VTEM values for display, plotting, and CSV export."""

    raw_voltage_v: float
    filtered_voltage_v: float
    spike_filter_mode: str
    median_filter_enabled: bool
    ema_cutoff_hz: float | None
    wire_compensation_ohm: float
    raw_resistance_ohm: float | None
    filtered_resistance_ohm: float | None
    compensated_resistance_ohm: float | None
    raw_temperature_c: float | None
    compensated_temperature_c: float | None

    @property
    def filter_cutoff_hz(self) -> float | None:
        """Backward-compatible name for the VTEM EMA cutoff."""
        return self.ema_cutoff_hz


@dataclass(slots=True)
class AdcProcessingResult:
    """Processed values for one complete ADC frame."""

    channels: dict[str, ChannelProcessingResult]
    vtem: VtemProcessingResult

    def raw_voltage_v(self, channel_key: str) -> float:
        return self.channels[channel_key].raw_voltage_v

    def despiked_voltage_v(self, channel_key: str) -> float:
        return self.channels[channel_key].despiked_voltage_v

    def filtered_voltage_v(self, channel_key: str) -> float:
        return self.channels[channel_key].filtered_voltage_v

    def ema_cutoff_hz(self, channel_key: str) -> float | None:
        return self.channels[channel_key].ema_cutoff_hz

    def spike_filter_mode(self, channel_key: str) -> str:
        return self.channels[channel_key].spike_filter_mode

    def try_va201_resistance_ohm(self, use_filtered_voltage: bool = True) -> float | None:
        voltage_v = self.filtered_voltage_v("va201") if use_filtered_voltage else self.raw_voltage_v("va201")
        try:
            return a201_resistance_from_output_voltage_v(voltage_v)
        except ValueError:
            return None

    def vbat_source_voltage_v(self, use_filtered_voltage: bool = True) -> float:
        voltage_v = self.filtered_voltage_v("vbat") if use_filtered_voltage else self.raw_voltage_v("vbat")
        return vbat_source_voltage_from_adc_voltage_v(voltage_v)


class SpikeFilter:
    """Selectable spike filter shared by all channels."""

    def __init__(self, mode: str = SPIKE_FILTER_MEDIAN3) -> None:
        self._mode = mode
        self._median_window: deque[float] = deque(maxlen=self._median_window_size(mode))
        self._stable_value: float | None = None
        self._candidate_value: float | None = None
        self._candidate_count = 0

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._mode = self._normalize_mode(mode)
        self._median_window = deque(maxlen=self._median_window_size(self._mode))
        self.reset()

    def reset(self) -> None:
        self._median_window.clear()
        self._stable_value = None
        self._candidate_value = None
        self._candidate_count = 0

    def apply(self, value: float) -> float:
        if self._mode == SPIKE_FILTER_NONE:
            return value
        if self._mode in {SPIKE_FILTER_MEDIAN3, SPIKE_FILTER_MEDIAN5}:
            return self._apply_median(value)
        if self._mode == SPIKE_FILTER_HOLD:
            return self._apply_hold(value)
        return value

    def _apply_median(self, value: float) -> float:
        self._median_window.append(value)
        if len(self._median_window) < self._median_window.maxlen:
            return self._median_window[0]
        values = sorted(self._median_window)
        return values[len(values) // 2]

    def _apply_hold(self, value: float) -> float:
        if self._stable_value is None:
            self._stable_value = value
            return value

        if abs(value - self._stable_value) < GLITCH_HOLD_THRESHOLD_V:
            self._stable_value = value
            self._candidate_value = None
            self._candidate_count = 0
            return self._stable_value

        if self._candidate_value is None or abs(value - self._candidate_value) >= GLITCH_HOLD_THRESHOLD_V:
            self._candidate_value = value
            self._candidate_count = 1
            return self._stable_value

        self._candidate_count += 1
        if self._candidate_count >= 2:
            self._stable_value = value
            self._candidate_value = None
            self._candidate_count = 0

        return self._stable_value

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        return mode if mode in SPIKE_FILTER_LABELS else SPIKE_FILTER_MEDIAN3

    @staticmethod
    def _median_window_size(mode: str) -> int:
        if mode == SPIKE_FILTER_MEDIAN5:
            return 5
        return 3


class AdcProcessor:
    """Stateful ADC channel spike filters, EMA filters, and derived values."""

    def __init__(
        self,
        spike_filter_mode: str = SPIKE_FILTER_MEDIAN3,
        default_ema_cutoff_hz: float | None = 5.0,
        wire_compensation_ohm: float = 0.0,
    ) -> None:
        self._spike_filter_mode = spike_filter_mode
        self._wire_compensation_ohm = max(0.0, wire_compensation_ohm)
        self._spike_filters = {
            key: SpikeFilter(spike_filter_mode) for key, _, _ in CHANNEL_SPECS
        }
        self._ema_cutoffs = {
            key: default_ema_cutoff_hz for key, _, _ in CHANNEL_SPECS
        }
        self._ema_values: dict[str, float | None] = {key: None for key, _, _ in CHANNEL_SPECS}
        self._last_timestamp_ms: int | None = None

    @property
    def spike_filter_mode(self) -> str:
        return self._spike_filter_mode

    @property
    def cutoff_hz(self) -> float | None:
        return self.ema_cutoff_hz("vtem")

    @property
    def wire_compensation_ohm(self) -> float:
        return self._wire_compensation_ohm

    def ema_cutoff_hz(self, channel_key: str) -> float | None:
        return self._ema_cutoffs[channel_key]

    def set_spike_filter_mode(self, mode: str) -> None:
        mode = SpikeFilter._normalize_mode(mode)
        self._spike_filter_mode = mode
        for spike_filter in self._spike_filters.values():
            spike_filter.set_mode(mode)
        self._reset_ema()

    def set_cutoff_hz(self, cutoff_hz: float | None) -> None:
        self.set_channel_ema_cutoff_hz("vtem", cutoff_hz)

    def set_channel_ema_cutoff_hz(self, channel_key: str, cutoff_hz: float | None) -> None:
        self._ema_cutoffs[channel_key] = None if cutoff_hz is None else max(0.001, float(cutoff_hz))
        self._ema_values[channel_key] = None

    def set_wire_compensation_ohm(self, resistance_ohm: float) -> None:
        self._wire_compensation_ohm = max(0.0, float(resistance_ohm))

    def reset(self) -> None:
        for spike_filter in self._spike_filters.values():
            spike_filter.reset()
        self._reset_ema()
        self._last_timestamp_ms = None

    def process(self, frame: AdcFrame) -> AdcProcessingResult:
        if self._last_timestamp_ms is not None and frame.timestamp_ms < self._last_timestamp_ms:
            self.reset()

        dt_s = None if self._last_timestamp_ms is None else max(0.0, (frame.timestamp_ms - self._last_timestamp_ms) / 1000.0)
        channels: dict[str, ChannelProcessingResult] = {}

        for channel_key, _, attr_name in CHANNEL_SPECS:
            raw_voltage_v = getattr(frame, attr_name) / 1000.0
            despiked_voltage_v = self._spike_filters[channel_key].apply(raw_voltage_v)
            filtered_voltage_v = self._apply_ema(channel_key, despiked_voltage_v, dt_s)
            channels[channel_key] = ChannelProcessingResult(
                raw_voltage_v=raw_voltage_v,
                despiked_voltage_v=despiked_voltage_v,
                filtered_voltage_v=filtered_voltage_v,
                spike_filter_mode=self._spike_filter_mode,
                ema_cutoff_hz=self._ema_cutoffs[channel_key],
            )

        vtem_voltage_v = channels["vtem"].filtered_voltage_v
        raw_resistance_ohm = self._resistance_from_voltage(channels["vtem"].raw_voltage_v)
        filtered_resistance_ohm = self._resistance_from_voltage(vtem_voltage_v)
        compensated_resistance_ohm = self._compensate_resistance(filtered_resistance_ohm)

        raw_temperature_c = self._temperature_from_resistance(raw_resistance_ohm)
        compensated_temperature_c = self._temperature_from_resistance(compensated_resistance_ohm)

        vtem_result = VtemProcessingResult(
            raw_voltage_v=channels["vtem"].raw_voltage_v,
            filtered_voltage_v=vtem_voltage_v,
            spike_filter_mode=self._spike_filter_mode,
            median_filter_enabled=self._spike_filter_mode in {SPIKE_FILTER_MEDIAN3, SPIKE_FILTER_MEDIAN5},
            ema_cutoff_hz=self._ema_cutoffs["vtem"],
            wire_compensation_ohm=self._wire_compensation_ohm,
            raw_resistance_ohm=raw_resistance_ohm,
            filtered_resistance_ohm=filtered_resistance_ohm,
            compensated_resistance_ohm=compensated_resistance_ohm,
            raw_temperature_c=raw_temperature_c,
            compensated_temperature_c=compensated_temperature_c,
        )
        self._last_timestamp_ms = frame.timestamp_ms
        return AdcProcessingResult(channels=channels, vtem=vtem_result)

    def _apply_ema(self, channel_key: str, value: float, dt_s: float | None) -> float:
        cutoff_hz = self._ema_cutoffs[channel_key]
        if cutoff_hz is None:
            self._ema_values[channel_key] = value
            return value

        previous = self._ema_values[channel_key]
        if previous is None or dt_s is None:
            self._ema_values[channel_key] = value
            return value

        if dt_s <= 0.0:
            return previous

        alpha = 1.0 - exp(-2.0 * pi * cutoff_hz * dt_s)
        filtered = previous + alpha * (value - previous)
        self._ema_values[channel_key] = filtered
        return filtered

    def _reset_ema(self) -> None:
        for key in self._ema_values:
            self._ema_values[key] = None

    def _compensate_resistance(self, resistance_ohm: float | None) -> float | None:
        if resistance_ohm is None:
            return None

        compensated = resistance_ohm - self._wire_compensation_ohm
        return compensated if compensated > 0.0 else None

    @staticmethod
    def _resistance_from_voltage(voltage_v: float) -> float | None:
        try:
            return pt1000_resistance_from_divider_voltage_v(voltage_v)
        except ValueError:
            return None

    @staticmethod
    def _temperature_from_resistance(resistance_ohm: float | None) -> float | None:
        if resistance_ohm is None:
            return None

        try:
            return pt1000_temperature_from_resistance_ohm(resistance_ohm)
        except ValueError:
            return None


VtemProcessor = AdcProcessor
