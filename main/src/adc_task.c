/*
 * adc_task.c
 * --------------------------------------------------------------------------
 * 这是 ADC 采样模块的实现文件。
 *
 * 模块核心职责：
 * 1. 初始化 ADC oneshot 驱动
 * 2. 尝试为各通道初始化 ADC 校准
 * 3. 通过一个后台 FreeRTOS 任务周期采样 4 路数据
 * 4. 把原始 raw 值转换成 mV
 * 5. 维护一份“最近一次完整采样快照”供 BLE 模块读取
 *
 * 整体架构可以理解成：
 *
 *   ADC 硬件 -> ESP-IDF ADC oneshot 驱动 -> 本模块采样任务
 *           -> 最近采样快照 s_latest_sample -> BLE 模块读取并打包
 *
 * 这种结构的优点是：
 * - 采样与发送解耦
 * - BLE 任务不需要关心 ADC 驱动细节
 * - 数据共享边界非常清楚：只共享一份快照
 */

#include <math.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/portmacro.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "adc_task.h"

/*
 * 模块日志标签。
 * 通过这个 TAG，串口日志中可以快速定位消息来自 ADC_TASK 模块。
 */
static const char *TAG = "ADC_TASK";

/*
 * 这个结构体把“逻辑通道顺序”和“物理硬件信息”绑定在一起。
 *
 * 设计意图：
 * 初学者常见的错误是把 GPIO 号、ADC Channel 号、协议顺序混在一起。
 * 这里显式做一个描述表，可以把三个概念分开：
 * - channel：给驱动用的 ADC 通道号
 * - gpio_num：给日志和板级理解用的 GPIO 号
 * - name：给日志和调试看的可读名称
 */
typedef struct {
    /*
     * ESP-IDF ADC 驱动使用的通道号。
     * 这是 adc_oneshot_read() 真正会用到的硬件抽象标识。
     */
    adc_channel_t channel;

    /*
     * 板上实际接线的 GPIO 号。
     * 主要用于打印日志，让人能把代码与原理图对应起来。
     */
    int gpio_num;

    /*
     * 该通道的人类可读名称，用于日志输出。
     * 使用 const char * 指向只读字符串常量，不需要动态分配。
     */
    const char *name;
} adc_channel_desc_t;

/*
 * 通道描述表。
 *
 * 这个数组的下标必须和 adc_sample_index_t 枚举一一对应。
 * 因此这里用指定下标初始化（designated initializer），而不是单纯依赖顺序。
 *
 * 这样写的好处：
 * - 就算将来调整书写顺序，也不容易把逻辑索引写错
 * - 初学者能更直观看到“枚举值 -> 物理通道”的映射关系
 */
static const adc_channel_desc_t s_channels[APP_ADC_CHANNEL_COUNT] = {
    [ADC_SAMPLE_INDEX_VTEM]  = { .channel = ADC_CHANNEL_0, .gpio_num = APP_ADC_GPIO_VTEM,  .name = "VTEM"  },
    [ADC_SAMPLE_INDEX_VM]    = { .channel = ADC_CHANNEL_1, .gpio_num = APP_ADC_GPIO_VM,    .name = "VM"    },
    [ADC_SAMPLE_INDEX_VA201] = { .channel = ADC_CHANNEL_3, .gpio_num = APP_ADC_GPIO_VA201, .name = "VA201" },
    [ADC_SAMPLE_INDEX_VBAT]  = { .channel = ADC_CHANNEL_4, .gpio_num = APP_ADC_GPIO_VBAT,  .name = "VBAT"  },
};

/*
 * ADC oneshot 驱动句柄。
 *
 * 生命周期：
 * - 在 adc_task_init() 中创建
 * - 之后在整个系统运行期内持续可用
 *
 * 作用域：
 * - static 说明只在当前源文件中可见
 * - 其他模块不能直接访问它，只能通过对外接口间接使用 ADC 数据
 */
static adc_oneshot_unit_handle_t s_adc_handle;

/*
 * 每个逻辑通道各自持有一个 ADC 校准句柄。
 *
 * 为什么不是只有一个句柄？
 * 因为不同通道可能对应不同校准配置或支持情况，
 * 单独保存能让换算逻辑按通道独立处理。
 */
static adc_cali_handle_t s_cali_handles[APP_ADC_CHANNEL_COUNT];

/*
 * 记录每个通道是否成功启用了校准。
 *
 * 这样后续换算时就不必盲目调用 adc_cali_raw_to_voltage()，
 * 可以先看当前通道到底有没有可用校准资源。
 */
