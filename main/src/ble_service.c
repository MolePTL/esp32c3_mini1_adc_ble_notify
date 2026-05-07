/*
 * ble_service.c
 * --------------------------------------------------------------------------
 * 这是 BLE 服务模块的实现文件。
 *
 * 这个模块在整个工程中的位置非常关键：
 * - 它是 ADC 数据通往上位机的“无线出口”
 * - 它负责搭建自定义 GATT 服务
 * - 它负责广播、建链、通知开关、周期发送
 * - 它还负责把系统状态同步到 WS2812 状态灯
 *
 * 从框架关系上看，这个文件同时接触了 BLE 的两层概念：
 *
 * 1. GAP（Generic Access Profile）层
 *    负责“设备如何被发现、如何广播、何时开始/停止广播”等问题。
 *
 * 2. GATT Server（Generic Attribute Profile Server）层
 *    负责“设备内部有哪些服务、特征值、客户端读写哪些属性、如何发送 Notify”等问题。
 *
 * 对初学者来说，可以把它理解成：
 * - GAP 更像“门口招牌和招呼客人”
 * - GATT 更像“屋子里的服务目录和数据接口”
 *
 * 本项目采用最小可用设计：
 * - 1 个自定义 Service
 * - 1 个可读 + 可 Notify 的 Characteristic
 * - 1 个 CCCD（客户端配置描述符）
 *
 * 这样已经足够支持电脑端：
 *   扫描 -> 连接 -> 订阅 Notify -> 连续收数
 */

#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/portmacro.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_err.h"
#include "esp_gap_ble_api.h"
#include "esp_gatt_common_api.h"
#include "esp_gatts_api.h"
#include "esp_log.h"
#include "adc_task.h"
#include "ble_service.h"
#include "ws2812_status.h"
#include "app_config.h"

/* 模块日志标签，用于区分串口输出来源。 */
static const char *TAG = "BLE_SERVICE";

/*
 * ==================== BLE Profile / 实例常量 ====================
 * 这一组宏用于描述当前 GATT Server 的最小组织方式。
 *
 * 当前项目只有一个 GATT Profile，因此这些值都保持最简配置。
 * 它们不是“协议字段”，而是 Bluedroid 框架内部注册服务时用到的标识。
 */

/* 当前只实现 1 个 Profile；首版不做多 Profile 设计。 */
#define BLE_PROFILE_NUM                     1

/* 当前 Profile 在内部数组/逻辑中的索引。 */
#define BLE_PROFILE_APP_IDX                 0

/* 当前 GATT 应用注册给协议栈的应用 ID。 */
#define BLE_APP_ID                          0x42

/* 当前 Service 实例号。单服务场景下固定为 0 即可。 */
#define BLE_SERVICE_INSTANCE_ID             0

/*
 * 广播数据配置完成标志位。
 *
 * 为什么要用位标志而不是简单 bool：
 * 在复杂 BLE 工程里，经常会同时跟踪“广播包配置完成”“扫描响应包配置完成”等多个异步事件。
 * 当前工程只有一个标志位，但仍沿用这种可扩展写法，便于理解 IDF 常见模式。
 */
#define ADV_CONFIG_FLAG                     (1 << 0)

/*
 * ==================== GATT 属性表索引 ====================
 * 这个匿名枚举不是给协议帧用的，而是给本地 GATT 数据库数组索引用的。
 *
 * 设计意图：
 * 通过具名索引而不是硬编码 0/1/2/3，
 * 让后续代码在引用 s_handle_table[] 时更清楚“当前句柄代表什么属性”。
 */
enum {
    /* 主服务声明（Primary Service Declaration）。 */
    BLE_IDX_SVC = 0,

    /* Characteristic 声明项，用于告诉客户端“下面有一个特征值”。 */
    BLE_IDX_CHAR_DECL,

    /* Characteristic 实际值，也就是我们真正发送 ADC 数据的地方。 */
    BLE_IDX_CHAR_VAL,

    /* Client Characteristic Configuration Descriptor，简称 CCCD。 */
    BLE_IDX_CHAR_CCCD,

    /* 属性总数。常用于数组长度或句柄数量校验。 */
    BLE_IDX_NB,
};

/*
 * Bluedroid 的 128-bit UUID 使用 16 字节数组表示。
 *
 * 这里直接复用 app_config.h 里的集中配置，
 * 这样 BLE 协议标识与全局配置保持一致，不会散落在多个文件里。
 */
static const uint8_t s_service_uuid128[ESP_UUID_LEN_128] = { APP_BLE_SERVICE_UUID_BYTES };
static const uint8_t s_char_uuid128[ESP_UUID_LEN_128] = { APP_BLE_CHARACTERISTIC_UUID_BYTES };

/*
 * GATT 框架通用 UUID。
 *
 * 这些不是我们自定义的业务 UUID，而是蓝牙规范定义好的标准 UUID：
 * - 主服务声明 UUID
 * - 特征声明 UUID
 * - 客户端配置描述符 UUID
 */
