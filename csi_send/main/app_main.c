/*
 * SPDX-FileCopyrightText: 2025-2026 Espressif Systems (Shanghai) CO LTD
 * SPDX-License-Identifier: Apache-2.0
 */

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "argtable3/argtable3.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_app_desc.h"
#include "esp_console.h"
#include "esp_event.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "research_protocol.h"

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61 || \
        (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0))
#define CONFIG_WIFI_BAND_MODE WIFI_BAND_MODE_2G_ONLY
#define CONFIG_WIFI_2G_BANDWIDTHS WIFI_BW_HT40
#define CONFIG_WIFI_5G_BANDWIDTHS WIFI_BW_HT40
#define CONFIG_WIFI_2G_PROTOCOL WIFI_PROTOCOL_11N
#define CONFIG_WIFI_5G_PROTOCOL WIFI_PROTOCOL_11N
#else
#define CONFIG_WIFI_BANDWIDTH WIFI_BW_HT40
#endif

#define CONFIG_ESP_NOW_PHYMODE WIFI_PHY_MODE_HT40
#define CONFIG_ESP_NOW_RATE WIFI_PHY_RATE_MCS0_LGI
#define SEND_CALLBACK_TIMEOUT_MS 2000
#define TX_TASK_STACK_SIZE 4096

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#endif

static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};
static const uint8_t BROADCAST_MAC[ESP_NOW_ETH_ALEN] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
static const char *TAG = "research_tx";

typedef struct {
    char experiment_id[RESEARCH_EXPERIMENT_ID_LEN];
    uint32_t interval_ms;
    uint16_t payload_length;
    bool running;
    uint32_t next_seq;
    uint64_t attempted;
    uint64_t accepted;
    uint64_t immediate_errors;
    uint64_t callback_success;
    uint64_t callback_fail;
    uint64_t callback_timeouts;
    uint64_t orphan_callbacks;
    uint64_t bytes_accepted;
} research_tx_state_t;

static research_tx_state_t g_tx = {
    .experiment_id = CONFIG_RESEARCH_TX_EXPERIMENT_ID,
    .interval_ms = CONFIG_RESEARCH_TX_SEND_INTERVAL_MS,
    .payload_length = CONFIG_RESEARCH_TX_PAYLOAD_LENGTH,
    .running = CONFIG_RESEARCH_TX_AUTOSTART,
};
static portMUX_TYPE g_tx_lock = portMUX_INITIALIZER_UNLOCKED;
static TaskHandle_t g_tx_task = NULL;
static bool g_callback_pending = false;

static struct {
    struct arg_str *experiment_id;
    struct arg_int *interval_ms;
    struct arg_int *payload_length;
    struct arg_lit *start;
    struct arg_lit *stop;
    struct arg_lit *reset_counters;
    struct arg_lit *status;
    struct arg_end *end;
} tx_args;

static research_tx_state_t tx_snapshot(void)
{
    research_tx_state_t snapshot;
    portENTER_CRITICAL(&g_tx_lock);
    snapshot = g_tx;
    portEXIT_CRITICAL(&g_tx_lock);
    return snapshot;
}

static void tx_reset_counters_locked(void)
{
    g_tx.next_seq = 0;
    g_tx.attempted = 0;
    g_tx.accepted = 0;
    g_tx.immediate_errors = 0;
    g_tx.callback_success = 0;
    g_tx.callback_fail = 0;
    g_tx.callback_timeouts = 0;
    g_tx.orphan_callbacks = 0;
    g_tx.bytes_accepted = 0;
}

static void print_tx_telemetry(void)
{
    research_tx_state_t state = tx_snapshot();
    printf("TX_TELEMETRY,%" PRIu32 ",%s,%" PRIu32 ",%u,%" PRIu32 ",%u,%" PRIu32
           ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64
           ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu32 ",%" PRIu32 "\n",
           (uint32_t)(esp_timer_get_time() / 1000), state.experiment_id,
           research_experiment_hash(state.experiment_id), state.running,
           state.interval_ms, state.payload_length, state.next_seq,
           state.attempted, state.accepted, state.immediate_errors,
           state.callback_success, state.callback_fail, state.callback_timeouts,
           state.orphan_callbacks, state.bytes_accepted,
           esp_get_free_heap_size(), esp_get_minimum_free_heap_size());
}

