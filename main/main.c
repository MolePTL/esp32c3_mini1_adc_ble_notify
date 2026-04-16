/*
 * main.c
 * --------------------------------------------------------------------------
 * 这是整个固件的入口文件，作用类似桌面程序里的 main()。
 *
 * 在 ESP-IDF 中，用户通常不直接写传统 C 语言意义上的 main()，
 * 而是实现 app_main()。ESP-IDF 启动流程在完成底层启动代码、
 * 时钟初始化、堆栈准备等工作后，会自动调用 app_main()。
 *
 * 这个文件本身不承担复杂业务，而是负责把系统按正确顺序“拉起来”：
 * 1. 初始化 NVS（BLE 协议栈依赖它）
 * 2. 初始化状态灯（便于可视化观察系统状态）
 * 3. 初始化 ADC 采样模块
 * 4. 初始化 BLE 服务模块
 * 5. 让主任务进入低负载保活状态
 *
 * 之所以这样设计，而不是把所有业务都塞进 app_main()，
 * 是为了让不同功能模块各司其职：
 * - ADC 模块自己创建采样任务
 * - BLE 模块自己创建发送任务和注册回调
 * - app_main() 只负责统一启动顺序和全局初始化依赖
 */

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_err.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "adc_task.h"
#include "ble_service.h"
#include "ws2812_status.h"
#include "app_config.h"

/*
 * ESP_LOGI / ESP_LOGE 等日志宏通常会带一个 TAG，
 * 这样串口日志里就能知道消息来自哪个模块。
 *
 * 这里把 TAG 设为 "APP_MAIN"，表示这些日志来自系统入口模块。
 * static 说明它只在当前源文件可见；
 * const char * 表示它指向一个只读字符串常量。
 */
static const char *TAG = "APP_MAIN";

/*
 * 函数作用：
 *   初始化 NVS（Non-Volatile Storage，非易失性存储）。
 *
 * 调用时机：
 *   在 app_main() 中、BLE 初始化之前调用。
 *
 * 参数含义：
 *   无。
 *
 * 返回值含义：
 *   - ESP_OK：初始化成功
 *   - 其他错误码：初始化失败，错误含义由 ESP-IDF 的 NVS 组件定义
 *
 * 是否修改全局状态：
 *   会修改芯片内部 Flash 上的 NVS 区域状态；在特定错误场景下还可能执行擦除。
 *
 * 与其他模块/任务/回调的关系：
 *   BLE 协议栈依赖 NVS 保存配对、协议栈内部状态或相关系统数据。
 *   因此 BLE 模块启动前，必须先把 NVS 处于可用状态。
 *
 * 设计说明：
 *   这里采用 ESP-IDF 官方示例里非常常见的一段“容错初始化”逻辑：
 *   - 先调用 nvs_flash_init()
 *   - 如果返回“页空间不足”或“NVS 版本不匹配”
 *   - 就执行 nvs_flash_erase() 擦除整块 NVS
 *   - 然后再次初始化
 *
 *   这样写的原因是：
 *   当你刷写过不同版本固件、修改过分区表、或者 NVS 区损坏时，
 *   单纯调用 nvs_flash_init() 可能失败。擦除并重建是最稳妥的恢复方式。
 */
static esp_err_t app_init_nvs(void)
{
    /*
     * nvs_flash_init():
     *   初始化默认 NVS 分区，使后续组件可以读写键值对数据。
     *
     * 返回值是 esp_err_t：
     *   这是 ESP-IDF 通用错误码类型。
     */
    esp_err_t ret = nvs_flash_init();

    /*
     * 下面这两个错误是 NVS 初始化时最常见、也最适合“自动恢复”的情况：
     *
     * ESP_ERR_NVS_NO_FREE_PAGES：
     *   通常表示 NVS 分区结构损坏或可用页不足。
     *
     * ESP_ERR_NVS_NEW_VERSION_FOUND：
     *   通常表示当前 Flash 中的 NVS 数据版本与本固件期望版本不兼容。
     */
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        /*
         * ESP_ERROR_CHECK(x) 是 ESP-IDF 中非常常用的“错误即中止”宏：
         * - 如果 x 返回 ESP_OK，则什么都不发生
         * - 如果 x 返回非 ESP_OK，则会打印错误信息并触发 abort
         *
         * 这里用它包住 nvs_flash_erase()，
         * 表示“如果连擦除都失败，就没必要继续启动系统了”。
         */
        ESP_ERROR_CHECK(nvs_flash_erase());

        /* 擦除后重新初始化，尝试把 NVS 恢复到干净可用状态。 */
        ret = nvs_flash_init();
    }

    /* 这里不直接 ESP_ERROR_CHECK(ret)，而是把结果返回给上层统一处理。 */
    return ret;
}