static const uint16_t s_primary_service_uuid = ESP_GATT_UUID_PRI_SERVICE;
static const uint16_t s_char_decl_uuid = ESP_GATT_UUID_CHAR_DECLARE;
static const uint16_t s_client_config_uuid = ESP_GATT_UUID_CHAR_CLIENT_CONFIG;

/*
 * Characteristic 属性位。
 *
 * 当前特征值需要支持：
 * - Read   : 让客户端即使不订阅，也能主动读一帧当前值
 * - Notify : 让客户端订阅后持续接收推送
 */
static const uint8_t s_char_property = ESP_GATT_CHAR_PROP_BIT_READ | ESP_GATT_CHAR_PROP_BIT_NOTIFY;

/*
 * Attribute backing store：属性后备存储区。
 *
 * 这两个静态数组是 GATT 数据库里某些属性的“实际数据缓冲区”：
 * 1. s_frame_value：Characteristic 当前值，客户端 Read 时读到的就是它
 * 2. s_cccd_value ：CCCD 当前值，记录客户端是否打开 Notify
 *
 * 注意：
 * 在 ESP_GATT_AUTO_RSP 模式下，协议栈会自动帮我们处理部分读写响应，
 * 但底层仍然需要一块实际数据缓冲区来承载属性值。
 */
static uint8_t s_frame_value[APP_FRAME_LEN_BYTES] = {0};
static uint8_t s_cccd_value[2] = {0x00, 0x00};

/*
 * ==================== 广播相关配置 ====================
 * 这一组静态变量描述广播数据内容和广播参数。
 *
 * 广播相关流程的关键特点：
 * - 配置广播数据是异步的
 * - 配置完成后才适合真正 start advertising
 * - 因此这里需要一个 s_adv_config_done 标志来跟踪状态
 */
static uint8_t s_adv_config_done = 0;

/*
 * 广播数据内容。
 *
 * esp_ble_adv_data_t 各字段在当前项目中的含义：
 * - set_scan_rsp       : false，表示这是主广播包，不是扫描响应包
 * - include_name       : true，把设备名放进广播中，便于人眼扫描识别
 * - include_txpower    : true，把发射功率放进广播中，便于调试
 * - min_interval/max_interval：这里属于连接参数建议值，不是广播周期本身
 * - appearance         : 设备外观类别，当前不特别声明
 * - manufacturer/service data：当前都不使用，保持空
 * - service_uuid_len/p_service_uuid：当前广播包里不直接携带 Service UUID
 * - flag               : 通用可发现 + 不支持经典蓝牙
 */
static esp_ble_adv_data_t s_adv_data = {
    .set_scan_rsp = false,
    .include_name = true,
    .include_txpower = true,
    .min_interval = 12,
    .max_interval = 24,
    .appearance = 0,
    .manufacturer_len = 0,
    .p_manufacturer_data = NULL,
    .service_data_len = 0,
    .p_service_data = NULL,
    .service_uuid_len = 0,
    .p_service_uuid = NULL,
    .flag = ESP_BLE_ADV_FLAG_GEN_DISC | ESP_BLE_ADV_FLAG_BREDR_NOT_SPT,
};

/*
 * 实际启动广播时使用的参数。
 *
 * esp_ble_adv_params_t 各字段在当前项目中的作用：
 * - adv_int_min/max    : 广播间隔范围，单位是 0.625ms
 * - adv_type           : ADV_TYPE_IND，可连接、可扫描的普通广播
 * - own_addr_type      : 使用本机公共地址
 * - channel_map        : 三个广播信道都启用
 * - adv_filter_policy  : 允许任意设备扫描和连接
 */
static esp_ble_adv_params_t s_adv_params = {
    .adv_int_min = 32,
    .adv_int_max = 64,
    .adv_type = ADV_TYPE_IND,
    .own_addr_type = BLE_ADDR_TYPE_PUBLIC,
    .channel_map = ADV_CHNL_ALL,
    .adv_filter_policy = ADV_FILTER_ALLOW_SCAN_ANY_CON_ANY,
};

/*
 * ==================== GATT 数据库 ====================
 * 这是当前 GATT Server 的静态属性表。
 *
 * esp_gatts_attr_db_t 本质上描述了“这个设备暴露给客户端的属性列表”。
 * 每个条目都包含两层信息：
 * 1. 控制信息（例如是否自动响应）
 * 2. 属性本身的信息（UUID、权限、长度、初值等）
 *
 * 当前项目的最小结构是：
 * - 一个主服务
 * - 一个 Characteristic 声明
 * - 一个 Characteristic 值
 * - 一个 CCCD
 */