static bool s_cali_enabled[APP_ADC_CHANNEL_COUNT];

/*
 * “最近一次完整采样”的共享快照。
 *
 * 生命周期：整个系统运行期内一直存在。
 * 生产者：adc_task_sampling_loop() 任务
 * 消费者：ble_service_build_frame() 等读取者
 *
 * 这里保存的是“最新值”，不是历史队列。
 * 这是一种非常轻量的实时系统设计：
 * - 不保留长历史
 * - 只保证消费者随时能拿到最近一份完整样本
 */
static adc_latest_sample_t s_latest_sample;

/*
 * 保护 s_latest_sample 的临界区锁。
 *
 * 为什么这里需要锁：
 * - ADC 采样任务会写 s_latest_sample
 * - BLE 发送任务会读 s_latest_sample
 * - 两者可能在不同 CPU 时间片下交错执行
 *
 * 如果没有保护，可能发生“读到一半，另一边正在写”的情况，
 * 导致一帧里混入两次采样结果。
 *
 * 这里使用 portMUX_TYPE + portENTER_CRITICAL() / portEXIT_CRITICAL()，
 * 是因为共享数据很小，临界区很短，成本低、实现直观。
 */
static portMUX_TYPE s_sample_lock = portMUX_INITIALIZER_UNLOCKED;

/*
 * 函数作用：
 *   在 ADC 校准不可用时，用线性公式把 raw 值近似换算为毫伏值。
 *
 * 调用时机：
 *   仅在 adc_task_sampling_loop() 中，当 adc_task_raw_to_mv() 返回失败时调用。
 *
 * 参数含义：
 *   raw：ADC 原始读数。当前默认假定其量程近似为 0~4095。
 *
 * 返回值含义：
 *   返回近似换算得到的毫伏值。
 *
 * 是否会修改全局状态：
 *   不会。
 *
 * 与其他模块/任务/回调的关系：
 *   它只是本模块内部的兜底工具函数，对外不可见。
 *
 * 设计说明：
 *   这不是严格精确的校准结果，只是一个“没有校准也能大致可用”的保底方案。
 *   工程上这样做的意义是：即使某些环境下校准不支持，
 *   整条采样 -> BLE -> 上位机链路仍然可以继续工作。
 */
static int adc_task_raw_to_mv_fallback(uint16_t raw)
{
    return (int)(((uint32_t)raw * 3300U) / 4095U);
}

/*
 * 函数作用：
 *   根据 PT1000 的温度求理论阻值。
 *
 * 设计说明：
 *   这主要给负温区的 Newton 迭代提供“正向模型”与导数。
 */
static float adc_task_pt1000_resistance_from_temperature_c(float temperature_c)
{
    if (temperature_c >= 0.0f) {
        return APP_PT1000_R0_OHM *
               (1.0f + APP_PT1000_COEFF_A * temperature_c +
                APP_PT1000_COEFF_B * temperature_c * temperature_c);
    }

    float temp2 = temperature_c * temperature_c;
    float temp3 = temp2 * temperature_c;
    return APP_PT1000_R0_OHM *
           (1.0f + APP_PT1000_COEFF_A * temperature_c +
            APP_PT1000_COEFF_B * temp2 +
            APP_PT1000_COEFF_C * (temperature_c - 100.0f) * temp3);
}

/*
 * 函数作用：
 *   求 PT1000 负温区 Callendar-Van Dusen 方程对温度的一阶导数。
 */
static float adc_task_pt1000_resistance_derivative_below_zero(float temperature_c)
{
    float temp2 = temperature_c * temperature_c;
    float temp3 = temp2 * temperature_c;
    return APP_PT1000_R0_OHM *
           (APP_PT1000_COEFF_A +
            2.0f * APP_PT1000_COEFF_B * temperature_c +
            APP_PT1000_COEFF_C * (4.0f * temp3 - 300.0f * temp2));
}

/*
 * 函数作用：
 *   尝试为指定 ADC 通道初始化校准句柄。
 *
 * 调用时机：
 *   在 adc_task_init() 中，对每个通道初始化一次。
 *
 * 参数含义：
 *   channel：目标 ADC 通道号（驱动层通道号）
 *   out_handle：输出参数，成功时返回校准句柄，失败时置为 NULL
 *
 * 返回值含义：
 *   true：成功创建校准句柄
 *   false：当前环境下无法创建校准句柄
 *
 * 是否会修改全局状态：
 *   不直接修改模块全局变量，但会通过 out_handle 把创建结果返回给调用者。
 *
 * 与其他模块/任务/回调的关系：
 *   由 adc_task_init() 调用，其返回结果会写入 s_cali_handles[] 和 s_cali_enabled[]。
 *
 * 设计说明：
 *   ESP-IDF 的 ADC 校准方案可能因芯片、IDF 配置、编译开关而不同。
 *   因此这里不是假设“必然有某种校准”，而是按“有则用之，无则退化”的思路写。
 */
