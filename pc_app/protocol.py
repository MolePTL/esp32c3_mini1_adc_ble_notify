"""BLE 二进制协议定义与解析模块。

这个模块在整个上位机工程中的定位，和固件里的协议打包逻辑是配对的：
固件负责“按固定格式打包”，这里负责“按同一格式解包”。

教学上，这个模块非常重要，因为它把“数据通信”和“界面显示”分开了：
- BLE 模块只负责把一串 `bytes` 收上来
- 协议模块负责判断这串 `bytes` 是不是合法数据帧
- GUI / 绘图模块只关心解析后的结构化数据对象

这种分层方式有几个明显好处：
1. 协议变更时，优先修改这里，而不是满工程到处改
2. 可以单独测试解析函数，不必真的连 BLE 设备
3. 上层代码不需要记住字节偏移，直接操作具名字段即可
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from uuid import UUID

# ==================== 协议固定常量 ====================
# 这一组常量描述“这份 BLE 数据帧应该长什么样”。
# 它们相当于协议的最外层边界条件。

# 固定帧头，用来快速识别“这是不是我们自己的协议帧”。
# 上位机收到一帧原始 bytes 后，通常先看前两个字节是否为 0xAA 0x55。
FRAME_HEADER = b"\xAA\x55"

# 当前协议固定长度为 18 字节。
# 这和固件端的 APP_FRAME_LEN_BYTES 必须保持一致。
FRAME_LENGTH = 18

# 当前协议固定 3 路通道。
# 如果以后固件扩展成更多通道，这个常量和解析逻辑都需要同步更新。
EXPECTED_CHANNEL_COUNT = 3

# 协议版本号。
# 当前版本 0x03 表示三路通道载荷语义为“毫伏值 mV”。
# 之所以保留版本字段，是为了防止上下位机版本不一致时误解析。
EXPECTED_PROTOCOL_VERSION = 0x03

# 协议中的 3 路通道固定顺序。
# 这个常量用于把“协议字段名”“界面显示名”“AdcFrame 属性名”统一起来。
CHANNEL_SPECS = (
    ("vtem", "VTEM", "vtem_mv"),
    ("va201", "VA201", "va201_mv"),
    ("vbat", "VBAT", "vbat_mv"),
)

# 默认目标设备名。
# 它主要用于扫描结果排序和人眼识别，不是唯一可靠身份标识。
DEFAULT_DEVICE_NAME = "C3-ADC-BLE"

# ==================== UUID 原始字节定义 ====================
# 这里保留了与固件端一致的 128-bit UUID 字节数组。
# 这样上位机可以直接从“固件语义”推导出标准 UUID 字符串。

SERVICE_UUID_BYTES = bytes(
    [
        0x10,
        0x32,
        0x54,
        0x76,
        0x98,
        0xBA,
        0xDC,
        0xFE,
        0x12,
        0x34,
        0x56,
        0x78,
        0x9A,
        0xBC,
        0xDE,
        0xF0,
    ]
)

CHARACTERISTIC_UUID_BYTES = bytes(
    [
        0x21,
        0x43,
        0x65,
        0x87,
        0xA9,
        0xCB,
        0xED,
        0x0F,
        0x13,
        0x57,
        0x9B,
        0xDF,
        0x24,
        0x68,
        0xAC,
        0xE0,
    ]
)


def _uuid_from_esp_bytes(raw: bytes, reverse: bool) -> str:
    """把固件里的 16 字节 UUID 数组转成标准字符串形式。

    函数作用：
        把 ESP-IDF / BLE 常见的“16 字节 UUID 数组表示法”转换成
        Python / bleak 更常见的标准 UUID 字符串形式。

    调用时机：
        在模块导入阶段，用于生成默认 Service UUID 和 Characteristic UUID。

    参数含义：
        raw：16 字节原始 UUID 数据。
        reverse：是否先反转字节序再生成 UUID。

    返回值含义：
        返回标准格式、统一转成小写的 UUID 字符串。

    是否会修改全局状态：
        不会。

    与其他模块/任务/回调的关系：
        生成出的字符串会被 BLE 扫描/连接模块用于服务和特征匹配。

    设计说明：
        BLE / ESP-IDF 在不同层面对 128-bit UUID 的字节序理解可能让初学者困惑。
        因此这里显式把“是否反转”作为参数，让转换逻辑一眼可见。
    """
    data = raw[::-1] if reverse else raw
    return str(UUID(bytes=data)).lower()


# 默认使用“更贴近固件侧写法”的 UUID 解释方式。
DEFAULT_SERVICE_UUID = _uuid_from_esp_bytes(SERVICE_UUID_BYTES, reverse=True)
DEFAULT_CHARACTERISTIC_UUID = _uuid_from_esp_bytes(CHARACTERISTIC_UUID_BYTES, reverse=True)

# 为了兼容上下位机在 UUID 字节序理解上的细微差异，
# 这里把两种解释结果都保留，作为候选匹配集合。
SERVICE_UUID_CANDIDATES = tuple(
    dict.fromkeys(
        [
            DEFAULT_SERVICE_UUID,
            _uuid_from_esp_bytes(SERVICE_UUID_BYTES, reverse=False),
        ]
    )
)
CHARACTERISTIC_UUID_CANDIDATES = tuple(
    dict.fromkeys(
        [
            DEFAULT_CHARACTERISTIC_UUID,
            _uuid_from_esp_bytes(CHARACTERISTIC_UUID_BYTES, reverse=False),
        ]
    )
)

# ==================== PT1000 换算配置 ====================
# 当前按以下接法解释 VTEM 通道：
#   3.3V -> 3.3k -> ADC 节点 -> PT1000 -> GND
PT1000_DIVIDER_SUPPLY_V = 3.302
PT1000_DIVIDER_SERIES_OHM = 3300.0
PT1000_R0_OHM = 1000.0
PT1000_COEFF_A = 3.9083e-3
PT1000_COEFF_B = -5.775e-7
PT1000_COEFF_C = -4.183e-12

# ==================== FlexiForce A201 换算配置 ====================
# 当前按以下运放电路解释 VA201 通道：
#   A201 -> 反相输入，0.5V 基准接同相输入，反馈电阻 39kΩ
A201_REFERENCE_V = 0.5
A201_FEEDBACK_RESISTANCE_OHM = 39000.0

# ==================== VBAT 分压换算配置 ====================
# 当前按以下电阻顺序解释 VBAT 通道：
#   VBAT -> 100kΩ -> ADC 节点 -> 39kΩ -> GND
VBAT_DIVIDER_TOP_OHM = 100000.0
VBAT_DIVIDER_BOTTOM_OHM = 39000.0


def pt1000_resistance_from_divider_voltage_v(voltage_v: float) -> float:
    """把 PT1000 分压节点电压换算为 PT1000 阻值。

    当前约定的分压接法为：
        3.3V -> 3.3k -> ADC 节点 -> PT1000 -> GND
    """
    if not 0.0 <= voltage_v < PT1000_DIVIDER_SUPPLY_V:
        raise ValueError(
            f"divider voltage must be within [0, {PT1000_DIVIDER_SUPPLY_V}), got {voltage_v}"
        )

    return PT1000_DIVIDER_SERIES_OHM * voltage_v / (PT1000_DIVIDER_SUPPLY_V - voltage_v)


def _pt1000_resistance_from_temperature_c(temperature_c: float) -> float:
    """按 Callendar-Van Dusen 正向方程计算 PT1000 理论阻值。"""
    if temperature_c >= 0.0:
        return PT1000_R0_OHM * (
            1.0 + PT1000_COEFF_A * temperature_c + PT1000_COEFF_B * temperature_c * temperature_c
        )

    return PT1000_R0_OHM * (
        1.0
        + PT1000_COEFF_A * temperature_c
        + PT1000_COEFF_B * temperature_c * temperature_c
        + PT1000_COEFF_C * (temperature_c - 100.0) * temperature_c * temperature_c * temperature_c
    )


def _pt1000_resistance_derivative_below_zero(temperature_c: float) -> float:
    """求 PT1000 负温区方程对温度的一阶导数。"""
    return PT1000_R0_OHM * (
        PT1000_COEFF_A
        + 2.0 * PT1000_COEFF_B * temperature_c
        + PT1000_COEFF_C * (4.0 * temperature_c**3 - 300.0 * temperature_c**2)
    )


def pt1000_temperature_from_resistance_ohm(resistance_ohm: float) -> float:
    """把 PT1000 阻值换算为摄氏温度。"""
    if resistance_ohm <= 0.0:
        raise ValueError(f"PT1000 resistance must be positive, got {resistance_ohm}")

    if resistance_ohm >= PT1000_R0_OHM:
        discriminant = PT1000_COEFF_A**2 - 4.0 * PT1000_COEFF_B * (1.0 - resistance_ohm / PT1000_R0_OHM)
        if discriminant < 0.0:
            raise ValueError(f"invalid PT1000 resistance for positive-temperature branch: {resistance_ohm}")

        return (-PT1000_COEFF_A + discriminant**0.5) / (2.0 * PT1000_COEFF_B)

    temperature_c = (resistance_ohm - PT1000_R0_OHM) / (PT1000_R0_OHM * PT1000_COEFF_A)
    for _ in range(12):
        error = _pt1000_resistance_from_temperature_c(temperature_c) - resistance_ohm
        derivative = _pt1000_resistance_derivative_below_zero(temperature_c)
        if abs(derivative) < 1e-12:
            raise ValueError("PT1000 temperature iteration failed because derivative is too small")
        temperature_c -= error / derivative

    return temperature_c


def pt1000_temperature_from_divider_voltage_v(voltage_v: float) -> float:
    """把 PT1000 分压节点电压直接换算为摄氏温度。"""
    resistance_ohm = pt1000_resistance_from_divider_voltage_v(voltage_v)
    return pt1000_temperature_from_resistance_ohm(resistance_ohm)


def a201_resistance_from_output_voltage_v(voltage_v: float) -> float:
    """按当前运放电路把 VA201 输出电压反算为 A201 等效电阻。"""
    voltage_delta_v = voltage_v - A201_REFERENCE_V
    if voltage_delta_v <= 0.0:
        raise ValueError(
            f"A201 output voltage must be greater than {A201_REFERENCE_V} V, got {voltage_v}"
        )

    return A201_FEEDBACK_RESISTANCE_OHM * A201_REFERENCE_V / voltage_delta_v


def vbat_source_voltage_from_adc_voltage_v(voltage_v: float) -> float:
    """按 100k/39k 分压把 VBAT ADC 节点电压还原为电池端电压。"""
    divider_ratio = (VBAT_DIVIDER_TOP_OHM + VBAT_DIVIDER_BOTTOM_OHM) / VBAT_DIVIDER_BOTTOM_OHM
    return voltage_v * divider_ratio


class ProtocolError(ValueError):
    """协议错误异常。

    当收到的数据帧不满足本项目协议要求时，就抛出这个异常。

    为什么不直接返回 `None`：
    - 把“数据非法”显式当成异常，可以让上层统计无效帧和错误原因
    - 调用者能区分“没有数据”和“有数据但格式错误”这两种情况
    """


@dataclass(slots=True)
class AdcFrame:
    """一帧已经解析完成的 ADC 数据对象。

    函数作用：
        把原本没有语义的字节序列，转换成一个字段明确、可直接使用的数据对象。

    调用时机：
        由 `parse_frame()` 在协议解析成功后创建，随后会被 BLE、GUI、绘图、CSV 模块复用。

    参数含义：
        每个字段都对应协议中的一个明确含义，下面已逐项解释。

    返回值含义：
        这是数据类定义本身，不直接返回值；实例化后就是“一帧结构化数据”。

    是否会修改全局状态：
        不会。它是纯数据载体。

    与其他模块/任务/回调的关系：
        - BLE 模块负责产出它
        - GUI 模块负责消费它
        - 绘图模块和 CSV 模块都依赖它提供具名字段

    设计说明：
        使用 `@dataclass(slots=True)` 有两个教学上的好处：
        1. 字段列表直观，读者一眼就能看懂一帧数据包含什么
        2. `slots=True` 能避免随意给实例增加新属性，更接近“固定结构体”的感觉
    """

    # 电脑端真正收到这一帧的本地时间。
    # 这个时间不是来自固件，而是来自上位机本机时钟，主要用于日志与保存 CSV。
    pc_recv_time: datetime

    # 协议版本号。
    # 这个字段帮助上位机确认当前收到的帧是否使用自己理解的协议版本。
    protocol_version: int

    # 通道数量。
    # 当前固定为 3，但保留这个字段有助于后续扩展或做一致性检查。
    channel_count: int

    # 帧序号。
    # 由固件递增，用于观察丢帧、重连后重新计数、数据连续性等现象。
    frame_id: int

    # 固件侧时间戳，单位毫秒。
    # 这个值来自 ESP32 上运行中的时钟，不等于电脑本地时间。
    timestamp_ms: int

    # 3 路通道的电压值，单位 mV。
    # 字段顺序必须与固件打包顺序保持一致。
    vtem_mv: int
    va201_mv: int
    vbat_mv: int

    # CRC16 字段。
    # 当前第一版协议保留了这个字段，但暂时不做真实校验。
    crc16: int

    @property
    def channels_mv(self) -> tuple[int, int, int]:
        """按固定顺序返回 3 路通道的毫伏值。

        函数作用：
            给绘图、日志或后续批量处理提供统一顺序的数据接口。

        调用时机：
            当上层代码不想逐个写字段名，而是想按“通道列表”方式遍历时调用。

        返回值含义：
            返回一个三元组，顺序固定为：VTEM、VA201、VBAT。
        """
        return (self.vtem_mv, self.va201_mv, self.vbat_mv)

    @property
    def channels_v(self) -> tuple[float, float, float]:
        """把 3 路毫伏值统一换算成伏特值。

        设计说明：
            BLE 线上传输时使用整数 mV 更稳定、更节省协议设计复杂度；
            但上位机显示和绘图时，用 V 更符合直觉。
            因此这里提供一个只读转换属性，避免上层模块重复写 `/ 1000.0`。
        """
        return tuple(value / 1000.0 for value in self.channels_mv)

    @property
    def vtem_pt1000_resistance_ohm(self) -> float:
        """把 VTEM 通道按 PT1000 分压模型换算为阻值。"""
        return pt1000_resistance_from_divider_voltage_v(self.vtem_mv / 1000.0)

    @property
    def vtem_temperature_c(self) -> float:
        """把 VTEM 通道按 PT1000 分压模型换算为温度。"""
        return pt1000_temperature_from_resistance_ohm(self.vtem_pt1000_resistance_ohm)

    @property
    def va201_resistance_ohm(self) -> float:
        """把 VA201 通道按 A201 运放模型换算为传感器等效阻值。"""
        return a201_resistance_from_output_voltage_v(self.va201_mv / 1000.0)

    @property
    def vbat_source_voltage_v(self) -> float:
        """把 VBAT ADC 节点电压还原为电池端电压。"""
        return vbat_source_voltage_from_adc_voltage_v(self.vbat_mv / 1000.0)

    def try_vtem_pt1000_metrics(self) -> tuple[float | None, float | None]:
        """安全获取 VTEM 的 PT1000 阻值和温度。

        返回值含义：
            (阻值, 温度)；当分压值或温度反解超出模型可接受范围时，
            对应项返回 None，而不是把异常继续抛给界面层或 CSV 导出层。
        """
        try:
            resistance_ohm = self.vtem_pt1000_resistance_ohm
        except ValueError:
            return None, None

        try:
            temperature_c = pt1000_temperature_from_resistance_ohm(resistance_ohm)
        except ValueError:
            return resistance_ohm, None

        return resistance_ohm, temperature_c

    def try_va201_resistance_ohm(self) -> float | None:
        """安全获取 A201 等效阻值。"""
        try:
            return self.va201_resistance_ohm
        except ValueError:
            return None


@dataclass(slots=True)
class FrameStats:
    """上位机运行统计信息结构。

    这个数据类不是协议的一部分，而是上位机内部运行状态的抽象：
    它帮助界面统一显示当前收包情况、最近帧信息和错误状态。
    """

    # 成功解析的帧数量。
    valid_frames: int = 0

    # 收到但未通过协议校验的无效帧数量。
    invalid_frames: int = 0

    # 根据 frame_id 连续性估算出来的丢帧数量。
    dropped_frames: int = 0

    # 当前估算出来的帧率（帧/秒）。
    frame_rate: float = 0.0

    # 最近一次成功解析到的帧序号。
    last_frame_id: int | None = None

    # 最近一次成功解析到的固件时间戳。
    last_timestamp_ms: int | None = None

    # 最近一次错误信息字符串。
    last_error: str = ""


def normalize_uuid(uuid_text: str) -> str:
    """把任意合法 UUID 字符串规范化成统一格式。

    函数作用：
        把可能大小写不同、格式写法略有差异的 UUID 表示，统一转成标准小写字符串。

    设计意义：
        这样后续比较时，不需要担心 `ABC` 和 `abc` 这种纯格式差异。
    """
    return str(UUID(uuid_text)).lower()


def uuid_matches(candidate: str, accepted: Iterable[str]) -> bool:
    """判断某个 UUID 是否属于允许列表。

    函数作用：
        用统一格式比较一个候选 UUID 是否匹配一组允许的 UUID 候选值。

    调用时机：
        BLE 服务发现阶段，用于判断某个 service / characteristic 是否就是我们期望的目标。

    参数含义：
        candidate：待检查的 UUID 字符串。
        accepted：允许匹配的 UUID 集合。

    返回值含义：
        True：匹配成功。
        False：不匹配，或者 candidate 本身就不是合法 UUID。

    设计说明：
        这里故意把异常吃掉并返回 False，原因是：
        在设备服务发现阶段，我们更希望“稳健跳过异常值”，而不是让整个扫描流程崩掉。
    """
    try:
        normalized = normalize_uuid(candidate)
    except (ValueError, AttributeError, TypeError):
        return False
    return normalized in {normalize_uuid(item) for item in accepted}


def validate_crc16(_payload: bytes) -> bool:
    """CRC16 校验预留接口。

    函数作用：
        为未来真实 CRC 校验保留统一入口。

    调用时机：
        在 `parse_frame()` 中，在帧头、长度、版本检查之后调用。

    返回值含义：
        当前版本恒返回 True，表示暂不校验。

    设计说明：
        先把接口位置留好，是一种很典型的工程做法：
        第一版先保证链路跑通，后续增强时不需要重构解析主流程。
    """
    return True


def parse_frame(payload: bytes, recv_time: datetime | None = None) -> AdcFrame:
    """把 18 字节 BLE Notify 数据解析成结构化帧对象。

    函数作用：
        对收到的原始 `bytes` 做协议层校验和字段提取，返回 `AdcFrame`。

    调用时机：
        每次 BLE Notify 回调收到一帧原始数据时，由 BLE 模块调用。

    参数含义：
        payload：原始字节序列，理论上应该正好是 18 字节。
        recv_time：可选的接收时间。如果调用方不传，则自动用当前本机时间。

    返回值含义：
        成功时返回一个 `AdcFrame` 实例。
        失败时抛出 `ProtocolError`，由上层决定如何统计和显示错误。

    是否会修改全局状态：
        不会。它是纯函数式解析逻辑。

    与其他模块/任务/回调的关系：
        - 输入来自 `ble_client.py` 的通知回调
        - 输出被 `main_window.py`、`plot_widget.py`、`data_logger.py` 继续使用

    解析流程说明：
        1. 检查长度是否为固定 18 字节
        2. 检查帧头 0xAA 0x55
        3. 检查协议版本是否匹配
        4. 检查通道数是否匹配
        5. 预留 CRC 校验入口
        6. 按 little-endian 提取各字段

    为什么按这个顺序检查：
        因为这是“从最外层结构，到最内层语义”的典型校验顺序。
        越前面的检查越便宜，也越能快速淘汰明显错误的数据。
    """
    # 长度不对，说明连“这是不是一帧完整数据”都无法成立，直接拒绝。
    if len(payload) != FRAME_LENGTH:
        raise ProtocolError(f"invalid frame length: expected {FRAME_LENGTH}, got {len(payload)}")

    # 帧头不对，说明这很可能不是我们定义的协议帧。
    if payload[:2] != FRAME_HEADER:
        raise ProtocolError(
            f"invalid frame header: expected {FRAME_HEADER.hex()}, got {payload[:2].hex()}"
        )

    # 先取出协议层最基础的两个控制字段。
    protocol_version = payload[2]
    channel_count = payload[3]

    # 版本不匹配时直接中止，避免把旧协议的 raw 数据误当成新协议的电压值。
    if protocol_version != EXPECTED_PROTOCOL_VERSION:
        raise ProtocolError(
            f"invalid protocol version: expected 0x{EXPECTED_PROTOCOL_VERSION:02X}, got 0x{protocol_version:02X}"
        )

    # 当前桌面端只接受固定 3 通道帧。
    if channel_count != EXPECTED_CHANNEL_COUNT:
        raise ProtocolError(
            f"invalid channel count: expected {EXPECTED_CHANNEL_COUNT}, got {channel_count}"
        )

    # 预留 CRC 检查位置。
    # 当前 validate_crc16() 恒返回 True，但这个调用位置已经固定下来。
    if not validate_crc16(payload):
        raise ProtocolError("crc16 validation failed")

    # 如果调用者没有显式传入接收时间，就用电脑当前时间补上。
    recv_time = recv_time or datetime.now()

    # Python 的 int.from_bytes(..., byteorder="little")
    # 正好对应固件端 `ble_write_u16_le()` / `ble_write_u32_le()` 的打包方式。
    # 这就是上下位机在“字节序”上的一一对应关系。
    return AdcFrame(
        pc_recv_time=recv_time,
        protocol_version=protocol_version,
        channel_count=channel_count,
        frame_id=int.from_bytes(payload[4:6], byteorder="little", signed=False),
        timestamp_ms=int.from_bytes(payload[6:10], byteorder="little", signed=False),
        vtem_mv=int.from_bytes(payload[10:12], byteorder="little", signed=False),
        va201_mv=int.from_bytes(payload[12:14], byteorder="little", signed=False),
        vbat_mv=int.from_bytes(payload[14:16], byteorder="little", signed=False),
        crc16=int.from_bytes(payload[16:18], byteorder="little", signed=False),
    )