static const esp_gatts_attr_db_t s_gatt_db[BLE_IDX_NB] = {
    [BLE_IDX_SVC] =
    {
        {ESP_GATT_AUTO_RSP},
        {
            ESP_UUID_LEN_16,
            (uint8_t *)&s_primary_service_uuid,
            ESP_GATT_PERM_READ,
            ESP_UUID_LEN_128,
            ESP_UUID_LEN_128,
            (uint8_t *)s_service_uuid128
        }
    },

    [BLE_IDX_CHAR_DECL] =
    {
        {ESP_GATT_AUTO_RSP},
        {
            ESP_UUID_LEN_16,
            (uint8_t *)&s_char_decl_uuid,
            ESP_GATT_PERM_READ,
            sizeof(uint8_t),
            sizeof(uint8_t),
            (uint8_t *)&s_char_property
        }
    },

    [BLE_IDX_CHAR_VAL] =
    {
        {ESP_GATT_AUTO_RSP},
        {
            ESP_UUID_LEN_128,
            (uint8_t *)s_char_uuid128,
            ESP_GATT_PERM_READ,
            APP_FRAME_LEN_BYTES,
            sizeof(s_frame_value),
            s_frame_value
        }
    },

    [BLE_IDX_CHAR_CCCD] =
    {
        {ESP_GATT_AUTO_RSP},
        {
            ESP_UUID_LEN_16,
            (uint8_t *)&s_client_config_uuid,
            ESP_GATT_PERM_READ | ESP_GATT_PERM_WRITE,
            sizeof(uint16_t),
            sizeof(s_cccd_value),
            s_cccd_value
        }
    },
};

/*
 * ==================== BLE 运行时状态 ====================
 * 这些静态变量共同构成当前 BLE 模块的最小运行时状态机。
 *
 * 它们的典型更新来源：
 * - 注册事件更新 s_gatts_if
 * - 建链/断链事件更新连接状态
 * - 写 CCCD 事件更新 notify 开关
 * - 定时发送任务读取这些状态决定是否发包
 */

/*
 * 属性表创建完成后，协议栈会返回每个属性的实际句柄。
 * 后续读写、发送 Notify 都需要使用这些句柄。
 */
static uint16_t s_handle_table[BLE_IDX_NB];

/* 当前 GATT 接口句柄，由注册成功事件给出。 */
static esp_gatt_if_t s_gatts_if = ESP_GATT_IF_NONE;

/* 当前连接 ID。未连接时用 0xFFFF 作为无效占位值。 */
static uint16_t s_conn_id = 0xFFFF;

/* 当前是否已经建立 BLE 连接。 */
static bool s_is_connected = false;

/* 当前客户端是否已经通过 CCCD 打开 Notify。 */
static bool s_notify_enabled = false;

/* 连续递增的帧序号，用于上位机观察丢帧或时序。 */
static uint16_t s_frame_seq = 0;

/*
 * 保护上述共享 BLE 状态的临界区锁。
 *
 * 为什么需要它：
 * - BLE 回调运行在协议栈事件上下文中
 * - Notify 发送任务运行在独立 FreeRTOS 任务中
 * - 两边都会读写连接状态和通知状态
 *
 * 如果不保护，就可能在发送任务读状态时，回调正好改状态，
 * 导致读到不一致的中间值。
 */
static portMUX_TYPE s_ble_lock = portMUX_INITIALIZER_UNLOCKED;

/*
 * 函数作用：
 *   把 16 位无符号整数按 little-endian 写入目标缓冲区。
 *
 * 调用时机：
 *   在协议帧打包函数中，用于写入 frame_id、各通道值、CRC 等字段。
 *
 * 参数含义：
 *   dst   ：目标缓冲区起始地址，至少应有 2 字节可写空间
 *   value ：要写入的 16 位值
 *
 * 返回值含义：
 *   无。
 *
 * 是否会修改全局状态：
 *   不会，只会修改调用者传入的缓冲区。
 *
 * 与其他模块/任务/回调的关系：
 *   这是 BLE 协议帧打包的内部工具函数。
 *
 * 设计说明：
 *   显式封装字节序有助于教学，也能避免到处手写位运算造成可读性下降。
 */
static void ble_write_u16_le(uint8_t *dst, uint16_t value)
{
    dst[0] = (uint8_t)(value & 0xFF);
    dst[1] = (uint8_t)((value >> 8) & 0xFF);
}

/*
 * 函数作用：
 *   把 32 位无符号整数按 little-endian 写入目标缓冲区。
 *
 * 调用时机：
 *   在协议帧打包函数中，用于写入时间戳字段。
 *
 * 参数含义：
 *   dst   ：目标缓冲区起始地址，至少应有 4 字节可写空间
 *   value ：要写入的 32 位值
 *
 * 返回值含义：
 *   无。
 *
 * 是否会修改全局状态：
 *   不会，只修改传入缓冲区内容。
 */
