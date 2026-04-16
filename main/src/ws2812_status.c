/*
 * ws2812_status.c
 * --------------------------------------------------------------------------
 * 这是板载单颗 WS2812 状态灯模块的实现文件。
 *
 * 对初学者来说，这个文件非常适合用来理解 ESP-IDF 中 RMT 驱动的“框架式抽象”：
 * - 不是直接手搓 GPIO 翻转时序
 * - 而是把数据先编码成 RMT symbol，再交给硬件定时发送
 *
 * 在当前项目里，WS2812 只承担“状态指示”功能，不追求复杂动画。
 * 但它依然涉及几个很值得学习的概念：
 * 1. RMT 发射通道的创建与启用
 * 2. bytes encoder / copy encoder 的组合使用
 * 3. 自定义 rmt_encoder_t 的状态机写法
 * 4. WS2812 的 GRB 字节顺序和 reset code 概念
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "driver/rmt_encoder.h"
#include "driver/rmt_tx.h"
#include "esp_err.h"
#include "esp_log.h"
#include "app_config.h"
#include "ws2812_status.h"

/* 模块日志标签。 */
static const char *TAG = "WS2812";

/*
 * 自定义一个轻量版 container_of，避免依赖额外系统头实现。
 *
 * container_of 的思想：
 * 已知结构体中某个成员的地址，反推出整个结构体对象的起始地址。
 *
 * 在本文件里这样做的原因是：
 * RMT 框架对外只给我们一个 rmt_encoder_t *，
 * 但我们真正的自定义编码器对象其实是 ws2812_encoder_t。
 * 通过这个宏，可以把“基类指针”还原回“派生结构体指针”。
 */
#define WS2812_CONTAINER_OF(ptr, type, member) \
    ((type *)((uint8_t *)(ptr) - offsetof(type, member)))

/*
 * 自定义 WS2812 编码器对象。
 *
 * 这体现了 ESP-IDF RMT 编码器框架的一种“组合式设计”：
 * - base           : 对外暴露为通用 rmt_encoder_t 接口
 * - bytes_encoder  : 负责把字节流编码成 bit0 / bit1 对应的时序脉冲
 * - copy_encoder   : 负责把 reset code 原样发送出去
 * - state          : 自定义编码流程当前进行到哪一步
 * - reset_code     : WS2812 一帧结尾所需的长低电平符号
 *
 * 你可以把它理解成一个“小状态机对象”：
 * 先发像素，再发 reset，最后回到初始状态。
 */
typedef struct {
    /*
     * RMT 框架要求的通用编码器接口。
     * 外部只认这个字段里的函数指针，不直接认识 ws2812_encoder_t。
     */
    rmt_encoder_t base;

    /*
     * 字节编码器。
     * 它知道如何把 0x00~0xFF 这样的字节，按 bit 序列转换成高低电平时序。
     */
    rmt_encoder_t *bytes_encoder;

    /*
     * 复制编码器。
     * 它不做复杂编码，只负责把给定 symbol 原样送出去。
     * 在这里用来发送 WS2812 的 reset code。
     */
    rmt_encoder_t *copy_encoder;

    /*
     * 当前编码状态。
     * 0 表示正在/将要发送像素数据；
     * 1 表示正在/将要发送 reset code。
     */
    int state;

    /*
     * reset code 对应的 RMT 符号。
     * WS2812 需要在一帧数据之后保持一段较长低电平，
     * 这样灯珠才会“锁存”前面接收到的颜色数据。
     */
    rmt_symbol_word_t reset_code;
} ws2812_encoder_t;

/*
 * RMT 发射通道句柄。
 * 在初始化时创建，后续所有发送都复用它。
 */
static rmt_channel_handle_t s_rmt_channel;

/*
 * 顶层编码器句柄。
 * 实际上会指向我们创建的 ws2812_encoder_t.base。
 */
static rmt_encoder_handle_t s_rmt_encoder;

/* 模块是否已经完成初始化。 */
static bool s_initialized;

/*
 * 单颗 WS2812 的像素缓存，顺序必须是 GRB。
 *
 * 注意：
 * 很多人第一次接触 WS2812 会以为顺序是 RGB，
 * 但大量 WS2812 器件实际采用的是 G-R-B 字节顺序。
 */
static uint8_t s_pixel_grb[3];

