/*
 * SPDX-FileCopyrightText: 2025-2026 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */
/* Wi-Fi CSI console Example

   This example code is in the Public Domain (or CC0 licensed, at your option.)

   Unless required by applicable law or agreed to in writing, this
   software is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
   CONDITIONS OF ANY KIND, either express or implied.
*/

#include <errno.h>
#include <inttypes.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "esp_err.h"
#include "esp_console.h"

#include "esp_mac.h"
#include "esp_wifi.h"
#include "lwip/inet.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include "ping/ping_sock.h"
#include "hal/uart_ll.h"
#include "mbedtls/base64.h"

#include "led_strip.h"
#include "esp_radar.h"
#include "csi_commands.h"
#include "research_protocol.h"

extern esp_ping_handle_t g_ping_handle;
static led_strip_handle_t led_strip;
#if CONFIG_IDF_TARGET_ESP32C5
#define WS2812_GPIO 27
#elif CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
#define WS2812_GPIO 8
#elif CONFIG_IDF_TARGET_ESP32S3
#define WS2812_GPIO 38
#elif CONFIG_IDF_TARGET_ESP32S2
#define WS2812_GPIO 18
#elif CONFIG_IDF_TARGET_ESP32C3
#define WS2812_GPIO 8
#else
#define WS2812_GPIO 4
#endif
#define RECV_ESPNOW_CSI
#define CONFIG_LESS_INTERFERENCE_CHANNEL    11
#define CONFIG_SEND_DATA_FREQUENCY          100

#define RADAR_EVALUATE_SERVER_PORT          3232
#define RADAR_BUFF_MAX_LEN                  25

static QueueHandle_t g_csi_info_queue    = NULL;
static bool g_wifi_connect_status        = false;
static uint32_t g_send_data_interval     = 1000 / CONFIG_SEND_DATA_FREQUENCY;
static const char *TAG                   = "app_main";
static volatile uint32_t g_csi_received_count = 0;
static volatile uint32_t g_csi_queue_drop_count = 0;
static volatile uint32_t g_csi_queue_high_water = 0;
static volatile uint32_t g_research_rx_valid = 0;
static volatile uint32_t g_research_rx_crc_errors = 0;
static volatile uint32_t g_research_rx_other_errors = 0;
static volatile uint32_t g_research_rx_seq_gaps = 0;
static volatile uint64_t g_research_rx_bytes = 0;
static volatile uint32_t g_research_rx_last_seq = 0;
static volatile uint32_t g_research_rx_experiment_hash = 0;
static bool g_research_rx_has_seq = false;
static portMUX_TYPE g_research_rx_lock = portMUX_INITIALIZER_UNLOCKED;

static struct {
    struct arg_lit *train_start;
    struct arg_lit *train_stop;
    struct arg_lit *train_add;
    struct arg_str *predict_someone_threshold;
    struct arg_str *predict_someone_sensitivity;
    struct arg_str *predict_move_threshold;
    struct arg_str *predict_move_sensitivity;
    struct arg_int *predict_buff_size;
    struct arg_int *predict_outliers_number;
    struct arg_str *collect_taget;
    struct arg_int *collect_number;
    struct arg_int *collect_duration;
    struct arg_lit *csi_start;
    struct arg_lit *csi_stop;
    struct arg_str *csi_output_type;
    struct arg_str *csi_output_format;
    struct arg_int *csi_scale_shift;
    struct arg_int *channel_filter;
    struct arg_lit *physics_mode;
    struct arg_int *send_data_interval;
    struct arg_end *end;
} radar_args;
static struct console_input_config {
    bool train_start;
    float predict_someone_threshold;
    float predict_someone_sensitivity;
    float predict_move_threshold;
    float predict_move_sensitivity;
    uint32_t predict_buff_size;
    uint32_t predict_outliers_number;
    char collect_taget[16];
    uint32_t collect_number;
    char csi_output_type[16];
    char csi_output_format[16];
} g_console_input_config = {
    .predict_someone_threshold = 0,
    .predict_someone_sensitivity = 0.15,
    .predict_move_threshold    = 0.0003,
    .predict_move_sensitivity  = 0.20,
    .predict_buff_size         = 5,
    .predict_outliers_number   = 2,
    .train_start               = false,
    .collect_taget             = "unknown",
    .csi_output_type           = "LLTF",
    .csi_output_format         = "decimal"
};

static TimerHandle_t g_collect_timer_handele = NULL;
static void trigger_router_send_data_task(void *arg);