static void ble_write_u32_le(uint8_t *dst, uint32_t value)
{
    dst[0] = (uint8_t)(value & 0xFF);
    dst[1] = (uint8_t)((value >> 8) & 0xFF);
    dst[2] = (uint8_t)((value >> 16) & 0xFF);
    dst[3] = (uint8_t)((value >> 24) & 0xFF);
}

/*
 * 函数作用：
 *   统一启动 BLE 广播。
 *
 * 调用时机：
 *   - 广播数据配置完成后
 *   - 断开连接后需要重新进入可发现状态时
 *
 * 参数含义：
 *   无。
 *
 * 返回值含义：
 *   无。失败时通过日志和状态灯体现。
 *
 * 是否会修改全局状态：
 *   会触发控制器进入广播状态，并更新状态灯颜色。
 *
 * 与其他模块/任务/回调的关系：
 *   由 GAP 相关回调或断链逻辑调用。
 */
static void ble_service_start_advertising(void)
{
    /*
     * esp_ble_gap_start_advertising():
     *   根据给定广播参数启动 BLE 广播。
     *
     * 这是一个 GAP 层 API，负责让设备重新变成“可被扫描/可被连接”的状态。
     */
    esp_err_t ret = esp_ble_gap_start_advertising(&s_adv_params);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "start advertising failed: %s", esp_err_to_name(ret));
        ws2812_status_set(WS2812_STATUS_ERROR);
        return;
    }

    /* 成功发起广播后，把状态灯切到 advertising 语义颜色。 */
    ws2812_status_set(WS2812_STATUS_ADVERTISING);
}

/*
 * 函数作用：
 *   依据最近一次 ADC 快照拼出一帧固定长度的二进制协议数据。
 *
 * 调用时机：
 *   由 BLE Notify 发送任务在每次准备发包前调用。
 *
 * 参数含义：
 *   frame：调用者提供的 18 字节缓冲区，函数会按协议布局写满它。
 *
 * 返回值含义：
 *   无。
 *
 * 是否会修改全局状态：
 *   会递增全局帧序号 s_frame_seq；不会修改 ADC 模块状态。
 *
 * 与其他模块/任务/回调的关系：
 *   - 通过 adc_task_get_latest() 读取 ADC 模块快照
 *   - 打包结果随后会被写入 GATT 特征值并通过 Notify 发出
 *
 * 设计说明：
 *   这里采用固定偏移写字段的方式，能让协议布局一目了然。
 *   对初学者来说，理解“每个字节偏移对应哪个字段”非常重要。
 */
static void ble_service_build_frame(uint8_t frame[APP_FRAME_LEN_BYTES])
{
    /* 从 ADC 模块拿到一份最近采样快照。 */
    adc_latest_sample_t sample = {0};
    adc_task_get_latest(&sample);

    /*
     * 按协议定义写帧头和基本信息：
     * Byte 0  : 帧头 0xAA
     * Byte 1  : 帧头 0x55
     * Byte 2  : 协议版本
     * Byte 3  : 通道数
     */
    frame[0] = APP_FRAME_HEAD0;
    frame[1] = APP_FRAME_HEAD1;
    frame[2] = APP_BLE_FRAME_VERSION;
    frame[3] = APP_ADC_CHANNEL_COUNT;

    /*
     * 帧序号是共享状态，需要在临界区中递增。
     *
     * 为什么这里也要加锁：
     * 虽然当前只有 Notify 任务会主动发包，
     * 但把帧序号视为“模块共享状态”来保护更稳妥，也更利于后续扩展。
     */
    portENTER_CRITICAL(&s_ble_lock);
    s_frame_seq++;
    ble_write_u16_le(&frame[4], s_frame_seq);
    portEXIT_CRITICAL(&s_ble_lock);

    /*
     * 按 little-endian 顺序写入剩余字段：
     * Byte  6~ 9：时间戳 ms
     * Byte 10~11：VTEM 电压 mV
     * Byte 12~13：VA201 电压 mV
     * Byte 14~15：VBAT 电压 mV
     * Byte 16~17：CRC16（当前固定为 0）
     */
    ble_write_u32_le(&frame[6], sample.timestamp_ms);
    ble_write_u16_le(&frame[10], sample.voltage_mv[ADC_SAMPLE_INDEX_VTEM]);
    ble_write_u16_le(&frame[12], sample.voltage_mv[ADC_SAMPLE_INDEX_VA201]);
    ble_write_u16_le(&frame[14], sample.voltage_mv[ADC_SAMPLE_INDEX_VBAT]);
    ble_write_u16_le(&frame[16], APP_FRAME_CRC16_DEFAULT);
}

