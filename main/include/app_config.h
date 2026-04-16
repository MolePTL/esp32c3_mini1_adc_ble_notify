#pragma once

#include <stdint.h>
#include "hal/adc_types.h"

/*
 * app_config.h
 * --------------------------------------------------------------------------
 * 这个头文件存放“整个项目的集中配置宏”。
 *
 * 设计意图：
 * 1. 把容易调整的工程参数统一收口，避免散落在多个 .c 文件里
 * 2. 让业务代码更多表达“做什么”，而不是到处硬编码常量
 * 3. 当你后续想改采样周期、BLE 名称、UUID、GPIO 映射、任务栈大小时，
 *    优先在这里调整，而不是逐文件搜索替换
 *
 * 注意：
 * 这个文件只放“配置语义”，不放具体业务逻辑。
 * 换句话说，它回答的是“系统应该如何配置”，而不是“系统如何运行”。
 */

/*
 * ==================== ADC 配置 ====================
 * 这一组宏控制 ADC 模块的硬件映射与采样节奏。
 *
 * 工程意义：
 * - 采哪几路
 * - 采样周期是多少
 * - ADC 衰减配置是什么
 * - 逻辑通道与板上 GPIO 如何对应
 */

/*
 * 当前固件固定采 4 路 ADC。
 * 这个宏不仅影响 ADC 模块本身，还会影响：
 * - BLE 协议里的“通道数”字段
 * - 数据缓存数组长度
 * - 上位机按固定 4 路解析的逻辑
 */
#define APP_ADC_CHANNEL_COUNT              4

/*
 * ADC 采样周期，单位 ms。
 * 这里配置为 10ms，意味着 ADC 任务大约每 10ms 采一次 4 路数据。
 *
 * 它影响：
 * - 数据时间分辨率
 * - CPU 占用
 * - BLE 侧能拿到的新鲜数据密度
 */
#define APP_ADC_SAMPLE_PERIOD_MS           10

/*
 * ADC 衰减配置。
 * ADC_ATTEN_DB_12 是 ESP-IDF 推荐的一个较常用衰减档位，
 * 用于扩展输入电压测量范围。
 *
 * 这个宏会传给 adc_oneshot_config_channel() 和 ADC 校准配置，
 * 因此必须保证驱动配置和校准配置保持一致。
 */
#define APP_ADC_ATTEN                      ADC_ATTEN_DB_12

/*
 * 采样通道与原理图信号对应关系。
 *
 * 这里的宏值表示物理 GPIO 号，而不是 ADC 通道号。
 * 真正的 ADC 通道号映射在 adc_task.c 里的 s_channels[] 表中定义。
 *
 * 之所以拆成两层：
 * - 这里保留“板级连接关系”
 * - adc_task.c 保留“驱动层通道关系”
 * 这样教学上更清楚，也便于以后换板时单独调整。
 */
#define APP_ADC_GPIO_VTEM                  0
#define APP_ADC_GPIO_VM                    1
#define APP_ADC_GPIO_VA201                 3
#define APP_ADC_GPIO_VBAT                  4

/*
 * ==================== PT1000 配置 ====================
 * 当前温度通道使用 PT1000 + 3.3k 分压。
 *
 * 这里约定的接法为：
 *   3.3V -> 3.3k -> ADC 节点 -> PT1000 -> GND
 *
 * 因此 ADC 采到的是 PT1000 两端的电压，后续可按分压公式先还原阻值，
 * 再按 IEC 60751 / Callendar-Van Dusen 公式换算温度。
 */
#define APP_PT1000_DIVIDER_SUPPLY_MV       3300.0f
#define APP_PT1000_DIVIDER_SERIES_OHM      3300.0f
#define APP_PT1000_R0_OHM                  1000.0f
#define APP_PT1000_COEFF_A                 3.9083e-3f
#define APP_PT1000_COEFF_B                -5.775e-7f
#define APP_PT1000_COEFF_C                -4.183e-12f

/*
 * ==================== BLE 配置 ====================
 * 这一组宏控制 BLE 广播、设备标识和协议版本。
 *
 * 工程意义：
 * - 手机/电脑扫描时看到什么设备名
 * - Notify 大约多久发送一次
 * - 上下位机如何确认协议是否一致
 * - 自定义 Service / Characteristic 的 UUID 是什么
 */

/*
 * 电脑端扫描时看到的设备名。
 *
 * 注意：
 * 这是 BLE 广播中的“设备名字符串”，主要用于人眼识别；
 * 真正可靠的协议识别仍然建议依赖自定义 UUID。
 */
#define APP_BLE_DEVICE_NAME                "C3-ADC-BLE"

/*
 * BLE Notify 周期，单位 ms。
 * 当前配置为 10ms，表示 BLE 发送任务大约每 10ms 尝试发送一帧。
 *
 * 它与 ADC 采样周期保持一致时，比较容易做到“每次发送都是较新的采样值”。
 * 但这里并不保证严格一一对应，因为采样和发送是两个独立 FreeRTOS 任务。
 */