/*
 * 函数作用：
 *   这是自定义 RMT 编码器的核心 encode 回调。
 *
 * 调用时机：
 *   当 rmt_transmit() 需要编码一段待发送数据时，由 RMT 框架反复调用。
 *
 * 参数含义：
 *   encoder    ：RMT 框架传进来的通用编码器指针
 *   channel    ：当前使用的 RMT 通道
 *   primary_data：主数据缓冲区，这里对应像素 GRB 字节流
 *   data_size  ：主数据长度，当前单颗灯就是 3 字节
 *   ret_state  ：输出参数，告知框架当前编码状态是否完成/是否内存满
 *
 * 返回值含义：
 *   返回本次调用实际编码出的 symbol 数量。
 *
 * 是否会修改全局状态：
 *   不修改模块全局变量，但会修改当前编码器对象内部 state。
 *
 * 与其他模块/任务/回调的关系：
 *   它是 RMT 框架在发送期间自动回调的底层编码逻辑，
 *   上层调用者只需调用 rmt_transmit()，不直接操作这里。
 *
 * 设计说明：
 *   这是一个典型的“两阶段状态机”：
 *   1. 先把像素字节流编码成时序发送出去
 *   2. 再发送 reset code
 *
 *   之所以不一次性写死成单个编码器，是为了体现 RMT 框架“可组合编码”的思想。
 */
static size_t ws2812_encode(rmt_encoder_t *encoder,
                            rmt_channel_handle_t channel,
                            const void *primary_data,
                            size_t data_size,
                            rmt_encode_state_t *ret_state)
{
    /*
     * 把通用基类指针还原为我们自己的编码器对象。
     * 这是 container_of 在本文件中的核心用途。
     */
    ws2812_encoder_t *led_encoder = WS2812_CONTAINER_OF(encoder, ws2812_encoder_t, base);

    /*
     * session_state：子编码器本次调用返回的状态
     * state        ：当前顶层编码器想返回给 RMT 框架的状态
     * encoded_symbols：累计编码出的 symbol 数量
     */
    rmt_encode_state_t session_state = RMT_ENCODING_RESET;
    rmt_encode_state_t state = RMT_ENCODING_RESET;
    size_t encoded_symbols = 0;

    switch (led_encoder->state) {
    case 0:
        /*
         * 第 1 阶段：发送像素数据。
         *
         * bytes_encoder->encode() 会按 bytes_cfg 中定义的 bit0 / bit1 时序，
         * 把 primary_data 里的 GRB 字节流编码成一串 RMT symbol。
         */
        encoded_symbols += led_encoder->bytes_encoder->encode(
            led_encoder->bytes_encoder,
            channel,
            primary_data,
            data_size,
            &session_state
        );

        /* 像素数据全部编码完成后，切到下一阶段：发送 reset code。 */
        if (session_state & RMT_ENCODING_COMPLETE) {
            led_encoder->state = 1;
        }

        /*
         * 如果当前 RMT 内存不够继续放 symbol，
         * 就先把“内存满”状态返回给框架，等待后续再次回调继续编码。
         */
        if (session_state & RMT_ENCODING_MEM_FULL) {
            state |= RMT_ENCODING_MEM_FULL;
            goto out;
        }
        /* fall through */

    case 1:
        /*
         * 第 2 阶段：发送 reset code。
         *
         * copy_encoder 的作用不是“解释数据”，而是把现成 symbol 原样复制到发送队列中。
         */
        encoded_symbols += led_encoder->copy_encoder->encode(
            led_encoder->copy_encoder,
            channel,
            &led_encoder->reset_code,
            sizeof(led_encoder->reset_code),
            &session_state
        );

        /* reset code 也发完后，整个编码流程才算完成。 */
        if (session_state & RMT_ENCODING_COMPLETE) {
            state |= RMT_ENCODING_COMPLETE;
            led_encoder->state = RMT_ENCODING_RESET;
        }

        /* 同样处理“RMT 内存满”的情况。 */
        if (session_state & RMT_ENCODING_MEM_FULL) {
            state |= RMT_ENCODING_MEM_FULL;
            goto out;
        }
        break;

    default:
        /* 理论上不会走到这里，保留 default 主要是让状态机更完整。 */
        break;
    }

out:
    *ret_state = state;
    return encoded_symbols;
}