/*
 * 函数作用：
 *   GAP 事件回调，处理广播相关异步事件。
 *
 * 调用时机：
 *   由 Bluedroid GAP 框架在对应事件发生时自动回调，用户不主动调用。
 *
 * 参数含义：
 *   event：事件类型，告诉我们当前发生了什么 GAP 事件
 *   param：该事件附带的参数联合体，不同事件对应不同成员
 *
 * 返回值含义：
 *   无。
 *
 * 是否会修改全局状态：
 *   会修改广播配置完成标志，并可能触发重新开始广播或更新状态灯。
 *
 * 与其他模块/任务/回调的关系：
 *   该回调与 GATTS 回调一起，构成 BLE 模块最核心的异步事件入口。
 */
static void ble_service_gap_event_handler(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t *param)
{
    switch (event) {
    case ESP_GAP_BLE_ADV_DATA_SET_COMPLETE_EVT:
        /*
         * 广播数据配置完成事件。
         *
         * 广播数据配置是异步的，所以不能在调用 esp_ble_gap_config_adv_data() 后
         * 立刻假定配置已经完成。
         * 这里通过清除标志位来表示该异步步骤已结束。
         */
        s_adv_config_done &= (uint8_t)(~ADV_CONFIG_FLAG);
        if (s_adv_config_done == 0) {
            /* 所有待完成的广播配置项都结束后，才真正启动广播。 */
            ble_service_start_advertising();
        }
        break;

    case ESP_GAP_BLE_ADV_START_COMPLETE_EVT:
        /*
         * 广播启动完成事件。
         * param->adv_start_cmpl.status 反映的是“启动操作是否成功”，
         * 而不是“有没有设备连接上来”。
         */
        if (param->adv_start_cmpl.status == ESP_BT_STATUS_SUCCESS) {
            ESP_LOGI(TAG, "advertising start successfully");
        } else {
            ESP_LOGE(TAG, "advertising start failed, status=%d", param->adv_start_cmpl.status);
            ws2812_status_set(WS2812_STATUS_ERROR);
        }
        break;

    case ESP_GAP_BLE_ADV_STOP_COMPLETE_EVT:
        /* 广播停止完成事件，当前只记录日志。 */
        ESP_LOGI(TAG, "advertising stop status=%d", param->adv_stop_cmpl.status);
        break;

    default:
        /* 其他 GAP 事件在当前最小工程里不特别处理。 */
        break;
    }
}

/*
 * 函数作用：
 *   GATTS 事件回调，处理服务注册、属性表创建、建链/断链、CCCD 写入等事件。
 *
 * 调用时机：
 *   由 Bluedroid GATT Server 框架在对应事件发生时自动回调。
 *
 * 参数含义：
 *   event   ：GATTS 事件类型
 *   gatts_if：当前事件所属的 GATT 接口句柄
 *   param   ：事件附带参数联合体，不同事件对应不同成员
 *
 * 返回值含义：
 *   无。
 *
 * 是否会修改全局状态：
 *   会修改连接状态、通知开关、接口句柄、句柄表等模块核心状态。
 *
 * 与其他模块/任务/回调的关系：
 *   - 注册成功后会触发 GAP 配置和属性表创建
 *   - 建链/断链事件会影响 Notify 发送任务是否继续发包
 *   - 写 CCCD 事件会决定电脑端是否真的开始收 Notify
 */