static void telemetry_task(void *arg)
{
    (void)arg;
    printf("type,device_uptime_ms,experiment_id,experiment_hash,running,send_interval_ms,payload_length,"
           "next_seq,attempted,accepted,immediate_errors,callback_success,callback_fail,callback_timeouts,"
           "orphan_callbacks,bytes_accepted,free_heap,minimum_free_heap\n");
    while (true) {
        print_tx_telemetry();
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 0)
static void espnow_send_cb(const esp_now_send_info_t *tx_info, esp_now_send_status_t status)
#else
static void espnow_send_cb(const uint8_t *mac_addr, esp_now_send_status_t status)
#endif
{
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 0)
    (void)tx_info;
#else
    (void)mac_addr;
#endif
    bool notify = false;
    portENTER_CRITICAL(&g_tx_lock);
    if (g_callback_pending) {
        g_callback_pending = false;
        if (status == ESP_NOW_SEND_SUCCESS) {
            g_tx.callback_success++;
        } else {
            g_tx.callback_fail++;
        }
        notify = true;
    } else {
        g_tx.orphan_callbacks++;
    }
    portEXIT_CRITICAL(&g_tx_lock);
    if (notify && g_tx_task) {
        xTaskNotifyGive(g_tx_task);
    }
}

static void transmitter_task(void *arg)
{
    (void)arg;
    uint8_t frame[RESEARCH_FRAME_MAX_LEN];
    g_tx_task = xTaskGetCurrentTaskHandle();

    while (true) {
        research_tx_state_t state = tx_snapshot();
        if (!state.running) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        uint32_t sequence;
        portENTER_CRITICAL(&g_tx_lock);
        sequence = g_tx.next_seq++;
        g_tx.attempted++;
        portEXIT_CRITICAL(&g_tx_lock);

        int64_t start_us = esp_timer_get_time();
        size_t frame_length = research_frame_header_size() + state.payload_length;
        research_frame_result_t encoded = research_frame_encode(
            frame, sizeof(frame), frame_length, state.experiment_id,
            sequence, (uint64_t)start_us,
            RESEARCH_FRAME_FLAG_BROADCAST | RESEARCH_FRAME_FLAG_MONOTONIC_CLOCK);
        if (encoded != RESEARCH_FRAME_OK) {
            ESP_LOGE(TAG, "frame encode failed: %s", research_frame_result_name(encoded));
            portENTER_CRITICAL(&g_tx_lock);
            g_tx.immediate_errors++;
            g_tx.running = false;
            portEXIT_CRITICAL(&g_tx_lock);
            continue;
        }

        ulTaskNotifyTake(pdTRUE, 0);
        portENTER_CRITICAL(&g_tx_lock);
        g_callback_pending = true;
        portEXIT_CRITICAL(&g_tx_lock);
        esp_err_t result = esp_now_send(BROADCAST_MAC, frame, frame_length);
        if (result != ESP_OK) {
            portENTER_CRITICAL(&g_tx_lock);
            g_callback_pending = false;
            g_tx.immediate_errors++;
            portEXIT_CRITICAL(&g_tx_lock);
            ESP_LOGW(TAG, "esp_now_send: %s", esp_err_to_name(result));
        } else {
            portENTER_CRITICAL(&g_tx_lock);
            g_tx.accepted++;
            g_tx.bytes_accepted += state.payload_length;
            portEXIT_CRITICAL(&g_tx_lock);
            if (ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(SEND_CALLBACK_TIMEOUT_MS)) == 0) {
                portENTER_CRITICAL(&g_tx_lock);
                if (g_callback_pending) {
                    g_callback_pending = false;
                    g_tx.callback_timeouts++;
                }
                portEXIT_CRITICAL(&g_tx_lock);
            }
        }

        int64_t elapsed_us = esp_timer_get_time() - start_us;
        int64_t target_us = (int64_t)state.interval_ms * 1000;
        if (elapsed_us < target_us) {
            vTaskDelay(pdMS_TO_TICKS((target_us - elapsed_us + 999) / 1000));
        }
    }
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