static bool adc_task_try_init_cali(adc_channel_t channel, adc_cali_handle_t *out_handle)
{
    esp_err_t ret = ESP_FAIL;
    adc_cali_handle_t handle = NULL;
    bool calibrated = false;

#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    /*
     * 如果当前目标平台/配置支持 Curve Fitting（曲线拟合）校准，
     * 就优先尝试这种方案。
     *
     * adc_cali_curve_fitting_config_t 各字段含义：
     * - unit_id  : ADC 单元编号。ESP32-C3 当前只用 ADC_UNIT_1
     * - chan     : 目标通道
     * - atten    : 衰减配置，必须与实际采样配置一致
     * - bitwidth : ADC 位宽配置，当前用默认位宽
     */
    adc_cali_curve_fitting_config_t cali_config = {
        .unit_id = ADC_UNIT_1,
        .chan = channel,
        .atten = APP_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };

    /*
     * adc_cali_create_scheme_curve_fitting():
     *   根据配置创建一个“raw -> 电压”的校准对象。
     *
     * 成功时，handle 会变成可用句柄；
     * 失败时，不会中止系统，而是继续尝试其他方案。
     */
    ret = adc_cali_create_scheme_curve_fitting(&cali_config, &handle);
    if (ret == ESP_OK) {
        calibrated = true;
    }
#endif

#if ADC_CALI_SCHEME_LINE_FITTING_SUPPORTED
    /*
     * 如果曲线拟合没成功，再尝试 Line Fitting（线性拟合）方案。
     *
     * 之所以要加 if (!calibrated)，是为了保证前一种成功后就不重复创建。
     */
    if (!calibrated) {
        adc_cali_line_fitting_config_t cali_config = {
            .unit_id = ADC_UNIT_1,
            .atten = APP_ADC_ATTEN,
            .bitwidth = ADC_BITWIDTH_DEFAULT,
        };

        /*
         * adc_cali_create_scheme_line_fitting():
         *   创建线性校准对象。
         *
         * 注意这个结构体没有单独的 chan 字段，
         * 它的使用方式由 ESP-IDF 当前校准方案定义。
         */
        ret = adc_cali_create_scheme_line_fitting(&cali_config, &handle);
        if (ret == ESP_OK) {
            calibrated = true;
        }
    }
#endif

    /*
     * 如果任一方案成功，就把句柄返回给调用者。
     * 如果都失败，则明确返回 NULL，避免调用者拿到野指针。
     */
    if (calibrated) {
        *out_handle = handle;
        return true;
    }

    *out_handle = NULL;
    return false;
}

/*
 * 函数作用：
 *   这是 ADC 模块的后台采样任务函数。
 *
 * 调用时机：
 *   由 xTaskCreate() 在 adc_task_init() 中创建后，随系统运行一直循环执行。
 *
 * 参数含义：
 *   arg：FreeRTOS 任务入口参数。当前项目不需要额外参数，因此未使用。
 *
 * 返回值含义：
 *   无。FreeRTOS 任务函数通常设计为不返回，而是在 while(1) 中持续运行。
 *
 * 是否会修改全局状态：
 *   会周期性更新 s_latest_sample。
 *
 * 与其他模块/任务/回调的关系：
 *   - 生产最新 ADC 快照
 *   - BLE 任务随后会读取这份快照并打包发送
 *
 * 设计说明：
 *   这里采用“先在局部变量里完成一整份采样，再一次性写入共享快照”的方式，
 *   这样比边采边写共享变量更安全，也更容易保证数据一致性。
 */