static void ble_service_gatts_event_handler(esp_gatts_cb_event_t event,
                                            esp_gatt_if_t gatts_if,
                                            esp_ble_gatts_cb_param_t *param)
{
    switch (event) {
    case ESP_GATTS_REG_EVT: {
        /*
         * 应用注册完成事件。
         * 只有注册成功后，后续设置设备名、配置广播、创建属性表才有意义。
         */
        if (param->reg.status != ESP_GATT_OK) {
            ESP_LOGE(TAG, "gatts register failed, status=%d", param->reg.status);
            ws2812_status_set(WS2812_STATUS_ERROR);
            break;
        }

        /* 保存协议栈分配给本应用的 GATT 接口句柄。 */
        s_gatts_if = gatts_if;
        ESP_LOGI(TAG, "gatts register ok, app_id=0x%04X", BLE_APP_ID);

        /*
         * esp_ble_gap_set_device_name():
         *   设置 GAP 层设备名，后续广播时可带出该名字。
         */
        esp_err_t ret = esp_ble_gap_set_device_name(APP_BLE_DEVICE_NAME);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "set device name failed: %s", esp_err_to_name(ret));
            ws2812_status_set(WS2812_STATUS_ERROR);
            break;
        }

        /*
         * 标记“广播数据配置”这一异步步骤尚未完成。
         * 后续在 GAP 回调收到完成事件时再清掉它。
         */
        s_adv_config_done |= ADV_CONFIG_FLAG;

        /*
         * esp_ble_gap_config_adv_data():
         *   向 GAP 层提交广播数据配置请求。
         *
         * 注意它是异步接口：
         * 调用成功只表示“请求已提交”，
         * 真正配置完成要等 ESP_GAP_BLE_ADV_DATA_SET_COMPLETE_EVT 事件。
         */
        ret = esp_ble_gap_config_adv_data(&s_adv_data);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "config adv data failed: %s", esp_err_to_name(ret));
            ws2812_status_set(WS2812_STATUS_ERROR);
            break;
        }

        /*
         * esp_ble_gatts_create_attr_tab():
         *   根据 s_gatt_db[] 创建整个属性表。
         *
         * 参数：
         * - s_gatt_db                : 我们定义好的静态属性数据库
         * - gatts_if                 : 当前 GATT 接口
         * - BLE_IDX_NB               : 属性总数
         * - BLE_SERVICE_INSTANCE_ID  : 当前服务实例号
         *
         * 也是异步接口，真正结果会在 ESP_GATTS_CREAT_ATTR_TAB_EVT 事件里返回。
         */
        ret = esp_ble_gatts_create_attr_tab(s_gatt_db, gatts_if, BLE_IDX_NB, BLE_SERVICE_INSTANCE_ID);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "create attr table failed: %s", esp_err_to_name(ret));
            ws2812_status_set(WS2812_STATUS_ERROR);
        }
        break;
    }

    case ESP_GATTS_CREAT_ATTR_TAB_EVT:
        /*
         * 属性表创建完成事件。
         * 到这里协议栈已经为每个属性分配了实际句柄。
         */
        if (param->add_attr_tab.status != ESP_GATT_OK) {
            ESP_LOGE(TAG, "create attr table event failed, status=%d", param->add_attr_tab.status);
            ws2812_status_set(WS2812_STATUS_ERROR);
            break;
        }
        if (param->add_attr_tab.num_handle != BLE_IDX_NB) {
            /*
             * 句柄数量和我们定义的属性项数不一致，说明属性表创建结果异常，
             * 此时继续运行风险较大，因此只记录错误并标出异常状态。
             */
            ESP_LOGE(TAG, "unexpected handle count: %d", param->add_attr_tab.num_handle);
            ws2812_status_set(WS2812_STATUS_ERROR);
            break;
        }

        /* 保存实际句柄表，供后续读写/Notify 使用。 */
        memcpy(s_handle_table, param->add_attr_tab.handles, sizeof(s_handle_table));

        /*
         * esp_ble_gatts_start_service():
         *   让指定服务正式进入可用状态。
         *
         * 这里用 ESP_ERROR_CHECK，表示服务启动失败属于致命初始化错误。
         */
        ESP_ERROR_CHECK(esp_ble_gatts_start_service(s_handle_table[BLE_IDX_SVC]));
        ESP_LOGI(TAG, "gatt service started");
        break;

    case ESP_GATTS_CONNECT_EVT:
        /*
         * 建链事件。
         * 连接建立后，并不代表客户端已经打开 Notify，
         * 所以 s_notify_enabled 要明确重置为 false。
         */
        portENTER_CRITICAL(&s_ble_lock);
        s_conn_id = param->connect.conn_id;
        s_is_connected = true;
        s_notify_enabled = false;
        portEXIT_CRITICAL(&s_ble_lock);

        ESP_LOGI(TAG, "BLE connected, conn_id=%u", param->connect.conn_id);
        ws2812_status_set(WS2812_STATUS_CONNECTED);
        break;

    case ESP_GATTS_DISCONNECT_EVT:
        /*
         * 断链事件。
         * 断开后需要把连接相关状态恢复到“未连接”语义，
         * 然后重新开始广播，等待下一次连接。
         */
        portENTER_CRITICAL(&s_ble_lock);
        s_conn_id = 0xFFFF;
        s_is_connected = false;
        s_notify_enabled = false;
        portEXIT_CRITICAL(&s_ble_lock);

        ESP_LOGI(TAG, "BLE disconnected, reason=%d", param->disconnect.reason);
        ws2812_status_set(WS2812_STATUS_ADVERTISING);
        ble_service_start_advertising();
        break;

    case ESP_GATTS_MTU_EVT:
        /*
         * MTU 更新事件。
         * 当前 18 字节数据帧远小于默认 MTU，但记录日志有助于调试链路状态。
         */
        ESP_LOGI(TAG, "MTU updated: %u", param->mtu.mtu);
        break;

    case ESP_GATTS_WRITE_EVT:
        /*
         * 客户端写事件。
         *
         * 当前工程只关心一种写操作：
         * 客户端往 CCCD 写入 0x0001，用来开启 Notify。
         *
         * 这里不处理：
         * - Prepare Write（长写分段缓存）
         * - 其他业务写命令
         */
        if (!param->write.is_prep &&
            param->write.handle == s_handle_table[BLE_IDX_CHAR_CCCD] &&
            param->write.len == 2) {
            /*
             * CCCD 是 2 字节 little-endian 值：
             * - 0x0000：关闭 Notify/Indicate
             * - 0x0001：开启 Notify
             *
             * 这里手动按 little-endian 还原成 uint16_t。
             */
            uint16_t cccd_value = (uint16_t)param->write.value[1] << 8 | param->write.value[0];

            portENTER_CRITICAL(&s_ble_lock);
            s_notify_enabled = (cccd_value == 0x0001);
            portEXIT_CRITICAL(&s_ble_lock);

            ESP_LOGI(TAG, "notify %s", s_notify_enabled ? "enabled" : "disabled");
        }
        break;

    default:
        /* 其他 GATTS 事件在当前最小工程中不专门处理。 */
        break;
    }
}