/*
 * 函数作用：
 *   删除自定义 WS2812 编码器并释放其占用资源。
 *
 * 调用时机：
 *   当外部通过 RMT 框架删除该编码器时调用。当前工程没有显式释放流程，
 *   但作为完整的编码器对象，必须实现这个接口。
 *
 * 参数含义：
 *   encoder：通用编码器指针，内部会还原成 ws2812_encoder_t。
 *
 * 返回值含义：
 *   ESP_OK：释放流程完成。
 *
 * 是否会修改全局状态：
 *   不修改模块全局变量，但会释放堆内存和子编码器对象。
 */
static esp_err_t ws2812_encoder_del(rmt_encoder_t *encoder)
{
    ws2812_encoder_t *led_encoder = WS2812_CONTAINER_OF(encoder, ws2812_encoder_t, base);
    rmt_del_encoder(led_encoder->bytes_encoder);
    rmt_del_encoder(led_encoder->copy_encoder);
    free(led_encoder);
    return ESP_OK;
}

/*
 * 函数作用：
 *   把自定义编码器重置到初始状态。
 *
 * 调用时机：
 *   RMT 框架在需要重新开始一轮发送时会调用。
 *
 * 参数含义：
 *   encoder：通用编码器指针。
 *
 * 返回值含义：
 *   ESP_OK：重置成功。
 *
 * 是否会修改全局状态：
 *   不修改模块全局变量，但会重置编码器内部状态机与子编码器状态。
 */
static esp_err_t ws2812_encoder_reset(rmt_encoder_t *encoder)
{
    ws2812_encoder_t *led_encoder = WS2812_CONTAINER_OF(encoder, ws2812_encoder_t, base);

    /*
     * rmt_encoder_reset()：
     *   把子编码器内部状态重置，保证下一轮编码从头开始。
     */
    rmt_encoder_reset(led_encoder->bytes_encoder);
    rmt_encoder_reset(led_encoder->copy_encoder);
    led_encoder->state = RMT_ENCODING_RESET;
    return ESP_OK;
}

/*
 * 函数作用：
 *   创建一个只服务于单颗 WS2812 的最小编码器对象。
 *
 * 调用时机：
 *   在 ws2812_status_init() 中初始化模块时调用一次。
 *
 * 参数含义：
 *   ret_encoder：输出参数，成功时返回顶层编码器句柄。
 *
 * 返回值含义：
 *   - ESP_OK：创建成功
 *   - ESP_ERR_NO_MEM：堆内存分配失败
 *   - 其他错误码：子编码器创建失败
 *
 * 是否会修改全局状态：
 *   不直接写模块全局变量；由调用者决定是否把返回句柄保存下来。
 *
 * 与其他模块/任务/回调的关系：
 *   返回的编码器会在后续 rmt_transmit() 中被反复调用。
 */