static research_frame_result_t decode_research_payload(
    const uint8_t *payload, size_t payload_length, research_frame_header_t *header)
{
    static const uint8_t wire_magic[] = {'R', 'C', 'S', 'I'};
    if (!payload || !header || payload_length < research_frame_header_size()) {
        return RESEARCH_FRAME_ERR_LENGTH;
    }
    research_frame_result_t candidate_error = RESEARCH_FRAME_ERR_MAGIC;
    for (size_t offset = 0; offset + research_frame_header_size() <= payload_length; ++offset) {
        if (memcmp(payload + offset, wire_magic, sizeof(wire_magic)) != 0) {
            continue;
        }
        research_frame_header_t candidate;
        memcpy(&candidate, payload + offset, sizeof(candidate));
        if (candidate.frame_len < research_frame_header_size() ||
                offset + candidate.frame_len > payload_length) {
            candidate_error = RESEARCH_FRAME_ERR_LENGTH;
            continue;
        }
        research_frame_result_t result = research_frame_decode(
            payload + offset, candidate.frame_len, header);
        if (result == RESEARCH_FRAME_OK) {
            return result;
        }
        candidate_error = result;
    }
    return candidate_error;
}

void wifi_csi_raw_cb(void *ctx, const wifi_csi_filtered_info_t *info)
{
    g_csi_received_count++;
    if (info->info && info->info->payload && info->info->payload_len >= research_frame_header_size()) {
        research_frame_header_t header;
        research_frame_result_t result = decode_research_payload(
            info->info->payload, info->info->payload_len, &header);
        portENTER_CRITICAL(&g_research_rx_lock);
        if (result == RESEARCH_FRAME_OK) {
            if (g_research_rx_has_seq && header.experiment_hash == g_research_rx_experiment_hash &&
                    header.packet_seq > g_research_rx_last_seq + 1) {
                g_research_rx_seq_gaps += header.packet_seq - g_research_rx_last_seq - 1;
            }
            if (!g_research_rx_has_seq || header.experiment_hash != g_research_rx_experiment_hash ||
                    header.packet_seq > g_research_rx_last_seq) {
                g_research_rx_last_seq = header.packet_seq;
            }
            g_research_rx_experiment_hash = header.experiment_hash;
            g_research_rx_has_seq = true;
            g_research_rx_valid++;
            g_research_rx_bytes += header.payload_length;
        } else if (result == RESEARCH_FRAME_ERR_CRC) {
            g_research_rx_crc_errors++;
        } else if (result != RESEARCH_FRAME_ERR_MAGIC && result != RESEARCH_FRAME_ERR_LENGTH) {
            g_research_rx_other_errors++;
        }
        portEXIT_CRITICAL(&g_research_rx_lock);
    }
    wifi_csi_filtered_info_t *q_data = malloc(sizeof(wifi_csi_filtered_info_t) + info->valid_len);
    if (!q_data) {
        g_csi_queue_drop_count++;
        return;
    }
    *q_data = *info;
    memcpy(q_data->valid_data, info->valid_data, info->valid_len);

    if (!g_csi_info_queue || xQueueSend(g_csi_info_queue, &q_data, 0) == pdFALSE) {
        g_csi_queue_drop_count++;
        ESP_LOGW(TAG, "g_csi_info_queue full");
        free(q_data);
    } else {
        uint32_t queue_depth = uxQueueMessagesWaiting(g_csi_info_queue);
        if (queue_depth > g_csi_queue_high_water) {
            g_csi_queue_high_water = queue_depth;
        }
    }
}