/*
 * 函数作用：
 *   ESP-IDF 应用程序入口，负责整个系统的初始化顺序控制。
 *
 * 调用时机：
 *   由 ESP-IDF 启动框架在系统底层启动完成后自动调用，用户不手动调用。
 *
 * 参数含义：
 *   无。
 *
 * 返回值含义：
 *   无。app_main() 在 ESP-IDF 中定义为 void。
 *
 * 是否修改全局状态：
 *   会触发多个模块初始化，并创建后台 FreeRTOS 任务。
 *
 * 与其他模块/任务/回调的关系：
 *   - 调用 adc_task_init() 后，会创建 ADC 采样任务
 *   - 调用 ble_service_init() 后，会注册 BLE 回调并创建 Notify 任务
 *   - 调用 ws2812_status_init() 后，状态灯模块可被其他模块使用
 *
 * 设计说明：
 *   这里特意把系统启动过程写成线性顺序，便于初学者理解“依赖关系”：
 *   状态灯 -> NVS -> ADC -> BLE。
 *   其中 BLE 放在 NVS 后面，是因为 BLE 协议栈依赖 NVS。
 */
void app_main(void)
{
    /*
     * ESP_LOGI(TAG, ...)：
     *   以“信息级别”输出日志。
     *
     * 常见日志级别包括：
     *   - ESP_LOGE：错误
     *   - ESP_LOGW：警告
     *   - ESP_LOGI：信息
     *   - ESP_LOGD：调试
     *   - ESP_LOGV：更详细调试
     *
     * 在当前项目里，启动日志有两个作用：
     * 1. 让串口输出能看见固件是否已经跑起来
     * 2. 记录当前关键配置，方便联调
     */
    ESP_LOGI(TAG, "ESP32-C3 ADC + BLE Notify firmware boot");
    ESP_LOGI(TAG, "ADC period: %d ms, BLE notify period: %d ms",
             APP_ADC_SAMPLE_PERIOD_MS, APP_BLE_NOTIFY_PERIOD_MS);

    /*
     * 状态灯优先初始化。
     *
     * 工程意义：
     *   即使后续 BLE 或 ADC 初始化失败，状态灯模块也仍可能帮助我们
     *   用不同颜色表达系统状态。对嵌入式开发来说，这是一种非常实用的
     *   “最小可视化调试手段”。
     */
    if (ws2812_status_init() != ESP_OK) {
        /* 如果状态灯初始化失败，只打印日志，不中止系统。 */
        ESP_LOGE(TAG, "WS2812 init failed");
    } else {
        /* 初始化成功后，先把灯设置为“空闲/正常待机”状态。 */
        ws2812_status_set(WS2812_STATUS_IDLE);
    }

    /*
     * 先初始化 NVS，再初始化依赖 NVS 的 BLE。
     *
     * 这里继续使用 ESP_ERROR_CHECK：
     *   表示 NVS 是“系统启动必要条件”，失败就不再继续执行后续逻辑。
     */
    ESP_ERROR_CHECK(app_init_nvs());

    /*
     * 启动 ADC 采样模块。
     * adc_task_init() 内部会完成驱动初始化并创建 FreeRTOS 采样任务。
     */
    ESP_ERROR_CHECK(adc_task_init());

    /*
     * 启动 BLE 服务模块。
     * 该函数内部会：
     * 1. 初始化 BLE 控制器与协议栈
     * 2. 注册 GAP / GATTS 回调
     * 3. 创建 Notify 发送任务
     */
    ESP_ERROR_CHECK(ble_service_init());

    ESP_LOGI(TAG, "System init complete");

    /*
     * 当前主任务不再承担业务逻辑，只做保活。
     *
     * 之所以不让 app_main() 直接退出，是因为：
     *   在很多 ESP-IDF 工程里，主任务退出并不是你想要的行为；
     *   更稳妥的方式是让它保持低负载运行。
     *
     * vTaskDelay(pdMS_TO_TICKS(1000)) 的含义：
     *   - pdMS_TO_TICKS(1000)：把 1000 毫秒转换成 FreeRTOS 的 tick 数
     *   - vTaskDelay(...)：让当前任务进入阻塞态一段时间，把 CPU 让给其他任务
     *
     * 这样写的工程意义：
     *   - 主任务不会空转占满 CPU
     *   - ADC 采样任务和 BLE 发送任务可以按各自节奏运行
     */
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