static void adc_task_sampling_loop(void *arg)
{
    /* 当前任务入口没有用到参数，所以显式消除未使用告警。 */
    (void)arg;

    /*
     * local_sample 是本次循环的局部采样缓存。
     * 生命周期只在当前任务栈帧内，但因为任务函数不会退出，
     * 它会在每轮循环中反复被复用。
     */
    adc_latest_sample_t local_sample = {0};

    while (1) {
        /*
         * 依次读取 4 路通道。
         * 这里以 APP_ADC_CHANNEL_COUNT 为上限，保证逻辑上与协议定义一致。
         */
        for (size_t i = 0; i < APP_ADC_CHANNEL_COUNT; ++i) {
            int raw = 0;

            /*
             * adc_oneshot_read():
             *   读取指定 ADC 通道的一次原始采样结果。
             *
             * 参数说明：
             * - s_adc_handle          : 之前创建好的 ADC oneshot 句柄
             * - s_channels[i].channel : 本次要读取的物理 ADC 通道
             * - &raw                  : 输出参数，返回 raw 原始值
             *
             * 返回语义：
             * - ESP_OK：读取成功
             * - 非 ESP_OK：读取失败
             */
            esp_err_t ret = adc_oneshot_read(s_adc_handle, s_channels[i].channel, &raw);
            if (ret != ESP_OK) {
                /*
                 * 读取失败时，不让整个任务崩掉，而是：
                 * 1. 记录警告日志
                 * 2. 把本通道值置 0
                 *
                 * 这样系统的实时性和可恢复性更好。
                 */
                ESP_LOGW(TAG, "Read %s failed: %s", s_channels[i].name, esp_err_to_name(ret));
                raw = 0;
            }

            int voltage_mv = 0;

            /*
             * 尝试使用校准接口把 raw 转成 mV。
             * 当前通道如果有可用校准句柄，这一步能得到更可信的电压值。
             */
            ret = adc_task_raw_to_mv((adc_sample_index_t)i, (uint16_t)raw, &voltage_mv);
            if (ret != ESP_OK) {
                /*
                 * 如果校准不可用或换算失败，则退化到近似公式。
                 *
                 * 注意：
                 * 这里不是“修 bug”，而是设计上的兜底路径。
                 * 目标是在尽量不丢功能的前提下保证项目可运行。
                 */
                voltage_mv = adc_task_raw_to_mv_fallback((uint16_t)raw);
            }

            /*
             * 把本通道结果写入本轮采样缓存。
             * 这里仍然按逻辑索引顺序存储，保证与 BLE 协议字段顺序一致。
             */
            local_sample.voltage_mv[i] = (uint16_t)voltage_mv;
        }

        /*
         * esp_timer_get_time():
         *   返回自系统启动以来经过的时间，单位是微秒（us）。
         *
         * 这里除以 1000，把它变成毫秒时间戳，足以满足当前上位机绘图需求。
         */
        local_sample.timestamp_ms = (uint32_t)(esp_timer_get_time() / 1000ULL);

        /*
         * 进入临界区，保护共享快照写入。
         *
         * 为什么要整个结构体一次性赋值：
         * 因为这样可以在非常短的临界区内完成“整帧替换”，
         * 让读取者看到的始终是一份完整采样，而不是半新半旧的数据。
         */
        portENTER_CRITICAL(&s_sample_lock);
        s_latest_sample = local_sample;
        portEXIT_CRITICAL(&s_sample_lock);

        /*
         * vTaskDelay(pdMS_TO_TICKS(...))：
         * - pdMS_TO_TICKS() 把毫秒转换成 FreeRTOS 调度 tick
         * - vTaskDelay() 让当前任务阻塞指定 tick 数
         *
         * 这样做的本质是“周期任务节流”，避免 while(1) 空转占满 CPU。
         */
        vTaskDelay(pdMS_TO_TICKS(APP_ADC_SAMPLE_PERIOD_MS));
    }
}

/*
 * 函数作用：
 *   初始化 ADC oneshot 驱动、配置各采样通道，并创建后台采样任务。
 *
 * 调用时机：
 *   在系统启动阶段由 app_main() 调用一次。
 *
 * 参数含义：
 *   无。
 *
 * 返回值含义：
 *   - ESP_OK：初始化和任务创建均成功
 *   - ESP_FAIL：任务创建失败
 *   - 其他错误：若驱动创建/配置失败，会被 ESP_ERROR_CHECK 直接中止程序
 *
 * 是否会修改全局状态：
 *   会初始化 s_adc_handle、s_cali_handles[]、s_cali_enabled[]、s_latest_sample，
 *   并创建长期运行的采样任务。
 *
 * 与其他模块/任务/回调的关系：
 *   该函数成功返回后，BLE 模块才能稳定读取最新采样快照。
 */