#if CONFIG_IDF_TARGET_ESP32C5
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE));
    wifi_protocols_t protocols = {.ghz_2g = CONFIG_WIFI_2G_PROTOCOL, .ghz_5g = CONFIG_WIFI_5G_PROTOCOL};
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {.ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS, .ghz_5g = CONFIG_WIFI_5G_BANDWIDTHS};
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE));
    wifi_protocols_t protocols = {.ghz_2g = CONFIG_WIFI_2G_PROTOCOL};
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {.ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS};
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#else
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, CONFIG_WIFI_BANDWIDTH));
    ESP_ERROR_CHECK(esp_wifi_start());
#endif

    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
#if CONFIG_IDF_TARGET_ESP32C5
    wifi_second_chan_t secondary =
        (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY && CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20) ?
        WIFI_SECOND_CHAN_NONE : WIFI_SECOND_CHAN_BELOW;
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    wifi_second_chan_t secondary = CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20 ?
        WIFI_SECOND_CHAN_NONE : WIFI_SECOND_CHAN_BELOW;
#else
    wifi_second_chan_t secondary = CONFIG_WIFI_BANDWIDTH == WIFI_BW_HT20 ?
        WIFI_SECOND_CHAN_NONE : WIFI_SECOND_CHAN_BELOW;
#endif
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_RESEARCH_TX_CHANNEL, secondary));
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC));
}

static void espnow_init(void)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((const uint8_t *)"pmk1234567890123"));
    ESP_ERROR_CHECK(esp_now_register_send_cb(espnow_send_cb));
    esp_now_peer_info_t peer = {
        .channel = CONFIG_RESEARCH_TX_CHANNEL,
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
    };
    memcpy(peer.peer_addr, BROADCAST_MAC, ESP_NOW_ETH_ALEN);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)
    esp_now_rate_config_t rate_config = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = CONFIG_ESP_NOW_RATE,
        .ersu = false,
        .dcm = false,
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));
#endif
}

static int research_tx_command(int argc, char **argv)
{
    int errors = arg_parse(argc, argv, (void **)&tx_args);
    if (errors) {
        arg_print_errors(stderr, tx_args.end, argv[0]);
        return ESP_ERR_INVALID_ARG;
    }
    research_tx_state_t before = tx_snapshot();
    bool changing_config = tx_args.experiment_id->count || tx_args.interval_ms->count ||
                           tx_args.payload_length->count || tx_args.reset_counters->count;
    if (before.running && changing_config && !tx_args.stop->count) {
        printf("ERROR: stop transmission before changing experiment settings\n");
        return ESP_ERR_INVALID_STATE;
    }
    if (tx_args.experiment_id->count && !research_experiment_id_valid(tx_args.experiment_id->sval[0])) {
        printf("ERROR: experiment_id must be 1-31 characters using A-Z, a-z, 0-9, '.', '_' or '-'\n");
        return ESP_ERR_INVALID_ARG;
    }
    if (tx_args.interval_ms->count && tx_args.interval_ms->ival[0] <= 0) {
        printf("ERROR: interval_ms must be greater than zero\n");
        return ESP_ERR_INVALID_ARG;
    }
    if (tx_args.payload_length->count &&
            (tx_args.payload_length->ival[0] < 0 ||
             tx_args.payload_length->ival[0] > RESEARCH_FRAME_MAX_LEN - (int)research_frame_header_size())) {
        printf("ERROR: payload_length must be between 0 and %u application bytes\n",
               (unsigned)(RESEARCH_FRAME_MAX_LEN - research_frame_header_size()));
        return ESP_ERR_INVALID_ARG;
    }

    portENTER_CRITICAL(&g_tx_lock);
    if (tx_args.stop->count) g_tx.running = false;
    if (tx_args.experiment_id->count) {
        strncpy(g_tx.experiment_id, tx_args.experiment_id->sval[0], sizeof(g_tx.experiment_id) - 1);
        g_tx.experiment_id[sizeof(g_tx.experiment_id) - 1] = '\0';
    }
    if (tx_args.interval_ms->count) g_tx.interval_ms = (uint32_t)tx_args.interval_ms->ival[0];
    if (tx_args.payload_length->count) g_tx.payload_length = (uint16_t)tx_args.payload_length->ival[0];
    if (tx_args.reset_counters->count) tx_reset_counters_locked();
    if (tx_args.start->count) g_tx.running = true;
    portEXIT_CRITICAL(&g_tx_lock);

    if (tx_args.status->count || argc == 1 || changing_config || tx_args.start->count || tx_args.stop->count) {
        print_tx_telemetry();
    }
    return ESP_OK;
}