static void system_telemetry_task(void *arg)
{
#if CONFIG_RESEARCH_RX_ESPNOW_MODE
    const char *send_mode = "espnow_research";
#elif WIFI_CSI_SEND_NULL_DATA_ENABLE
    const char *send_mode = "null_data";
#else
    const char *send_mode = "router_ping";
#endif
    printf("type,device_uptime_ms,csi_received,queue_drops,queue_depth,queue_high_water,free_heap,minimum_free_heap,send_interval_ms,send_mode,"
           "research_rx_valid,research_rx_crc_errors,research_rx_other_errors,research_rx_seq_gaps,research_rx_bytes,"
           "research_rx_last_seq,research_rx_experiment_hash\n");
    while (true) {
        uint32_t queue_depth = g_csi_info_queue ? uxQueueMessagesWaiting(g_csi_info_queue) : 0;
        uint32_t research_valid;
        uint32_t research_crc_errors;
        uint32_t research_other_errors;
        uint32_t research_seq_gaps;
        uint64_t research_bytes;
        uint32_t research_last_seq;
        uint32_t research_experiment_hash;
        portENTER_CRITICAL(&g_research_rx_lock);
        research_valid = g_research_rx_valid;
        research_crc_errors = g_research_rx_crc_errors;
        research_other_errors = g_research_rx_other_errors;
        research_seq_gaps = g_research_rx_seq_gaps;
        research_bytes = g_research_rx_bytes;
        research_last_seq = g_research_rx_last_seq;
        research_experiment_hash = g_research_rx_experiment_hash;
        portEXIT_CRITICAL(&g_research_rx_lock);
        printf("SYSTEM_TELEMETRY,%u,%u,%u,%u,%u,%u,%u,%u,%s,%u,%u,%u,%u,%" PRIu64 ",%u,%u\n",
               esp_log_timestamp(), g_csi_received_count, g_csi_queue_drop_count,
               queue_depth, g_csi_queue_high_water,
               esp_get_free_heap_size(), esp_get_minimum_free_heap_size(),
               g_send_data_interval, send_mode, research_valid, research_crc_errors,
               research_other_errors, research_seq_gaps, research_bytes,
               research_last_seq, research_experiment_hash);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

static void collect_timercb(TimerHandle_t timer)
{
    g_console_input_config.collect_number--;

    if (!g_console_input_config.collect_number) {
        xTimerStop(g_collect_timer_handele, 0);
        xTimerDelete(g_collect_timer_handele, 0);
        g_collect_timer_handele = NULL;
        strcpy(g_console_input_config.collect_taget, "unknown");
        return;
    }
}

static int wifi_cmd_radar(int argc, char **argv)
{
    if (arg_parse(argc, argv, (void **) &radar_args) != ESP_OK) {
        arg_print_errors(stderr, radar_args.end, argv[0]);
        return ESP_FAIL;
    }

    if (radar_args.train_start->count) {
        if (!radar_args.train_add->count) {
            esp_radar_train_remove();
        }

        esp_radar_train_start();
        g_console_input_config.train_start = true;
    }

    if (radar_args.train_stop->count) {
        esp_radar_train_stop(&g_console_input_config.predict_someone_threshold,
                             &g_console_input_config.predict_move_threshold);
        g_console_input_config.train_start = false;

        printf("RADAR_DADA,0,0,0,%.6f,0,0,%.6f,0\n",
               g_console_input_config.predict_someone_threshold,
               g_console_input_config.predict_move_threshold);
    }

    if (radar_args.predict_move_threshold->count) {
        g_console_input_config.predict_move_threshold = atof(radar_args.predict_move_threshold->sval[0]);
    }

    if (radar_args.predict_move_sensitivity->count) {
        g_console_input_config.predict_move_sensitivity = atof(radar_args.predict_move_sensitivity->sval[0]);
        ESP_LOGI(TAG, "predict_move_sensitivity: %f", g_console_input_config.predict_move_sensitivity);
    }

    if (radar_args.predict_someone_threshold->count) {
        g_console_input_config.predict_someone_threshold = atof(radar_args.predict_someone_threshold->sval[0]);
    }

    if (radar_args.predict_someone_sensitivity->count) {
        g_console_input_config.predict_someone_sensitivity = atof(radar_args.predict_someone_sensitivity->sval[0]);
        ESP_LOGI(TAG, "predict_someone_sensitivity: %f", g_console_input_config.predict_someone_sensitivity);
    }

    if (radar_args.predict_buff_size->count) {
        g_console_input_config.predict_buff_size = radar_args.predict_buff_size->ival[0];
    }

    if (radar_args.predict_outliers_number->count) {
        g_console_input_config.predict_outliers_number = radar_args.predict_outliers_number->ival[0];
    }

    if (radar_args.collect_taget->count && radar_args.collect_number->count && radar_args.collect_duration->count) {
        g_console_input_config.collect_number = radar_args.collect_number->ival[0];
        strcpy(g_console_input_config.collect_taget, radar_args.collect_taget->sval[0]);

        if (g_collect_timer_handele) {
            xTimerStop(g_collect_timer_handele, portMAX_DELAY);
            xTimerDelete(g_collect_timer_handele, portMAX_DELAY);
        }

        g_collect_timer_handele = xTimerCreate("collect", pdMS_TO_TICKS(radar_args.collect_duration->ival[0]),
                                               true, NULL, collect_timercb);
        xTimerStart(g_collect_timer_handele, portMAX_DELAY);
    }

    if (radar_args.csi_output_format->count) {
        strcpy(g_console_input_config.csi_output_format, radar_args.csi_output_format->sval[0]);
    }

    if (radar_args.csi_output_type->count) {
        esp_radar_config_t radar_config = {0};
        esp_radar_get_config(&radar_config);

        if (!strcasecmp(radar_args.csi_output_type->sval[0], "NULL")) {
            radar_config.csi_config.csi_filtered_cb = NULL;
        } else {
            radar_config.csi_config.csi_filtered_cb = wifi_csi_raw_cb;
            strcpy(g_console_input_config.csi_output_type, radar_args.csi_output_type->sval[0]);
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
            if (!strcasecmp(radar_args.csi_output_type->sval[0], "LLTF")) {
                radar_config.csi_config.acquire_csi_lltf = true;
                radar_config.dec_config.ltf_type = RADAR_LTF_TYPE_LLTF;
                radar_config.dec_config.sub_carrier_step_size = 2;
            } else if (!strcasecmp(radar_args.csi_output_type->sval[0], "HT-LTF")) {
                radar_config.csi_config.acquire_csi_lltf = false;
                radar_config.csi_config.acquire_csi_ht20 = true;
                radar_config.csi_config.acquire_csi_ht40 = true;
                radar_config.csi_config.acquire_csi_vht = true;
                radar_config.dec_config.ltf_type = RADAR_LTF_TYPE_HTLTF;
                radar_config.dec_config.sub_carrier_step_size = 5;
            } else if (!strcasecmp(radar_args.csi_output_type->sval[0], "STBC-HT-LTF")) {
                radar_config.csi_config.acquire_csi_lltf = false;
                radar_config.csi_config.acquire_csi_ht20 = true;
                radar_config.csi_config.acquire_csi_ht40 = true;
                radar_config.csi_config.acquire_csi_vht = true;
                radar_config.dec_config.ltf_type = RADAR_LTF_TYPE_STBC_HTLTF;
                radar_config.dec_config.sub_carrier_step_size = 5;
            } else if (!strcasecmp(radar_args.csi_output_type->sval[0], "HE-LTF")) {
                radar_config.csi_config.acquire_csi_lltf = false;
                radar_config.csi_config.acquire_csi_su = true;
                radar_config.csi_config.acquire_csi_mu = true;
                radar_config.csi_config.acquire_csi_dcm = true;
                radar_config.csi_config.acquire_csi_beamformed = true;
                radar_config.csi_config.acquire_csi_vht = true;
                radar_config.dec_config.ltf_type = RADAR_LTF_TYPE_HELTF;
                radar_config.dec_config.sub_carrier_step_size = 5;
            } else if (!strcasecmp(radar_args.csi_output_type->sval[0], "STBC-HE-LTF")) {
                radar_config.csi_config.acquire_csi_lltf = false;
                radar_config.csi_config.acquire_csi_su = true;
                radar_config.csi_config.acquire_csi_mu = true;
                radar_config.csi_config.acquire_csi_dcm = true;
                radar_config.csi_config.acquire_csi_beamformed = true;
                radar_config.csi_config.acquire_csi_he_stbc_mode = CSI_HE_STBC_MODE_AVERAGE;
                radar_config.csi_config.acquire_csi_vht = true;
                radar_config.dec_config.ltf_type = RADAR_LTF_TYPE_STBC_HELTF;
                radar_config.dec_config.sub_carrier_step_size = 5;
            }
#endif
        }

        esp_radar_change_config(&radar_config);
    }

    if (radar_args.csi_start->count) {
        esp_radar_start();
    }

    if (radar_args.csi_stop->count) {
        esp_radar_stop();
    }

#if CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32C3 || CONFIG_IDF_TARGET_ESP32
    if (radar_args.csi_scale_shift->count) {
        esp_radar_config_t radar_config = {0};
        esp_radar_get_config(&radar_config);
        radar_config.csi_config.shift = radar_args.csi_scale_shift->ival[0];
        esp_radar_change_config(&radar_config);

        ESP_LOGI(TAG, "manually left shift %d bits of the scale of the CSI data", radar_config.csi_config.shift);
    }

    if (radar_args.channel_filter->count) {
        esp_radar_config_t radar_config = {0};
        esp_radar_get_config(&radar_config);
        radar_config.csi_config.channel_filter_en = radar_args.channel_filter->ival[0];
        esp_radar_change_config(&radar_config);

        ESP_LOGI(TAG, "enable(%d) to turn on channel filter to smooth adjacent sub-carrier", radar_config.csi_config.channel_filter_en);
    }
#endif
    if (radar_args.send_data_interval->count) {
        int interval_ms = radar_args.send_data_interval->ival[0];
        if (interval_ms <= 0) {
            ESP_LOGE(TAG, "send_data_interval must be greater than zero");
            return ESP_ERR_INVALID_ARG;
        }
        g_send_data_interval = (uint32_t)interval_ms;

        esp_radar_config_t radar_config = {0};
        esp_radar_get_config(&radar_config);
        radar_config.csi_config.csi_recv_interval = g_send_data_interval;
        esp_radar_change_config(&radar_config);

#if !WIFI_CSI_SEND_NULL_DATA_ENABLE
        /* A running ping session copies interval_ms at creation time. Recreate
         * it so the observed device setting matches the actual packet rate. */
        if (g_wifi_connect_status) {
            xTaskCreate(trigger_router_send_data_task, "restart_router_send_data", 4 * 1024, NULL, 5, NULL);
        }
#endif
    }

    if (radar_args.physics_mode->count) {
        esp_radar_config_t radar_config = {0};
        esp_radar_get_config(&radar_config);
        radar_config.csi_config.channel_filter_en = false;
        radar_config.csi_config.manu_scale = false;
        radar_config.csi_config.shift = 0;
        radar_config.csi_config.csi_filtered_cb = wifi_csi_raw_cb;
        esp_radar_change_config(&radar_config);
        strcpy(g_console_input_config.csi_output_format, "base64");
        ESP_LOGW(TAG, "physics_mode: raw signed I/Q enabled, component order=[imag,real], channel_filter=0, manual_scale=0; absolute phase is not calibrated");
    }

    return ESP_OK;
}

void cmd_register_radar(void)
{
    radar_args.train_start = arg_lit0(NULL, "train_start", "Start calibrating the 'Radar' algorithm");
    radar_args.train_stop  = arg_lit0(NULL, "train_stop", "Stop calibrating the 'Radar' algorithm");
    radar_args.train_add   = arg_lit0(NULL, "train_add", "Calibrate on the basis of saving the calibration results");

    radar_args.predict_someone_threshold = arg_str0(NULL, "predict_someone_threshold", "<0 ~ 1.0>", "Configure the threshold for someone");
    radar_args.predict_someone_sensitivity  = arg_str0(NULL, "predict_someone_sensitivity", "<0 ~ 1.0>", "Configure the sensitivity for someone");
    radar_args.predict_move_threshold    = arg_str0(NULL, "predict_move_threshold", "<0 ~ 1.0>", "Configure the threshold for move");
    radar_args.predict_move_sensitivity  = arg_str0(NULL, "predict_move_sensitivity", "<0 ~ 1.0>", "Configure the sensitivity for move");
    radar_args.predict_buff_size         = arg_int0(NULL, "predict_buff_size", "1 ~ 100", "Buffer size for filtering outliers");
    radar_args.predict_outliers_number   = arg_int0(NULL, "predict_outliers_number", "<1 ~ 100>", "The number of items in the buffer queue greater than the threshold");

    radar_args.collect_taget    = arg_str0(NULL, "collect_tagets", "<0 ~ 20>", "Type of CSI data collected");
    radar_args.collect_number   = arg_int0(NULL, "collect_number", "sequence", "Number of times CSI data was collected");
    radar_args.collect_duration = arg_int0(NULL, "collect_duration", "duration", "Time taken to acquire one CSI data");

    radar_args.csi_start         = arg_lit0(NULL, "csi_start", "Start collecting CSI data from Wi-Fi");
    radar_args.csi_stop          = arg_lit0(NULL, "csi_stop", "Stop CSI data collection from Wi-Fi");
    radar_args.csi_output_type   = arg_str0(NULL, "csi_output_type", "<NULL, LLTF, HT-LTF, HE-LTF, STBC-HT-LTF, STBC-HE-LTF>", "Type of CSI data");
    radar_args.csi_output_format = arg_str0(NULL, "csi_output_format", "<decimal, base64>", "Format of CSI data");
    radar_args.csi_scale_shift   = arg_int0(NULL, "scale_shift", "<0~15>", "manually left shift bits of the scale of the CSI data");
    radar_args.channel_filter    = arg_int0(NULL, "channel_filter", "<0 or 1>", "enable to turn on channel filter to smooth adjacent sub-carrier");
    radar_args.physics_mode      = arg_lit0(NULL, "physics_mode", "Preserve raw signed I/Q for physics-guided processing");

    radar_args.send_data_interval = arg_int0(NULL, "send_data_interval", "<interval_ms>", "The interval between sending null data or ping packets to the router");

    radar_args.end                = arg_end(9);

    const esp_console_cmd_t radar_cmd = {
        .command = "radar",
        .help = "Radar config",
        .hint = NULL,
        .func = &wifi_cmd_radar,
        .argtable = &radar_args
    };

    ESP_ERROR_CHECK(esp_console_cmd_register(&radar_cmd));
}

static void csi_data_print_task(void *arg)
{
    wifi_csi_filtered_info_t *info = NULL;
    char *buffer = malloc(8 * 1024);
    static uint32_t count = 0;

    while (xQueueReceive(g_csi_info_queue, &info, portMAX_DELAY)) {
        size_t len = 0;
        esp_radar_rx_ctrl_info_t *rx_ctrl = &info->rx_ctrl_info;

        if (!count) {
            ESP_LOGI(TAG, "================ CSI RECV ================");
            len += sprintf(buffer + len, "type,sequence,timestamp,taget_seq,target,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_state,agc_gain,fft_gain,len,first_word,data\n");
        }
        uint16_t valid_len = info->valid_len;
        ESP_LOGI(TAG, "info->valid_len1: %d", info->valid_len);
        if (!strcasecmp(g_console_input_config.csi_output_type, "LLTF")) {
            info->valid_len = info->valid_lltf_len;
        } else if (!strcasecmp(g_console_input_config.csi_output_type, "HT-LTF")) {
            info->valid_len = info->valid_lltf_len + info->valid_ht_ltf_len;
        } else if (!strcasecmp(g_console_input_config.csi_output_type, "STBC-HT-LTF")) {
            info->valid_len = info->valid_lltf_len + info->valid_ht_ltf_len + info->valid_stbc_ht_ltf_len;
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
        } else if (!strcasecmp(g_console_input_config.csi_output_type, "HE-LTF")) {
            info->valid_len = info->valid_he_ltf_len;
        } else if (!strcasecmp(g_console_input_config.csi_output_type, "STBC-HE-LTF")) {
            info->valid_len = info->valid_he_ltf_len + info->valid_stbc_he_ltf_len;
#endif
        }
        ESP_LOGD(TAG, "info->valid_len: %d", info->valid_len);
        if (info->valid_len == 0) {
            info->valid_len = valid_len;

        }
        len += sprintf(buffer + len, "CSI_DATA,%d,%u,%u,%s," MACSTR ",%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%u,%d,%d,%d,%d,%d,%d,%d,",
                       count++, esp_log_timestamp(), g_console_input_config.collect_number, g_console_input_config.collect_taget,
                       MAC2STR(info->mac), rx_ctrl->rssi, rx_ctrl->rate, rx_ctrl->signal_mode,
                       rx_ctrl->mcs, rx_ctrl->cwb, 0, 0,
                       0, rx_ctrl->stbc, 0, 0,
                       rx_ctrl->noise_floor, 0, rx_ctrl->channel, rx_ctrl->secondary_channel,
                       rx_ctrl->timestamp, 0, 0, 0, rx_ctrl->agc_gain, rx_ctrl->fft_gain, info->valid_len, 0);

        if (!strcasecmp(g_console_input_config.csi_output_format, "base64")) {
            size_t size = 0;
            mbedtls_base64_encode((uint8_t *)buffer + len, sizeof(buffer) - len, &size, (uint8_t *)info->valid_data, info->valid_len);
            len += size;
            len += sprintf(buffer + len, "\n");
        } else {
            len += sprintf(buffer + len, "\"[%d", info->valid_data[0]);

            for (int i = 1; i < info->valid_len; i++) {
                len += sprintf(buffer + len, ",%d", info->valid_data[i]);
            }

            len += sprintf(buffer + len, "]\"\n");
        }

        printf("%s", buffer);
        free(info);
    }

    free(buffer);
    vTaskDelete(NULL);
}

static void wifi_radar_cb(void *ctx, const wifi_radar_info_t *info)
{
    static float *s_buff_wander  = NULL;
    static float *s_buff_jitter  = NULL;
    static uint32_t s_buff_count = 0;
    uint32_t buff_max_size       = g_console_input_config.predict_buff_size;
    uint32_t buff_outliers_num   = g_console_input_config.predict_outliers_number;
    uint32_t someone_count       = 0;
    uint32_t move_count          = 0;
    bool room_status             = false;
    bool human_status            = false;

    if (!s_buff_wander) {
        s_buff_wander = calloc(RADAR_BUFF_MAX_LEN, sizeof(float));
    }

    if (!s_buff_jitter) {
        s_buff_jitter = calloc(RADAR_BUFF_MAX_LEN, sizeof(float));
    }

    s_buff_wander[s_buff_count % RADAR_BUFF_MAX_LEN] = info->waveform_wander;
    s_buff_jitter[s_buff_count % RADAR_BUFF_MAX_LEN] = info->waveform_jitter;
    s_buff_count++;

    if (s_buff_count < buff_max_size) {
        return;
    }

    extern float trimmean(const float * array, size_t len, float percent);
    extern float median(const float * a, size_t len);

    float wander_average = trimmean(s_buff_wander, RADAR_BUFF_MAX_LEN, 0.5);
    float jitter_midean = median(s_buff_jitter, RADAR_BUFF_MAX_LEN);

    for (int i = 0; i < buff_max_size; i++) {
        uint32_t index = (s_buff_count - 1 - i) % RADAR_BUFF_MAX_LEN;

        if ((wander_average * g_console_input_config.predict_someone_sensitivity > g_console_input_config.predict_someone_threshold)) {
            someone_count++;
        }

        if (s_buff_jitter[index] * g_console_input_config.predict_move_sensitivity > g_console_input_config.predict_move_threshold
                || (s_buff_jitter[index] * g_console_input_config.predict_move_sensitivity > jitter_midean && s_buff_jitter[index] > 0.0002)) {
            move_count++;
        }
    }

    if (someone_count >= 1) {
        room_status = true;
    }

    if (move_count >= buff_outliers_num) {
        human_status = true;
    }

    static uint32_t s_count = 0;

    if (!s_count) {
        ESP_LOGI(TAG, "================ RADAR RECV ================");
        ESP_LOGI(TAG, "type,sequence,timestamp,waveform_wander,someone_threshold,someone_status,waveform_jitter,move_threshold,move_status");
    }

    char timestamp_str[32] = {0};
    sprintf(timestamp_str, "%u", esp_log_timestamp());

    if (ctx) {
        strncpy(timestamp_str, (char *)ctx, 31);
    }

    static uint32_t s_last_move_time    = 0;
    static uint32_t s_last_someone_time = 0;

    if (g_console_input_config.train_start) {
        s_last_move_time    = esp_log_timestamp();
        s_last_someone_time = esp_log_timestamp();

        static bool led_status = false;

        if (led_status) {
            led_strip_set_pixel(led_strip, 0, 0, 0, 0);
        } else {
            led_strip_set_pixel(led_strip, 0, 255, 255, 0);
        }
        led_status = !led_status;
        led_strip_refresh(led_strip);
        return;
    }

    printf("RADAR_DADA,%d,%s,%.6f,%.6f,%.6f,%d,%.6f,%.6f,%.6f,%d\n",
           s_count++, timestamp_str,
           info->waveform_wander, wander_average, g_console_input_config.predict_someone_threshold / g_console_input_config.predict_someone_sensitivity, room_status,
           info->waveform_jitter, jitter_midean, jitter_midean / g_console_input_config.predict_move_sensitivity, human_status);

    if (room_status) {
        if (human_status) {
            led_strip_set_pixel(led_strip, 0, 0, 255, 0);
            ESP_LOGI(TAG, "Someone moved");
            s_last_move_time = esp_log_timestamp();
        } else if (esp_log_timestamp() - s_last_move_time > 3 * 1000) {
            led_strip_set_pixel(led_strip, 0, 255, 255, 255);
            ESP_LOGI(TAG, "Someone");
        }

        s_last_someone_time = esp_log_timestamp();
    } else if (esp_log_timestamp() - s_last_someone_time > 3 * 1000) {
        if (human_status) {
            s_last_move_time = esp_log_timestamp();
            led_strip_set_pixel(led_strip, 0, 255, 0, 0);
        } else if (esp_log_timestamp() - s_last_move_time > 3 * 1000) {
            led_strip_set_pixel(led_strip, 0, 0, 0, 0);
        }
    }
    led_strip_refresh(led_strip);
}

static void trigger_router_send_data_task(void *arg)
{
    esp_radar_config_t radar_config = {0};
    wifi_ap_record_t ap_info         = {0};
    uint8_t sta_mac[6]               = {0};

    esp_radar_get_config(&radar_config);
    esp_wifi_sta_get_ap_info(&ap_info);
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, sta_mac));

    radar_config.csi_config.csi_recv_interval = g_send_data_interval;
    memcpy(radar_config.csi_config.filter_dmac, sta_mac, sizeof(radar_config.csi_config.filter_dmac));

#if WIFI_CSI_SEND_NULL_DATA_ENABLE
    ESP_LOGI(TAG, "Send null data to router");

    memset(radar_config.csi_config.filter_mac, 0, sizeof(radar_config.csi_config.filter_mac));
    esp_radar_change_config(&radar_config);

    typedef struct {
        uint8_t frame_control[2];
        uint16_t duration;
        uint8_t destination_address[6];
        uint8_t source_address[6];
        uint8_t broadcast_address[6];
        uint16_t sequence_control;
    } __attribute__((packed)) wifi_null_data_t;

    wifi_null_data_t null_data = {
        .frame_control       = {0x48, 0x01},
        .duration            = 0x0000,
        .sequence_control    = 0x0000,
    };

    memcpy(null_data.destination_address, ap_info.bssid, 6);
    memcpy(null_data.broadcast_address, ap_info.bssid, 6);
    memcpy(null_data.source_address, sta_mac, 6);

    ESP_LOGW(TAG, "null_data, destination_address: "MACSTR", source_address: "MACSTR", broadcast_address: " MACSTR,
             MAC2STR(null_data.destination_address), MAC2STR(null_data.source_address), MAC2STR(null_data.broadcast_address));

    ESP_ERROR_CHECK(esp_wifi_config_80211_tx_rate(WIFI_IF_STA, WIFI_PHY_RATE_6M));

    for (int i = 0; g_wifi_connect_status; i++) {
        esp_err_t ret = esp_wifi_80211_tx(WIFI_IF_STA, &null_data, sizeof(wifi_null_data_t), true);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "esp_wifi_80211_tx, %s", esp_err_to_name(ret));
            vTaskDelay(pdMS_TO_TICKS(1000));
        }

        vTaskDelay(pdMS_TO_TICKS(g_send_data_interval));
    }