static esp_err_t ws2812_new_encoder(rmt_encoder_handle_t *ret_encoder)
{
    esp_err_t ret;

    /*
     * calloc(1, size)：
     *   分配一块堆内存并自动清零。
     *
     * 用 calloc 而不是 malloc 的好处是：
     * 编码器初始状态字段默认从 0 开始，更安全。
     */
    ws2812_encoder_t *led_encoder = calloc(1, sizeof(ws2812_encoder_t));
    if (led_encoder == NULL) {
        return ESP_ERR_NO_MEM;
    }

    /* 填充顶层“虚函数表”，把自定义行为挂到 base 上。 */
    led_encoder->base.encode = ws2812_encode;
    led_encoder->base.del = ws2812_encoder_del;
    led_encoder->base.reset = ws2812_encoder_reset;

    /*
     * rmt_bytes_encoder_config_t 描述 bit0 / bit1 分别编码成什么脉冲时序。
     *
     * 在 10MHz 分辨率下：
     * - 1 tick = 0.1us
     * - 3 tick = 0.3us
     * - 9 tick = 0.9us
     *
     * 这组参数对应一类常见的 WS2812 时序近似：
     * - bit0：高电平短，低电平长
     * - bit1：高电平长，低电平短
     */
    rmt_bytes_encoder_config_t bytes_cfg = {
        .bit0 = {
            .level0 = 1,
            .duration0 = 3,
            .level1 = 0,
            .duration1 = 9,
        },
        .bit1 = {
            .level0 = 1,
            .duration0 = 9,
            .level1 = 0,
            .duration1 = 3,
        },
        .flags.msb_first = 1,
    };

    /*
     * rmt_new_bytes_encoder():
     *   创建一个“按 bit 时序规则把字节流编码成 RMT symbol”的编码器。
     */
    ret = rmt_new_bytes_encoder(&bytes_cfg, &led_encoder->bytes_encoder);
    if (ret != ESP_OK) {
        free(led_encoder);
        return ret;
    }

    /*
     * rmt_new_copy_encoder():
     *   创建一个“原样复制 symbol”的编码器。
     * 当前用于发送 reset code。
     */
    rmt_copy_encoder_config_t copy_cfg = {};
    ret = rmt_new_copy_encoder(&copy_cfg, &led_encoder->copy_encoder);
    if (ret != ESP_OK) {
        rmt_del_encoder(led_encoder->bytes_encoder);
        free(led_encoder);
        return ret;
    }

    /*
     * reset code 总时长约 50us。
     *
     * WS2812 在收到一帧数据之后，需要一段足够长的低电平来锁存颜色数据。
     * 当前配置：250 + 250 tick = 500 tick = 50us。
     */
    led_encoder->reset_code = (rmt_symbol_word_t) {
        .level0 = 0,
        .duration0 = 250,
        .level1 = 0,
        .duration1 = 250,
    };

    *ret_encoder = &led_encoder->base;
    return ESP_OK;
}

/*
 * 函数作用：
 *   把指定 RGB 颜色真正发送到单颗 WS2812。
 *
 * 调用时机：
 *   由 ws2812_status_set() 在状态变化时调用；初始化结束时也会调用一次熄灯。
 *
 * 参数含义：
 *   red   ：红色分量
 *   green ：绿色分量
 *   blue  ：蓝色分量
 *
 * 返回值含义：
 *   - ESP_OK：发送完成
 *   - ESP_ERR_INVALID_STATE：模块尚未初始化
 *   - 其他错误码：RMT 发送或等待完成失败
 *
 * 是否会修改全局状态：
 *   会修改 s_pixel_grb 缓冲区内容，并驱动 RMT 外设开始发送。
 *
 * 与其他模块/任务/回调的关系：
 *   这是本模块内部真正“落到硬件输出”的核心函数。
 */
static esp_err_t ws2812_status_write_rgb(uint8_t red, uint8_t green, uint8_t blue)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }

    /*
     * WS2812 数据顺序是 GRB，不是常见的 RGB。
     *
     * 这一步非常关键：
     * 如果顺序写错，灯仍然会亮，但颜色会和你期望的不一致。
     */
    s_pixel_grb[0] = green;
    s_pixel_grb[1] = red;
    s_pixel_grb[2] = blue;

    /*
     * rmt_transmit_config_t 用于配置一次发送行为。
     * loop_count = 0 表示只发送一次，不循环。
     */
    rmt_transmit_config_t tx_cfg = {
        .loop_count = 0,
    };

    /*
     * rmt_transmit():
     *   启动一次 RMT 发送。
     *
     * 参数含义：
     * - s_rmt_channel           : 发送通道
     * - s_rmt_encoder           : 编码器对象
     * - s_pixel_grb             : 原始待发送数据
     * - sizeof(s_pixel_grb)     : 数据长度（3 字节）
     * - &tx_cfg                 : 发送配置
     */
    esp_err_t ret = rmt_transmit(s_rmt_channel, s_rmt_encoder, s_pixel_grb, sizeof(s_pixel_grb), &tx_cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "rmt_transmit failed: %s", esp_err_to_name(ret));
        return ret;
    }

    /*
     * rmt_tx_wait_all_done():
     *   阻塞等待当前通道上所有待发送内容完成。
     *
     * 最后一个参数 20 表示超时时间，单位毫秒。
     * 之所以等待完成，是为了确保本次颜色数据真正发完，再返回给调用者。
     */
    ret = rmt_tx_wait_all_done(s_rmt_channel, 20);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "wait tx done failed: %s", esp_err_to_name(ret));
        return ret;
    }

    return ESP_OK;
}