/*
 * 函数作用：
 *   BLE 后台发送任务，周期性检查是否需要发送 Notify。
 *
 * 调用时机：
 *   在 ble_service_init() 中由 xTaskCreate() 创建后长期运行。
 *
 * 参数含义：
 *   arg：FreeRTOS 任务参数，当前未使用。
 *
 * 返回值含义：
 *   无。任务函数正常设计为不返回。
 *
 * 是否会修改全局状态：
 *   会读取共享连接状态，并在需要时更新特征值缓存区 s_frame_value。
 *
 * 与其他模块/任务/回调的关系：
 *   - 依赖 GATTS 回调维护连接状态和 notify 开关
 *   - 依赖 ADC 模块提供最新采样
 *   - 依赖 GATT 属性表句柄已经创建成功
 *
 * 设计说明：
 *   这里采用“固定周期轮询 + 条件发送”的方式，逻辑简单、教学友好。
 */
static void ble_service_notify_task(void *arg)
{
    (void)arg;

    /* 发送缓冲区放在任务栈上，每轮循环复用。 */
    uint8_t frame[APP_FRAME_LEN_BYTES];

    while (1) {
        bool connected;
        bool notify_enabled;
        uint16_t conn_id;
        esp_gatt_if_t current_gatts_if;

        /*
         * 先在临界区里把需要的共享状态一次性拷出来，
         * 再在临界区外做后续耗时操作，缩短锁占用时间。
         */
        portENTER_CRITICAL(&s_ble_lock);
        connected = s_is_connected;
        notify_enabled = s_notify_enabled;
        conn_id = s_conn_id;
        current_gatts_if = s_gatts_if;
        portEXIT_CRITICAL(&s_ble_lock);

        /*
         * 只有满足三个条件才发包：
         * 1. 已连接
         * 2. 客户端已开启 Notify
         * 3. GATT 接口有效
         */
        if (connected && notify_enabled && current_gatts_if != ESP_GATT_IF_NONE) {
            /* 先打包一帧固定长度协议数据。 */
            ble_service_build_frame(frame);

            /*
             * 把最新帧复制到本地特征值缓存区。
             * 这样即使客户端主动 Read，也能读到最近一帧数据。
             */
            memcpy(s_frame_value, frame, sizeof(frame));

            /*
             * esp_ble_gatts_set_attr_value():
             *   更新 GATT 属性值缓存。
             *
             * 这里更新的是 Characteristic Value 这一项。
             */
            esp_ble_gatts_set_attr_value(s_handle_table[BLE_IDX_CHAR_VAL], sizeof(frame), frame);

            /*
             * esp_ble_gatts_send_indicate():
             *   向客户端发送一帧 GATT Server 主动通知。
             *
             * 参数含义：
             * - current_gatts_if               : 当前 GATT 接口
             * - conn_id                        : 当前连接 ID
             * - s_handle_table[BLE_IDX_CHAR_VAL] : 要发送的特征值句柄
             * - sizeof(frame)                  : 数据长度
             * - frame                          : 数据缓冲区
             * - false                          : 不要求确认
             *
             * 最后一个参数如果为 false，语义上就是 Notify；
             * 如果为 true，则是 Indicate（需要客户端确认）。
             */
            esp_err_t ret = esp_ble_gatts_send_indicate(
                current_gatts_if,
                conn_id,
                s_handle_table[BLE_IDX_CHAR_VAL],
                sizeof(frame),
                frame,
                false
            );

            if (ret != ESP_OK) {
                /* 发包失败不直接中止任务，只记录警告，等待下一轮继续尝试。 */
                ESP_LOGW(TAG, "notify failed: %s", esp_err_to_name(ret));
            }
        }

        /*
         * 按固定周期节流发送任务。
         * 这决定了本模块“最多多久尝试发一帧”。
         */
        vTaskDelay(pdMS_TO_TICKS(APP_BLE_NOTIFY_PERIOD_MS));
    }
}