#else
    ESP_LOGI(TAG, "Send ping data to router");

    memcpy(radar_config.csi_config.filter_mac, ap_info.bssid, sizeof(radar_config.csi_config.filter_mac));
    esp_radar_change_config(&radar_config);

    /* stop and delete existing ping session if any */
    if (g_ping_handle != NULL) {
        ESP_LOGI(TAG, "Stopping existing ping session before starting new one");
        esp_ping_stop(g_ping_handle);
        esp_ping_delete_session(g_ping_handle);
        g_ping_handle = NULL;
        ESP_LOGI(TAG, "Existing ping session stopped and deleted");
    }

    esp_ping_config_t config = ESP_PING_DEFAULT_CONFIG();
    config.count       = 0;
    config.data_size   = 1;
    config.interval_ms = g_send_data_interval;

    /**
     * @brief Get the Router IP information from the esp-netif
     */
    esp_netif_ip_info_t local_ip;
    esp_netif_get_ip_info(esp_netif_get_handle_from_ifkey("WIFI_STA_DEF"), &local_ip);
    ESP_LOGI(TAG, "Ping: got ip:" IPSTR ", gw: " IPSTR, IP2STR(&local_ip.ip), IP2STR(&local_ip.gw));
    config.target_addr.u_addr.ip4.addr = ip4_addr_get_u32(&local_ip.gw);
    config.target_addr.type = ESP_IPADDR_TYPE_V4;

    esp_ping_callbacks_t cbs = { 0 };
    esp_ping_new_session(&config, &cbs, &g_ping_handle);
    esp_ping_start(g_ping_handle);