#define APP_BLE_NOTIFY_PERIOD_MS           10

/*
 * 二进制帧协议版本号。
 *
 * 当前版本为 0x02，表示通道载荷字段语义为“毫伏值 mV”。
 * 如果以后协议格式变化，例如：
 * - 改字段顺序
 * - 改数据单位
 * - 改长度
 * - 增加 CRC 校验规则
 * 都应该同步提升版本号，避免上位机误解析。
 */
#define APP_BLE_FRAME_VERSION              0x02

/*
 * 自定义 128-bit Service UUID。
 *
 * 这里使用字节数组字面量，是因为 ESP-IDF 的 Bluedroid GATT 接口
 * 常直接以 16 字节数组形式描述 128-bit UUID。
 *
 * 这组字节会在 ble_service.c 中直接用于 GATT 数据库定义。
 */
#define APP_BLE_SERVICE_UUID_BYTES         \
    0x10, 0x32, 0x54, 0x76, 0x98, 0xba, 0xdc, 0xfe, \
    0x12, 0x34, 0x56, 0x78, 0x9a, 0xbc, 0xde, 0xf0

/*
 * 自定义 Notify Characteristic UUID。
 *
 * 电脑端连接后，会在这个 Characteristic 上订阅 Notify。
 * 当前项目只有一个主要数据通道，因此只需要一个 Notify 特征值就够了。
 */
#define APP_BLE_CHARACTERISTIC_UUID_BYTES  \
    0x21, 0x43, 0x65, 0x87, 0xa9, 0xcb, 0xed, 0x0f, \
    0x13, 0x57, 0x9b, 0xdf, 0x24, 0x68, 0xac, 0xe0

/*
 * ==================== 数据帧配置 ====================
 * 这一组宏描述上位机与固件之间的固定二进制协议格式。
 *
 * 当前帧总长度固定为 20 字节，布局在 BLE 打包函数中实现。
 * 使用固定长度的好处是：
 * - 上位机解析简单
 * - 内存管理简单
 * - 调试时更容易定位字段偏移
 */

/* 固定帧头第 1 字节，用于识别协议起始。 */
#define APP_FRAME_HEAD0                    0xAA

/* 固定帧头第 2 字节，与第 1 字节组合形成同步头。 */
#define APP_FRAME_HEAD1                    0x55

/*
 * 当前 CRC16 默认填充值。
 * 第一版项目先把 CRC 字段保留出来，但实际校验逻辑尚未启用，
 * 因此统一填 0x0000。
 */
#define APP_FRAME_CRC16_DEFAULT            0x0000

/* 整帧固定长度，单位字节。 */
#define APP_FRAME_LEN_BYTES                20

/*
 * ==================== WS2812 配置 ====================
 * 这一组宏控制板载状态灯所使用的 GPIO 与 RMT 时基。
 *
 * 工程意义：
 * - 指定哪一个 GPIO 连接了单颗 WS2812
 * - 指定 RMT 外设以什么时间分辨率输出脉冲
 */

/* 板上单颗 WS2812 状态灯接在 GPIO8。 */
#define APP_WS2812_GPIO                    8

/*
 * RMT 时钟分辨率 10MHz，即 1 tick = 0.1us。
 *
 * 为什么这个值重要：
 * WS2812 是靠高低电平持续时间来区分 bit0 / bit1 的，
 * 因此 RMT 的时间分辨率越合适，编码越容易写得清楚。
 *
 * 10MHz 的好处：
 * - 0.1us 一个 tick，时间粒度比较直观
 * - 0.3us / 0.9us 这类 WS2812 常见时序可用整数 tick 表示
 */
#define APP_WS2812_RMT_RESOLUTION_HZ       10000000

/*
 * ==================== 任务配置 ====================
 * 这一组宏控制 FreeRTOS 任务的栈大小和优先级。
 *
 * 工程意义：
 * - 栈太小可能溢出
 * - 栈太大浪费 RAM
 * - 优先级决定任务在竞争 CPU 时谁更容易被调度
 *
 * 当前 ADC 任务和 BLE Notify 任务都使用相同优先级，
 * 表示设计上更偏向“并列后台工作者”，而不是谁绝对压过谁。
 */

/* ADC 采样任务的栈大小，单位是字节。 */
#define APP_ADC_TASK_STACK_SIZE            4096

/* ADC 采样任务的 FreeRTOS 优先级。 */
#define APP_ADC_TASK_PRIORITY              5

/* BLE Notify 发送任务的栈大小，单位是字节。 */
#define APP_BLE_NOTIFY_TASK_STACK_SIZE     4096

/* BLE Notify 发送任务的 FreeRTOS 优先级。 */
#define APP_BLE_NOTIFY_TASK_PRIORITY       5