static void register_research_tx_command(void)
{
    tx_args.experiment_id = arg_str0(NULL, "experiment_id", "<id>", "research experiment/session ID");
    tx_args.interval_ms = arg_int0(NULL, "interval_ms", "<ms>", "send interval in milliseconds");
    tx_args.payload_length = arg_int0(NULL, "payload_length", "<bytes>", "application payload length, 0-186");
    tx_args.start = arg_lit0(NULL, "start", "start transmission");
    tx_args.stop = arg_lit0(NULL, "stop", "stop transmission");
    tx_args.reset_counters = arg_lit0(NULL, "reset_counters", "reset sequence and counters while stopped");
    tx_args.status = arg_lit0(NULL, "status", "print current telemetry immediately");
    tx_args.end = arg_end(8);
    const esp_console_cmd_t command = {
        .command = "research_tx",
        .help = "Configure the research ESP-NOW transmitter",
        .hint = NULL,
        .func = research_tx_command,
        .argtable = &tx_args,
    };
    ESP_ERROR_CHECK(esp_console_cmd_register(&command));
}

static void console_init(void)
{
    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t repl_config = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    repl_config.prompt = "research-tx>";
    repl_config.max_cmdline_length = 256;
    esp_console_register_help_command();
    register_research_tx_command();

#if defined(CONFIG_ESP_CONSOLE_UART_DEFAULT) || defined(CONFIG_ESP_CONSOLE_UART_CUSTOM)
    esp_console_dev_uart_config_t hw_config = ESP_CONSOLE_DEV_UART_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_uart(&hw_config, &repl_config, &repl));
#elif defined(CONFIG_ESP_CONSOLE_USB_CDC)
    esp_console_dev_usb_cdc_config_t hw_config = ESP_CONSOLE_DEV_CDC_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_usb_cdc(&hw_config, &repl_config, &repl));
#elif defined(CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG)
    esp_console_dev_usb_serial_jtag_config_t hw_config = ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&hw_config, &repl_config, &repl));
#else
#error Unsupported console type
#endif
    ESP_ERROR_CHECK(esp_console_start_repl(repl));
}

void app_main(void)
{
    esp_err_t result = nvs_flash_init();
    if (result == ESP_ERR_NVS_NO_FREE_PAGES || result == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        result = nvs_flash_init();
    }
    ESP_ERROR_CHECK(result);
    if (!research_experiment_id_valid(g_tx.experiment_id)) {
        ESP_LOGE(TAG, "Invalid default experiment ID: %s", g_tx.experiment_id);
        g_tx.running = false;
    }

    wifi_init();
    espnow_init();
    const esp_app_desc_t *app = esp_app_get_description();
    printf("TX_DEVICE_INFO,%s,%s,%s %s," MACSTR ",%d\n",
           app->version, esp_get_idf_version(), app->date, app->time,
           MAC2STR(CONFIG_CSI_SEND_MAC), CONFIG_RESEARCH_TX_CHANNEL);
    ESP_LOGI(TAG, "experiment=%s interval=%" PRIu32 "ms payload=%u channel=%d mac=" MACSTR,
             g_tx.experiment_id, g_tx.interval_ms, g_tx.payload_length,
             CONFIG_RESEARCH_TX_CHANNEL, MAC2STR(CONFIG_CSI_SEND_MAC));
    ESP_LOGW(TAG, "ESP-NOW callback success is local MAC status, not receiver delivery proof");

    console_init();
    xTaskCreate(transmitter_task, "research_tx", TX_TASK_STACK_SIZE, NULL, 5, NULL);
    xTaskCreate(telemetry_task, "tx_telemetry", 3072, NULL, 2, NULL);
}
