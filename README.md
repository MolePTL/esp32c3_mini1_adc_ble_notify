# ESP32-C3 Mini-1 ADC BLE Notify

这是一个基于 `ESP32-C3` 的四路 ADC 采样与 BLE Notify 演示项目，配套提供 Windows 上位机，用于扫描设备、连接订阅、实时显示波形，并导出 CSV 数据。

项目当前由两部分组成：

- 固件端：周期采样 4 路 ADC，并通过自定义 GATT Service/Characteristic 按固定 20 字节协议持续发送。
- PC 上位机：使用 `PySide6 + bleak + pyqtgraph` 实现 BLE 连接、协议解析、实时示波和 CSV 保存。

## 功能概览

- 4 路 ADC 周期采样，默认采样周期 `10 ms`
- BLE 广播、连接、Notify 推送，默认发送周期 `10 ms`
- 固定长度二进制协议，便于解析和调试
- 上位机实时显示 `VTEM / VM / VA201 / VBAT` 四路波形
- 支持自动跟随、自动量程、手动量程、单通道显示
- 支持实时保存 CSV，或采集完成后导出 CSV
- `VTEM` 通道内置 PT1000 分压换算，可显示阻值与温度
- 板载 `WS2812` 状态灯用于显示空闲、广播、连接、错误状态

## 仓库结构

```text
.
├─ main/                  # ESP32-C3 固件
│  ├─ include/            # 头文件与集中配置
│  ├─ src/                # ADC / BLE / WS2812 模块实现
│  └─ main.c              # 固件入口
├─ pc_app/                # Windows 上位机
│  ├─ main.py             # GUI 入口
│  ├─ ble_client.py       # BLE 异步桥接
│  ├─ protocol.py         # 协议定义与解析
│  ├─ plot_widget.py      # 实时波形控件
│  ├─ data_logger.py      # CSV 保存
│  └─ requirements.txt    # Python 依赖
├─ platformio.ini         # PlatformIO 构建配置
├─ run_pc_app.cmd         # 上位机启动脚本
├─ run_pc_app_debug.cmd   # 上位机调试启动脚本
└─ adc_voltage_capture.csv# 示例导出数据
```

## 硬件与引脚

当前固件默认采样 4 路 ADC，定义见 [`main/include/app_config.h`](main/include/app_config.h)。

| 信号 | GPIO | ADC 通道 | 说明 |
| --- | --- | --- | --- |
| `VTEM` | `GPIO0` | `ADC_CHANNEL_0` | PT1000 分压采样 |
| `VM` | `GPIO1` | `ADC_CHANNEL_1` | 电压采样 |
| `VA201` | `GPIO3` | `ADC_CHANNEL_3` | 电压采样 |
| `VBAT` | `GPIO4` | `ADC_CHANNEL_4` | 电压采样 |
| `WS2812` | `GPIO8` | - | 单颗状态灯 |

PT1000 默认分压模型如下：

```text
3.3V -> 3.3kΩ -> ADC 节点(VTEM) -> PT1000 -> GND
```

## 默认配置

关键默认参数同样集中放在 [`main/include/app_config.h`](main/include/app_config.h)：

- BLE 设备名：`C3-ADC-BLE`
- ADC 通道数：`4`
- ADC 采样周期：`10 ms`
- BLE Notify 周期：`10 ms`
- 协议版本：`0x02`
- 帧长度：`20 bytes`
- 当前 CRC16 字段保留，默认填 `0x0000`

## BLE 服务与协议

### UUID

- Service UUID：`f0debc9a-7856-3412-fedc-ba9876543210`
- Notify Characteristic UUID：`e0ac6824-df9b-5713-0fed-cba987654321`

### 数据帧格式

每一帧固定 `20` 字节，小端序：