esp_err_t adc_task_init(void)
{
    /*
     * ESP32-C3 只有 ADC1 可用，所以这里明确选择 ADC_UNIT_1。
     *
     * adc_oneshot_unit_init_cfg_t 中当前只配置 unit_id，
     * 表示创建一个 ADC1 的 oneshot 采样单元。
     */
    adc_oneshot_unit_init_cfg_t init_cfg = {
        .unit_id = ADC_UNIT_1,
    };

    /*
     * adc_oneshot_new_unit():
     *   创建一个 ADC oneshot 驱动实例，并返回句柄。
     *
     * 这里用 ESP_ERROR_CHECK 包裹，表示：
     * “如果 ADC 驱动都建不起来，就没必要继续启动系统”。
     */
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&init_cfg, &s_adc_handle));

    /*
     * 每个通道共享同一套采样配置：
     * - atten    : 使用项目统一定义的衰减
     * - bitwidth : 使用 ESP-IDF 默认位宽
     */
    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = APP_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };

    /*
     * 逐通道完成两件事：
     * 1. 配置 oneshot 采样参数
     * 2. 尝试初始化校准
     */
    for (size_t i = 0; i < APP_ADC_CHANNEL_COUNT; ++i) {
        /*
         * adc_oneshot_config_channel():
         *   把指定通道配置成可被 oneshot 驱动读取的状态。
         */
        ESP_ERROR_CHECK(adc_oneshot_config_channel(s_adc_handle, s_channels[i].channel, &chan_cfg));

        /* 尝试给当前通道创建校准句柄。 */
        s_cali_enabled[i] = adc_task_try_init_cali(s_channels[i].channel, &s_cali_handles[i]);

        /*
         * ESP_LOGI 在这里非常有用：
         * 它能清楚告诉你板上每路 ADC 是否已经配置好、校准是否可用。
         */
        ESP_LOGI(TAG, "Channel %s on GPIO%d configured, calibration: %s",
                 s_channels[i].name,
                 s_channels[i].gpio_num,
                 s_cali_enabled[i] ? "enabled" : "disabled");
    }

    /*
     * 把共享快照清零，避免 BLE 模块在系统刚启动时读到未初始化的脏数据。
     */
    memset(&s_latest_sample, 0, sizeof(s_latest_sample));

    /*
     * xTaskCreate():
     *   创建一个 FreeRTOS 任务。
     *
     * 参数含义依次为：
     * - adc_task_sampling_loop  : 任务入口函数
     * - "adc_task"             : 任务名字，便于调试
     * - APP_ADC_TASK_STACK_SIZE : 任务栈大小
     * - NULL                   : 传给任务的参数，当前不需要
     * - APP_ADC_TASK_PRIORITY   : 任务优先级
     * - NULL                   : 不关心返回的任务句柄
     *
     * 返回值类型是 BaseType_t：
     * - pdPASS：创建成功
     * - 其他值：创建失败
     */
    BaseType_t task_ok = xTaskCreate(
        adc_task_sampling_loop,
        "adc_task",
        APP_ADC_TASK_STACK_SIZE,
        NULL,
        APP_ADC_TASK_PRIORITY,
        NULL
    );

    return task_ok == pdPASS ? ESP_OK : ESP_FAIL;
}

/*
 * 函数作用：
 *   读取最近一次完整采样快照。
 *
 * 调用时机：
 *   当前主要由 BLE 模块在打包协议帧时调用。
 *
 * 参数含义：
 *   out_sample：调用者提供的输出缓冲区，用于接收复制出的快照。
 *
 * 返回值含义：
 *   - ESP_OK：读取成功
 *   - ESP_ERR_INVALID_ARG：out_sample 为 NULL
 *
 * 是否会修改全局状态：
 *   不修改共享快照，只读取并复制它。
 *
 * 与其他模块/任务/回调的关系：
 *   该函数是 ADC 模块对外暴露“最新采样值”的主要接口。
 */
