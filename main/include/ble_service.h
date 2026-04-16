#pragma once

#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * ble_service.h
 * --------------------------------------------------------------------------
 * 这是 BLE 服务模块对外暴露的接口头文件。
 *
 * 模块职责：
 * 1. 初始化 BLE 控制器和 Bluedroid 协议栈
 * 2. 创建自定义 GATT Service / Characteristic
 * 3. 负责广播、连接状态维护和 Notify 发送
 * 4. 把 ADC 模块提供的最新采样数据打包成固定协议帧发给上位机
 *
 * 这个模块相当于整个固件里的“无线数据出口”。
 */

/*
 * 函数作用：
 *   初始化 BLE 服务端、广播配置和后台 Notify 任务。
 *
 * 调用时机：
 *   系统启动阶段，由 app_main() 在 NVS 与 ADC 初始化之后调用一次。
 *
 * 参数含义：
 *   无。
 *
 * 返回值含义：
 *   - ESP_OK：初始化成功
 *   - ESP_FAIL 或其他 ESP-IDF 错误码：初始化失败
 *
 * 是否会修改全局状态：
 *   会初始化 BLE 协议栈、注册回调、创建 FreeRTOS 任务，
 *   并修改模块内部的连接/通知状态变量。
 *
 * 与其他模块/任务/回调的关系：
 *   - 依赖 NVS 已初始化
 *   - 依赖 ADC 模块已经可提供采样快照
 *   - 会调用 ws2812_status_set() 更新状态灯显示
 */
esp_err_t ble_service_init(void);

/*
 * 函数作用：
 *   返回当前是否已经建立 BLE 连接。
 *
 * 调用时机：
 *   供其他模块在需要时查询连接状态；当前项目里主要作为对外辅助接口保留。
 *
 * 参数含义：
 *   无。
 *
 * 返回值含义：
 *   true：当前存在活跃 BLE 连接
 *   false：当前未连接
 *
 * 是否会修改全局状态：
 *   不会；只在临界区中读取内部连接标志。
 *
 * 与其他模块/任务/回调的关系：
 *   该状态由 BLE GATTS 回调中的建链/断链事件维护。
 */
bool ble_service_is_connected(void);

#ifdef __cplusplus
}
#endif
