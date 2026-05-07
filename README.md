# ESP32-C3 Mini-1 ADC BLE Notify

这是一个基于 `ESP32-C3` 的三路 ADC 采样与 BLE Notify 数据采集项目，配套提供 Windows 上位机，用于扫描设备、连接订阅、实时显示波形、处理传感器数据、保存 CSV 数据，并把历史 CSV 导回上位机绘制总波形。

项目当前由两部分组成：

- 固件端：周期采样 3 路 ADC，并通过自定义 GATT Service/Characteristic 按固定 18 字节协议持续发送。
- PC 上位机：使用 `PySide6 + bleak + pyqtgraph` 实现 BLE 连接、协议解析、实时示波、滤波换算、CSV 保存和历史波形回放。

## 功能概览

- 3 路 ADC 周期采样，默认采样周期 `10 ms`（约 `100 Hz`）
- BLE 广播、连接、Notify 推送，默认发送周期 `10 ms`（约 `100 Hz`）
- 固定长度二进制协议，便于解析和调试
- 上位机实时显示 `VTEM / VA201 / VBAT` 三路波形，显示刷新率可在 `100 Hz` 和 `10 Hz` 间切换；`10 Hz` 显示按 10 帧截尾均值聚合，滤波开启时再做 2 Hz 一阶低通
- 支持自动跟随、自动量程、手动量程、单通道显示
- 支持按通道选择显示与保存，保存通道可跟随当前显示通道
- 支持实时保存 CSV、定时分段保存，或采集完成后手动导出 CSV；实时保存按 10 帧截尾均值聚合成约 `10 Hz`
- 支持导入单个 CSV 或整个 CSV 文件夹，绘制长时间总波形
- 历史导入默认优先使用滤波后的 `*_voltage_filtered_v` 列，缺失通道会自动隐藏
- 默认使用 5 点中值去尖峰 + 2 Hz EMA 低通滤波，界面可一键关闭滤波对比原始电压
- 运行状态区显示接收速率、有效帧数、无效帧数和基于 `frame_id` 的丢帧数
- 支持鼠标悬停读点、单击锁定读点，显示原始电压和转换后的物理量
- `VTEM` 通道内置 PT1000 分压换算，可显示阻值与温度，并支持导线电阻补偿
- `VA201` 通道可按 FlexiForce A201 运放模型换算等效阻值
- `VBAT` 通道可按分压模型还原电池端电压
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
│  ├─ csv_importer.py     # 历史 CSV 导入与合并
│  ├─ vtem_processor.py   # 去尖峰、EMA 滤波与派生物理量处理
│  └─ requirements.txt    # Python 依赖
├─ platformio.ini         # PlatformIO 构建配置
├─ run_pc_app.cmd         # 上位机启动脚本
└─ run_pc_app_debug.cmd   # 上位机调试启动脚本
```

## 硬件与引脚

当前固件默认采样 3 路 ADC，定义见 [`main/include/app_config.h`](main/include/app_config.h)。

| 信号 | GPIO | ADC 通道 | ADC 衰减 | 说明 |
| --- | --- | --- | --- | --- |
| `VTEM` | `GPIO0` | `ADC_CHANNEL_0` | `ADC_ATTEN_DB_6` | PT1000 分压采样，约 1.3V 档 |
| `VA201` | `GPIO3` | `ADC_CHANNEL_3` | `ADC_ATTEN_DB_12` | FlexiForce A201 相关电压采样，保持原量程 |
| `VBAT` | `GPIO4` | `ADC_CHANNEL_4` | `ADC_ATTEN_DB_6` | 电池分压采样，约 1.3V 档 |
| `WS2812` | `GPIO8` | - | - | 单颗状态灯 |

PT1000 默认分压模型如下：

```text
3.3V -> 3.3kΩ -> ADC 节点(VTEM) -> PT1000 -> GND
```

上位机中的传感器换算常量集中在 [`pc_app/protocol.py`](pc_app/protocol.py)。当前 `VA201` 按 A201 运放输出电压反算等效阻值，反馈电阻为 `39kΩ`；`VBAT` 按上拉 `100kΩ`、下拉 `39kΩ` 分压还原电池端电压。电池 `2.7~4.0V` 经该分压后，ADC 节点约为 `0.758~1.123V`，适合当前 VBAT 的 `ADC_ATTEN_DB_6` 档。

## 默认配置

关键默认参数同样集中放在 [`main/include/app_config.h`](main/include/app_config.h)：

- BLE 设备名：`C3-ADC-BLE`
- ADC 通道数：`3`
- ADC 采样周期：`10 ms`
- BLE Notify 周期：`10 ms`
- ADC 衰减量程：`VTEM / VBAT` 使用 `ADC_ATTEN_DB_6`（约 1.3V 档），`VA201` 保持 `ADC_ATTEN_DB_12`
- 协议版本：`0x03`
- 帧长度：`18 bytes`
- 当前 CRC16 字段保留，默认填 `0x0000`

## BLE 服务与协议

### UUID

- Service UUID：`f0debc9a-7856-3412-fedc-ba9876543210`
- Notify Characteristic UUID：`e0ac6824-df9b-5713-0fed-cba987654321`

### 数据帧格式

每一帧固定 `18` 字节，小端序：

| 字节偏移 | 长度 | 字段 | 说明 |
| --- | --- | --- | --- |
| `0..1` | 2 | Header | 固定 `0xAA 0x55` |
| `2` | 1 | Version | 当前 `0x03` |
| `3` | 1 | Channel Count | 当前固定 `3` |
| `4..5` | 2 | Frame ID | 帧序号，递增 |
| `6..9` | 4 | Timestamp | 固件运行时间，单位 `ms` |
| `10..11` | 2 | VTEM | 单位 `mV` |
| `12..13` | 2 | VA201 | 单位 `mV` |
| `14..15` | 2 | VBAT | 单位 `mV` |
| `16..17` | 2 | CRC16 | 当前保留，默认 `0x0000` |

通道顺序固定为：

```text
VTEM -> VA201 -> VBAT
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
6. 可按需要选择 `100 Hz` 或 `10 Hz` 显示刷新率，进行波形显示、实时保存或导出 CSV