/*
 * 函数作用：
 *   初始化 RMT 发送通道和 WS2812 编码器，并让状态灯进入可用状态。
 *
 * 调用时机：
 *   由 app_main() 在系统启动初期调用一次。
 *
 * 参数含义：
 *   无。
 *
 * 返回值含义：
 *   - ESP_OK：初始化成功
 *   - 其他错误码：RMT 通道、编码器或使能过程失败
 *
 * 是否会修改全局状态：
 *   会初始化 s_rmt_channel、s_rmt_encoder、s_initialized，
 *   并在最后把灯写成黑色（熄灭）。
 *
 * 与其他模块/任务/回调的关系：
 *   其他模块只有在这里成功后，才能通过 ws2812_status_set() 控灯。
 */
esp_err_t ws2812_status_init(void)
{
    /* 已经初始化过就直接返回，避免重复创建底层资源。 */
    if (s_initialized) {
        return ESP_OK;
    }

    /*
     * rmt_tx_channel_config_t 描述发送通道的硬件参数。
     *
     * 字段说明：
     * - clk_src          : 时钟源，当前使用默认值
     * - gpio_num         : RMT 输出对应的 GPIO
     * - mem_block_symbols: 通道内部可用 symbol 缓冲大小
     * - resolution_hz    : 时间分辨率
     * - trans_queue_depth: 发送队列深度
     */
    rmt_tx_channel_config_t tx_chan_cfg = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .gpio_num = APP_WS2812_GPIO,
        .mem_block_symbols = 64,
        .resolution_hz = APP_WS2812_RMT_RESOLUTION_HZ,
        .trans_queue_depth = 4,
    };

    /*
     * rmt_new_tx_channel():
     *   创建一个 RMT 发射通道句柄。
     */
    esp_err_t ret = rmt_new_tx_channel(&tx_chan_cfg, &s_rmt_channel);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "new tx channel failed: %s", esp_err_to_name(ret));
        return ret;
    }

    /* 创建自定义 WS2812 编码器。 */
    ret = ws2812_new_encoder(&s_rmt_encoder);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "new encoder failed: %s", esp_err_to_name(ret));
        return ret;
    }

    /*
     * rmt_enable():
     *   使能指定 RMT 通道，使其进入可工作状态。
     *
     * 通道创建成功并不代表已经可发数据，通常还需要 enable 一次。
     */
    ret = rmt_enable(s_rmt_channel);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "rmt enable failed: %s", esp_err_to_name(ret));
        return ret;
    }

    s_initialized = true;
    ESP_LOGI(TAG, "WS2812 initialized on GPIO%d", APP_WS2812_GPIO);

    /* 初始化完成后，先写黑色（全灭），确保状态灯处于可预期初始状态。 */
    return ws2812_status_write_rgb(0, 0, 0);
}

/*
 * 函数作用：
 *   根据预定义业务状态切换状态灯颜色。
 *
 * 调用时机：
 *   当系统状态变化时由其他模块调用。
 *
 * 参数含义：
 *   mode：状态模式枚举值。
 *
 * 返回值含义：
 *   - ESP_OK：颜色切换成功
 *   - ESP_ERR_INVALID_ARG：传入模式无效
 *   - 其他错误码：底层写灯失败
 *
 * 是否会修改全局状态：
 *   不维护额外状态记录，只是立即把颜色写到硬件。
 *
 * 与其他模块/任务/回调的关系：
 *   app_main()、BLE 模块等都会通过它表达系统状态。
 */
esp_err_t ws2812_status_set(ws2812_status_mode_t mode)
{
    switch (mode) {
    case WS2812_STATUS_IDLE:
        /* 白色低亮度：表示系统空闲但正常。 */
        return ws2812_status_write_rgb(4, 4, 4);

    case WS2812_STATUS_ADVERTISING:
        /* 蓝色：正在广播，等待电脑端连接。 */
        return ws2812_status_write_rgb(0, 0, 16);

    case WS2812_STATUS_CONNECTED:
        /* 绿色：BLE 已连接，当前系统可稳定发送数据。 */
        return ws2812_status_write_rgb(0, 16, 0);

    case WS2812_STATUS_ERROR:
        /* 红色：初始化或运行异常。 */
        return ws2812_status_write_rgb(16, 0, 0);

    default:
        /* 兜底参数检查，防止上层传入非法枚举值。 */
        return ESP_ERR_INVALID_ARG;
    }
}
