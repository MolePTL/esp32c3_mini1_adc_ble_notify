#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "hal/adc_types.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_oneshot.h"
#include "app_config.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * adc_task.h
 * --------------------------------------------------------------------------
 * 这是 ADC 采样模块对外暴露的接口头文件。
 *
 * 模块职责：
 * 1. 初始化 ADC oneshot 驱动
 * 2. 周期读取 4 路 ADC 数据
 * 3. 尝试把 raw 值换算成 mV
 * 4. 维护“最近一次完整采样快照”供 BLE 模块读取
 *
 * 之所以把 ADC 做成独立模块，而不是直接在 BLE 代码里采样，原因是：
 * - 采样逻辑与通信逻辑职责不同
 * - 以后如果 BLE 改成串口/Wi-Fi，ADC 模块仍然可以复用
 * - 初学者更容易理解“生产者（采样）/消费者（发送）”的结构
 */

/*
 * 统一定义 4 路采样在数组中的逻辑索引。
 *
 * 设计意图：
 * 这里不是简单写 0、1、2、3，而是显式给每一路命名，原因有三点：
 * 1. ADC 模块内部顺序固定，避免写魔法数字
 * 2. BLE 打包顺序固定，避免上下位机字段错位
 * 3. 上位机解析顺序固定，便于端到端一致
 *
 * 也就是说，这个枚举本质上是“协议顺序定义”，不只是数组下标。
 */
typedef enum {
    /* 第 0 路：板上定义为 VTEM。 */
    ADC_SAMPLE_INDEX_VTEM = 0,

    /* 第 1 路：板上定义为 VM。 */
    ADC_SAMPLE_INDEX_VM,

    /* 第 2 路：板上定义为 VA201。 */
    ADC_SAMPLE_INDEX_VA201,

    /* 第 3 路：板上定义为 VBAT。 */
    ADC_SAMPLE_INDEX_VBAT,
} adc_sample_index_t;

/*
 * 保存“最近一次完整采样”的快照。
 *
 * 设计意图：
 * - ADC 任务不断采样，并把最新结果写入这份结构
 * - BLE 任务只读取这份结构，不直接操作 ADC 驱动
 *
 * 这样做的好处：
 * 1. 降低模块耦合
 * 2. 读写边界清晰
 * 3. 共享数据量固定、结构简单，便于用临界区保护
 */
typedef struct {
    /*
     * 这次采样对应的时间戳，单位毫秒。
     * 时间源来自 esp_timer_get_time()，在 adc_task.c 中生成。
     */
    uint32_t timestamp_ms;

    /*
     * 4 路通道的电压值，单位 mV。
     * 数组下标必须使用 adc_sample_index_t，不能随意假设顺序。
     */
    uint16_t voltage_mv[APP_ADC_CHANNEL_COUNT];
} adc_latest_sample_t;

/*
 * 函数作用：
 *   初始化 ADC 驱动并创建后台采样任务。
 *
 * 调用时机：
 *   系统启动阶段，由 app_main() 调用一次。
 *
 * 参数含义：
 *   无。
 *
 * 返回值含义：
 *   - ESP_OK：ADC 驱动初始化成功，采样任务创建成功
 *   - ESP_FAIL 或其他错误码：初始化失败
 *
 * 是否会修改全局状态：
 *   会初始化模块内部的静态句柄、校准状态和共享采样快照，
 *   并创建一个永久运行的 FreeRTOS 任务。
 *
 * 与其他模块/任务/回调的关系：
 *   - 被 app_main() 调用
 *   - 创建出的采样任务会持续更新快照
 *   - BLE 模块后续通过 adc_task_get_latest() 读取快照
 */
esp_err_t adc_task_init(void);

/*
 * 函数作用：
 *   获取“最近一次完整采样”的拷贝。
 *
 * 调用时机：
 *   任何需要读取最新 ADC 数据的模块都可以调用；当前主要由 BLE 模块调用。
 *
 * 参数含义：
 *   out_sample：输出参数，调用成功后写入一份采样快照。
 *
 * 返回值含义：
 *   - ESP_OK：获取成功
 *   - ESP_ERR_INVALID_ARG：传入的指针为空
 *
 * 是否会修改全局状态：
 *   不会修改采样内容本身，只会在临界区中读取共享快照。
 *
 * 与其他模块/任务/回调的关系：
 *   这个函数是 ADC 模块和 BLE 模块之间最核心的数据接口。
 */
esp_err_t adc_task_get_latest(adc_latest_sample_t *out_sample);

/*
 * 函数作用：
 *   把单次 ADC raw 结果换算为毫伏值（mV）。
 *
 * 调用时机：
 *   当前由 ADC 采样任务在每次采样后调用；
 *   也可以被后续标定、调试代码复用。
 *
 * 参数含义：
 *   channel_index：逻辑通道索引，用于找到对应通道的校准句柄
 *   raw：原始 ADC 读数
 *   out_mv：输出参数，返回换算后的毫伏值
 *
 * 返回值含义：
 *   - ESP_OK：成功完成校准换算
 *   - ESP_ERR_INVALID_ARG：参数非法
 *   - ESP_ERR_NOT_SUPPORTED：当前通道没有可用校准句柄
 *   - 其他错误：由 adc_cali_raw_to_voltage() 返回
 *
 * 是否会修改全局状态：
 *   不修改模块状态，只读取已初始化好的校准句柄。
 *
 * 与其他模块/任务/回调的关系：
 *   这是 ADC 校准逻辑的集中入口。
 *   当前项目在校准不可用时，会在 .c 文件里退化到近似换算。
 */
esp_err_t adc_task_raw_to_mv(adc_sample_index_t channel_index, uint16_t raw, int *out_mv);

/*
 * 函数作用：
 *   把 PT1000 分压节点电压换算为 PT1000 阻值（欧姆）。
 *
 * 接法约定：
 *   3.3V -> 3.3k -> ADC 节点 -> PT1000 -> GND
 *
 * 参数含义：
 *   voltage_mv：ADC 节点电压，单位 mV
 *   out_ohm：输出参数，成功时写入计算得到的 PT1000 阻值
 *
 * 返回值含义：
 *   - ESP_OK：换算成功
 *   - ESP_ERR_INVALID_ARG：参数非法，或电压超出当前分压模型可解释范围
 */
esp_err_t adc_task_mv_to_pt1000_resistance_ohm(uint16_t voltage_mv, float *out_ohm);

/*
 * 函数作用：
 *   把 PT1000 分压节点电压直接换算为温度（摄氏度）。
 *
 * 设计说明：
 *   内部会先把电压换算为阻值，再按 PT1000 的 Callendar-Van Dusen 公式求温度。
 *   这样固件和上位机都可以复用同一组参数与公式。
 *
 * 参数含义：
 *   voltage_mv：ADC 节点电压，单位 mV
 *   out_temp_c：输出参数，成功时写入摄氏温度
 *
 * 返回值含义：
 *   - ESP_OK：换算成功
 *   - ESP_ERR_INVALID_ARG：参数非法
 *   - ESP_FAIL：数值迭代失败
 */
esp_err_t adc_task_mv_to_pt1000_temperature_c(uint16_t voltage_mv, float *out_temp_c);

#ifdef __cplusplus
}
#endif