### 5. 导入历史数据绘制总波形

上位机支持把已经保存或导出的 CSV 再导入回来，用于查看长时间采集的总波形：

1. 断开 BLE 连接，并停止实时保存
2. 点击“导入 CSV”选择一个或多个 CSV 文件，或点击“导入文件夹”选择保存目录
3. 保持“导入优先滤波列”勾选时，会优先绘制 `*_voltage_filtered_v`
4. 导入完成后，示波器会自动显示全部数据范围
5. 可继续使用通道开关、自动量程、拖动缩放和鼠标读点

导入时会按时间排序并去掉重复行。`frame_id` 可能回绕，因此历史总览不使用帧号作为横轴；有 `pc_recv_time` 时优先按 PC 接收时间排序，否则使用设备侧 `timestamp_ms`。

## CSV 数据说明

CSV 导出由 [`pc_app/data_logger.py`](pc_app/data_logger.py) 负责，基础列如下：

- `session_index`
- `pc_recv_time`
- `frame_id`
- `timestamp_ms`

开启“开始记录”实时保存时，CSV 会在上述基础列后增加聚合元数据列：

- `aggregation_method`
- `aggregation_sample_count`
- `frame_id_start`
- `frame_id_end`
- `timestamp_start_ms`
- `timestamp_end_ms`

按保存通道不同，还会附加这些列：

- `vtem_voltage_v`
- `vtem_resistance_ohm`
- `vtem_temperature_c`
- `vtem_voltage_filtered_v`
- `vtem_resistance_filtered_ohm`
- `vtem_resistance_compensated_ohm`
- `vtem_temperature_compensated_c`
- `vtem_median_filter_enabled`
- `vtem_wire_compensation_ohm`
- `va201_voltage_v`
- `va201_voltage_filtered_v`
- `va201_resistance_ohm`
- `va201_resistance_filtered_ohm`
- `vbat_voltage_v`
- `vbat_source_voltage_v`
- `vbat_voltage_filtered_v`
- `vbat_source_voltage_filtered_v`
- `*_filter_cutoff_hz`
- `*_spike_filter_mode`

实时保存不会逐帧写入 100Hz 数据，而是每 10 帧写 1 行。聚合算法固定为截尾均值：每个数值列在 10 个样本中去掉最大值和最小值，再对剩余 8 个样本求平均；滤波模式、截止频率、导线补偿等配置列使用该窗口最后一帧的值。不足 10 帧的尾部缓冲在停止记录或切分文件时丢弃。实时保存中，未带 `_filtered` 的数值列来自每帧原始电压或原始换算量后再聚合，带 `_filtered` 的数值列来自每帧 5 点中值 + 2 Hz EMA 后再聚合。手动“导出 CSV”仍导出内存缓存中的全速数据。

历史导入由 [`pc_app/csv_importer.py`](pc_app/csv_importer.py) 负责。它兼容早期没有 `session_index` 的 CSV，也兼容只保存部分通道的 CSV。

实时保存默认写入：

```text
data/YYYY-MM-DD/adc_capture_YYYYMMDD_HHMMSS_partNNN.csv
```

手动导出默认写入：

```text
data/YYYY-MM-DD/adc_export_YYYYMMDD_HHMMSS.csv
```

这些采集数据通常体积较大，仓库默认通过 `.gitignore` 忽略 `data/`、`adc_capture_*.csv` 和 `adc_export_*.csv`。如果需要归档原始实验数据，建议放到单独的数据盘、网盘或实验记录目录中，不随代码仓库提交。

## 可调配置

如果需要改设备名、UUID、采样周期、发送周期、引脚映射，优先修改：

- [`main/include/app_config.h`](main/include/app_config.h)

如果协议格式发生变化，需要同步修改：

- 固件打包逻辑：[`main/src/ble_service.c`](main/src/ble_service.c)
- 上位机解析逻辑：[`pc_app/protocol.py`](pc_app/protocol.py)

如果需要调整上位机的数据处理或传感器换算，优先查看：

- 滤波与导线补偿：[`pc_app/vtem_processor.py`](pc_app/vtem_processor.py)
- PT1000 / A201 / VBAT 换算常量：[`pc_app/protocol.py`](pc_app/protocol.py)

## 开发说明

- 固件端采用“ADC 采样任务”和“BLE Notify 任务”解耦设计
- 上位机使用 `Qt 主线程 + asyncio BLE 后台线程` 的桥接结构
- 上位机按 `BLE 通信 -> 协议解析 -> 数据处理 -> 绘图/CSV` 分层，便于单独调试
- 当前协议中 CRC 字段已预留，但尚未启用真实校验
- 上位机对 UUID 做了双字节序候选兼容，便于联调

## 参考入口

- 固件入口：[`main/main.c`](main/main.c)
- ADC 模块：[`main/src/adc_task.c`](main/src/adc_task.c)
- BLE 模块：[`main/src/ble_service.c`](main/src/ble_service.c)
- 上位机入口：[`pc_app/main.py`](pc_app/main.py)
- 协议定义：[`pc_app/protocol.py`](pc_app/protocol.py)
- 数据处理：[`pc_app/vtem_processor.py`](pc_app/vtem_processor.py)