esp_err_t adc_task_get_latest(adc_latest_sample_t *out_sample)
{
    /* 参数检查始终放在最前面，防止空指针解引用。 */
    if (out_sample == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    /*
     * 进入临界区保护读取过程。
     *
     * 原因：
     * 如果 ADC 任务正在写 s_latest_sample，而这里同时在读，
     * 就可能复制到一半新、一半旧的数据。
     */
    portENTER_CRITICAL(&s_sample_lock);
    *out_sample = s_latest_sample;
    portEXIT_CRITICAL(&s_sample_lock);
    return ESP_OK;
}

esp_err_t adc_task_mv_to_pt1000_resistance_ohm(uint16_t voltage_mv, float *out_ohm)
{
    if (out_ohm == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    if (voltage_mv >= (uint16_t)APP_PT1000_DIVIDER_SUPPLY_MV) {
        return ESP_ERR_INVALID_ARG;
    }

    float voltage = (float)voltage_mv;
    float denominator = APP_PT1000_DIVIDER_SUPPLY_MV - voltage;
    if (denominator <= 0.0f) {
        return ESP_ERR_INVALID_ARG;
    }

    *out_ohm = APP_PT1000_DIVIDER_SERIES_OHM * voltage / denominator;
    return ESP_OK;
}

esp_err_t adc_task_mv_to_pt1000_temperature_c(uint16_t voltage_mv, float *out_temp_c)
{
    if (out_temp_c == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    float resistance_ohm = 0.0f;
    esp_err_t ret = adc_task_mv_to_pt1000_resistance_ohm(voltage_mv, &resistance_ohm);
    if (ret != ESP_OK) {
        return ret;
    }

    if (resistance_ohm >= APP_PT1000_R0_OHM) {
        float discriminant = APP_PT1000_COEFF_A * APP_PT1000_COEFF_A -
                             4.0f * APP_PT1000_COEFF_B *
                                 (1.0f - resistance_ohm / APP_PT1000_R0_OHM);
        if (discriminant < 0.0f) {
            return ESP_FAIL;
        }

        *out_temp_c = (-APP_PT1000_COEFF_A + sqrtf(discriminant)) /
                      (2.0f * APP_PT1000_COEFF_B);
        return ESP_OK;
    }

    /*
     * 负温区需要处理 C 项，不能直接用二次方程反解。
     * 这里用 Newton 迭代，初值采用线性近似即可稳定收敛。
     */
    float temperature_c =
        (resistance_ohm - APP_PT1000_R0_OHM) / (APP_PT1000_R0_OHM * APP_PT1000_COEFF_A);

    for (int i = 0; i < 12; ++i) {
        float error = adc_task_pt1000_resistance_from_temperature_c(temperature_c) - resistance_ohm;
        float derivative = adc_task_pt1000_resistance_derivative_below_zero(temperature_c);
        if (fabsf(derivative) < 1e-6f) {
            return ESP_FAIL;
        }

        temperature_c -= error / derivative;
    }

    *out_temp_c = temperature_c;
    return ESP_OK;
}

/*
 * 函数作用：
 *   使用 ESP-IDF ADC 校准接口，把 raw 原始值换算成毫伏值。
 *
 * 调用时机：
 *   当前由采样任务在每次读取完 raw 值后调用。
 *
 * 参数含义：
 *   channel_index：逻辑通道索引，用于找到对应的校准句柄
 *   raw：ADC 原始采样值
 *   out_mv：输出参数，成功时写入毫伏值
 *
 * 返回值含义：
 *   - ESP_OK：换算成功
 *   - ESP_ERR_INVALID_ARG：参数非法
 *   - ESP_ERR_NOT_SUPPORTED：当前通道没有可用校准句柄
 *   - 其他错误：由底层 adc_cali_raw_to_voltage() 返回
 *
 * 是否会修改全局状态：
 *   不修改模块全局状态，只读取已有校准资源。
 *
 * 与其他模块/任务/回调的关系：
 *   这是本模块的校准换算入口，fallback 逻辑则由调用者决定是否启用。
 */
esp_err_t adc_task_raw_to_mv(adc_sample_index_t channel_index, uint16_t raw, int *out_mv)
{
    /*
     * 参数合法性检查：
     * - 输出指针不能为空
     * - 通道索引必须在数组范围内
     */
    if (out_mv == NULL || channel_index >= APP_ADC_CHANNEL_COUNT) {
        return ESP_ERR_INVALID_ARG;
    }

    /*
     * 如果该通道没有成功初始化校准资源，就明确返回“不支持”。
     * 调用者可以据此选择 fallback 策略。
     */
    if (!s_cali_enabled[channel_index] || s_cali_handles[channel_index] == NULL) {
        return ESP_ERR_NOT_SUPPORTED;
    }

    /*
     * adc_cali_raw_to_voltage():
     *   使用校准句柄，把 raw 值换算成更接近真实电压的毫伏值。
     *
     * 参数：
     * - s_cali_handles[channel_index] : 当前通道的校准对象
     * - raw                           : ADC 原始值
     * - out_mv                        : 输出毫伏值
     */
    return adc_cali_raw_to_voltage(s_cali_handles[channel_index], raw, out_mv);
}