#endif

    vTaskDelete(NULL);
}

/* Event handler for catching system events */
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        g_wifi_connect_status = true;

#if !CONFIG_RESEARCH_RX_ESPNOW_MODE
        xTaskCreate(trigger_router_send_data_task, "trigger_router_send_data", 4 * 1024, NULL, 5, NULL);
#else
        ESP_LOGI(TAG, "Research ESP-NOW RX mode: preserving source MAC filter 1a:00:00:00:00:00");
#endif

#ifdef RECV_ESPNOW_CSI
        ESP_ERROR_CHECK(esp_wifi_set_promiscuous(false));
#endif
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        g_wifi_connect_status = false;
        ESP_LOGW(TAG, "Wi-Fi disconnected");
        esp_radar_config_t radar_config;
        esp_radar_get_config(&radar_config);
        esp_radar_wifi_reinit(&radar_config.wifi_config);

    }
}
esp_err_t ws2812_led_init(void)
{
    led_strip_config_t strip_config = {
        .strip_gpio_num = WS2812_GPIO,
        .max_leds = 1,
    };

#if CONFIG_IDF_TARGET_ESP32C61
    led_strip_spi_config_t spi_config = {
        .clk_src = SPI_CLK_SRC_DEFAULT,
        .spi_bus = SPI2_HOST,
        .flags = {
            .with_dma = false,
        }
    };
    ESP_ERROR_CHECK(led_strip_new_spi_device(&strip_config, &spi_config, &led_strip));
#else
    led_strip_rmt_config_t rmt_config = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,
        .flags = {
            .with_dma = false,
        }
    };
    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_config, &rmt_config, &led_strip));