| 字节偏移 | 长度 | 字段 | 说明 |
| --- | --- | --- | --- |
| `0..1` | 2 | Header | 固定 `0xAA 0x55` |
| `2` | 1 | Version | 当前 `0x02` |
| `3` | 1 | Channel Count | 当前固定 `4` |
| `4..5` | 2 | Frame ID | 帧序号，递增 |
| `6..9` | 4 | Timestamp | 固件运行时间，单位 `ms` |
| `10..11` | 2 | VTEM | 单位 `mV` |
| `12..13` | 2 | VM | 单位 `mV` |
| `14..15` | 2 | VA201 | 单位 `mV` |
| `16..17` | 2 | VBAT | 单位 `mV` |
| `18..19` | 2 | CRC16 | 当前保留，默认 `0x0000` |

通道顺序固定为：

```text
VTEM -> VM -> VA201 -> VBAT
```

## 固件编译与烧录

本项目使用 `PlatformIO` 管理，框架为 `ESP-IDF`。

### 1. 安装依赖

- 安装 VS Code + PlatformIO 扩展，或单独安装 PlatformIO Core
- 准备好 USB 驱动和串口连接

### 2. 编译

在项目根目录执行：

```powershell
pio run -e esp32-c3-mini-1
```

### 3. 烧录

```powershell
pio run -e esp32-c3-mini-1 -t upload
```

### 4. 查看串口日志

```powershell
pio device monitor -b 115200
```

固件启动顺序如下：

1. 初始化 `WS2812` 状态灯
2. 初始化 `NVS`
3. 初始化 `ADC` 采样任务
4. 初始化 `BLE` 服务与 Notify 任务
5. 进入后台运行

## 上位机运行

### 环境要求

- Windows 10/11
- 电脑具备 BLE 功能
- Python 3

### 1. 创建虚拟环境

```powershell
py -3 -m venv .venv
```

### 2. 安装依赖

```powershell
.venv\Scripts\pip install -r pc_app\requirements.txt
```

### 3. 启动上位机

方式一：

```powershell
.venv\Scripts\python -m pc_app.main
```

方式二：

```powershell
run_pc_app.cmd
```

调试方式：

```powershell
run_pc_app_debug.cmd
```

### 4. 使用流程

1. 给 ESP32-C3 上电，等待其进入 BLE 广播状态
2. 打开上位机，点击“扫描设备”
3. 从列表中选择 `C3-ADC-BLE`
4. 点击“连接”
5. 建链成功后，上位机会自动订阅 Notify 并开始实时收数
6. 可按需要进行波形显示、实时保存或导出 CSV

## CSV 导出说明

CSV 导出由 [`pc_app/data_logger.py`](pc_app/data_logger.py) 负责，基础列如下：

- `session_index`
- `pc_recv_time`
- `frame_id`
- `timestamp_ms`

按保存通道不同，还会附加这些列：

- `vtem_voltage_v`
- `vtem_resistance_ohm`
- `vtem_temperature_c`
- `vm_voltage_v`
- `va201_voltage_v`
- `vbat_voltage_v`

仓库根目录中的 `adc_voltage_capture.csv` 可作为导出结果示例。

## 可调配置

如果需要改设备名、UUID、采样周期、发送周期、引脚映射，优先修改：

- [`main/include/app_config.h`](main/include/app_config.h)

如果协议格式发生变化，需要同步修改：

- 固件打包逻辑：[`main/src/ble_service.c`](main/src/ble_service.c)
- 上位机解析逻辑：[`pc_app/protocol.py`](pc_app/protocol.py)

## 开发说明

- 固件端采用“ADC 采样任务”和“BLE Notify 任务”解耦设计
- 上位机使用 `Qt 主线程 + asyncio BLE 后台线程` 的桥接结构
- 当前协议中 CRC 字段已预留，但尚未启用真实校验
- 上位机对 UUID 做了双字节序候选兼容，便于联调

## 参考入口

- 固件入口：[`main/main.c`](main/main.c)
- ADC 模块：[`main/src/adc_task.c`](main/src/adc_task.c)
- BLE 模块：[`main/src/ble_service.c`](main/src/ble_service.c)
- 上位机入口：[`pc_app/main.py`](pc_app/main.py)
- 协议定义：[`pc_app/protocol.py`](pc_app/protocol.py)