/*
 * 函数作用：
 *   完成 BLE 控制器、协议栈、GAP/GATTS 回调、本地 MTU 和发送任务的初始化。
 *
 * 调用时机：
 *   在系统启动阶段，由 app_main() 调用一次。
 *
 * 参数含义：
 *   无。
 *
 * 返回值含义：
 *   - ESP_OK：初始化和任务创建成功
 *   - ESP_FAIL：仅在创建 Notify 任务失败时返回
 *   - 其他错误：关键 BLE 初始化步骤失败时会被 ESP_ERROR_CHECK 直接中止程序
 *
 * 是否会修改全局状态：
 *   会初始化 BLE 子系统，并创建后台发送任务。
 *
 * 与其他模块/任务/回调的关系：
 *   - 依赖 NVS 已经可用
 *   - 依赖状态灯模块已经初始化（用于反馈状态）
 *   - 成功后会间接触发 GAP / GATTS 回调开始工作
 */
esp_err_t ble_service_init(void)
{
    /*
     * esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT)：
     *   释放经典蓝牙（BR/EDR）相关内存，只保留 BLE 所需资源。
     *
     * 当前项目只使用 BLE，不使用经典蓝牙，
     * 因此释放这部分 RAM 是一种常见优化做法。
     */
    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT));

    /*
     * BT_CONTROLLER_INIT_CONFIG_DEFAULT()：
     *   生成一份 BLE 控制器默认初始化配置。
     */
    esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();

    /* 初始化并启用蓝牙控制器，仅打开 BLE 模式。 */
    ESP_ERROR_CHECK(esp_bt_controller_init(&bt_cfg));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_BLE));

    /*
     * 初始化并启用 Bluedroid 主机协议栈。
     *
     * BLE 控制器和 Bluedroid 是两层不同概念：
     * - 控制器偏底层，负责无线收发
     * - Bluedroid 偏协议栈，负责 GAP / GATT 等高层蓝牙逻辑
     */
    ESP_ERROR_CHECK(esp_bluedroid_init());
    ESP_ERROR_CHECK(esp_bluedroid_enable());

    /*
     * 注册 GAP 与 GATTS 回调。
     *
     * 之后所有广播事件、连接事件、属性写事件等，
     * 都会通过这两个回调异步送回来。
     */
    ESP_ERROR_CHECK(esp_ble_gap_register_callback(ble_service_gap_event_handler));
    ESP_ERROR_CHECK(esp_ble_gatts_register_callback(ble_service_gatts_event_handler));

    /*
     * esp_ble_gatts_app_register():
     *   向协议栈注册一个 GATT Server 应用。
     *
     * 注册成功后，会触发 ESP_GATTS_REG_EVT 事件，
     * 后续真正的服务创建流程从那个事件继续展开。
     */
    ESP_ERROR_CHECK(esp_ble_gatts_app_register(BLE_APP_ID));

    /*
     * esp_ble_gatt_set_local_mtu(128)：
     *   设置本地 MTU 上限。
     *
     * 虽然当前协议帧只有 18 字节，远小于默认 ATT MTU，
     * 但把 MTU 设得稍高一些，后续如果扩展协议会更从容。
     */
    ESP_ERROR_CHECK(esp_ble_gatt_set_local_mtu(128));

    /*
     * 预先初始化 Characteristic 当前值。
     *
     * 工程意义：
     * 就算客户端在订阅前先主动 Read，也能读到一个格式正确、
     * 带固定帧头和版本号的初始数据，而不是未定义内容。
     */
    memset(s_frame_value, 0, sizeof(s_frame_value));
    s_frame_value[0] = APP_FRAME_HEAD0;
    s_frame_value[1] = APP_FRAME_HEAD1;
    s_frame_value[2] = APP_BLE_FRAME_VERSION;
    s_frame_value[3] = APP_ADC_CHANNEL_COUNT;

    /*
     * 创建后台 Notify 任务。
     *
     * 注意：
     * 真正是否发送数据，不由任务是否存在决定，
     * 而由任务内部检查连接状态和 Notify 开关决定。
     */
    BaseType_t task_ok = xTaskCreate(
        ble_service_notify_task,
        "ble_notify_task",
        APP_BLE_NOTIFY_TASK_STACK_SIZE,
        NULL,
        APP_BLE_NOTIFY_TASK_PRIORITY,
        NULL
    );

    if (task_ok != pdPASS) {
        return ESP_FAIL;
    }

    return ESP_OK;
}

/*
 * 函数作用：
 *   返回当前是否已建立 BLE 连接。
 *
 * 调用时机：
 *   供其他模块查询 BLE 连接状态时调用。
 *
 * 参数含义：
 *   无。
 *
 * 返回值含义：
 *   true  : 当前存在连接
 *   false : 当前未连接
 *
 * 是否会修改全局状态：
 *   不会，只读取状态。
 *
 * 与其他模块/任务/回调的关系：
 *   读取的是 GATTS 连接/断链事件维护的共享状态。
 */
bool ble_service_is_connected(void)
{
    bool connected;

    /* 在临界区内读取，防止与回调同时修改状态。 */
    portENTER_CRITICAL(&s_ble_lock);
    connected = s_is_connected;
    portEXIT_CRITICAL(&s_ble_lock);
    return connected;
}