#endif
    /* Set all LED off to clear all pixels */
    led_strip_clear(led_strip);
    return ESP_OK;
}
void app_main(void)
{
    ESP_LOGI(TAG, "app_main start, line: %d", __LINE__);
    /**
     * @brief Initialize NVS
     */

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_LOGI(TAG, "app_main line: %d", __LINE__);
    /**
     * @brief Install ws2812 driver, Used to display the status of the device
     */
    ws2812_led_init();

    ESP_LOGI(TAG, "app_main start, line: %d", __LINE__);
    /**
     * @brief Turn on the radar module printing information
     */
    esp_log_level_set("esp_radar", ESP_LOG_INFO);

    /**
     * @brief Register serial command
     */
    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t repl_config = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    esp_console_dev_uart_config_t uart_config = ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
    repl_config.prompt = "csi>";
    ESP_ERROR_CHECK(esp_console_new_repl_uart(&uart_config, &repl_config, &repl));

#if CONFIG_IDF_TARGET_ESP32 || CONFIG_IDF_TARGET_ESP32S2
    /**< Fix serial port garbled code due to high baud rate */
    uart_ll_set_sclk(UART_LL_GET_HW(CONFIG_ESP_CONSOLE_UART_NUM), UART_SCLK_APB);
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
    uart_ll_set_baudrate(UART_LL_GET_HW(CONFIG_ESP_CONSOLE_UART_NUM), CONFIG_ESP_CONSOLE_UART_BAUDRATE, APB_CLK_FREQ);
#else
    uart_ll_set_baudrate(UART_LL_GET_HW(CONFIG_ESP_CONSOLE_UART_NUM), CONFIG_ESP_CONSOLE_UART_BAUDRATE);
#endif
#endif

    /**
     * @brief Set the Wi-Fi radar configuration
     */
    esp_radar_csi_config_t csi_config = ESP_RADAR_CSI_CONFIG_DEFAULT();
    esp_radar_wifi_config_t wifi_config = ESP_RADAR_WIFI_CONFIG_DEFAULT();
    esp_radar_dec_config_t dec_config = ESP_RADAR_DEC_CONFIG_DEFAULT();
    memcpy(csi_config.filter_mac, "\x1a\x00\x00\x00\x00\x00", 6);
    csi_config.csi_recv_interval = g_send_data_interval;
    dec_config.wifi_radar_cb     = wifi_radar_cb;
#if WIFI_CSI_SEND_NULL_DATA_ENABLE
    csi_config.dump_ack_en       = true;
#endif
    dec_config.outliers_threshold = 0;
    ESP_ERROR_CHECK(esp_radar_wifi_init(&wifi_config));
    ESP_ERROR_CHECK(esp_radar_csi_init(&csi_config));
    ESP_ERROR_CHECK(esp_radar_dec_init(&dec_config));

    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED, &wifi_event_handler, NULL));

    cmd_register_ping();
    cmd_register_system();
    cmd_register_wifi_config();
    cmd_register_wifi_scan();
    cmd_register_radar();
    ESP_ERROR_CHECK(esp_console_start_repl(repl));

    /**
     * @brief Start Wi-Fi radar
     */
    esp_radar_start();

    /**
     * @brief Initialize CSI serial port printing task, Use tasks to avoid blocking wifi_csi_raw_cb
     */
    g_csi_info_queue = xQueueCreate(64, sizeof(void *));
    xTaskCreate(csi_data_print_task, "csi_data_print", 4 * 1024, NULL, 0, NULL);
    xTaskCreate(system_telemetry_task, "system_telemetry", 3 * 1024, NULL, 1, NULL);
}
