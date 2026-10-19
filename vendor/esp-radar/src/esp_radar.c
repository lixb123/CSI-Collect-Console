/*
 * SPDX-FileCopyrightText: 2025-2026 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"

#include "esp_mac.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_log.h"
#include "esp_check.h"
#include "esp_private/esp_wifi_he_types_private.h"
#include "esp_radar.h"
#include "esp_csi_gain_ctrl.h"
#include "esp_radar_motion_dec.h"
#include "utils.h"

static const char *TAG                          = "esp_radar";
static const char *TAG_DETECTION                = "csi_detection_task";
static const char *TAG_TRAIN                     = "esp_radar_train";
#define CSI_DETECTION_EXIT_BIT      BIT0
#define CSI_PREPROCESSING_EXIT_BIT  BIT1

#ifndef ESP_RADAR_MAX_PEERS
#define ESP_RADAR_MAX_PEERS 16
#endif

/* Runtime log output control for this module (per-tag) */
static bool s_esp_radar_log_output_enabled = true;
static bool s_esp_radar_saved_log_levels_valid = false;
static const char *s_esp_radar_log_tags[] = {
    "esp_radar",
    "csi_detection_task",
    "esp_radar_train",
    "esp_radar_filter",
    "esp_radar_csi_data_rebuild",
    "esp_radar_csi_rx_cb",
};
static esp_log_level_t s_esp_radar_saved_log_levels[sizeof(s_esp_radar_log_tags) / sizeof(s_esp_radar_log_tags[0])] = {0};

void esp_radar_set_log_output(bool enable, esp_log_level_t level)
{
    const size_t tag_num = sizeof(s_esp_radar_log_tags) / sizeof(s_esp_radar_log_tags[0]);

    if (!enable) {
        if (!s_esp_radar_log_output_enabled) {
            return;
        }
        for (size_t i = 0; i < tag_num; ++i) {
            s_esp_radar_saved_log_levels[i] = esp_log_level_get(s_esp_radar_log_tags[i]);
            esp_log_level_set(s_esp_radar_log_tags[i], ESP_LOG_NONE);
        }
        s_esp_radar_saved_log_levels_valid = true;
    } else {
        for (size_t i = 0; i < tag_num; ++i) {
            s_esp_radar_saved_log_levels[i] = level;
            esp_log_level_set(s_esp_radar_log_tags[i], level);
        }
        s_esp_radar_saved_log_levels_valid = true;
    }

    s_esp_radar_log_output_enabled = enable;
}

#define RADAR_MOTION_DEC_WINDOW_DEFAULT      4

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#endif

/**
 * @brief CSI amplitude ring buffer structure
 *
 * Stores CSI amplitude data, timestamps, and sequence IDs in a ring buffer format
 */
typedef struct {
    float **amplitude;      /**< 2D array for amplitude data: [buff_size][subcarrier_len] */
    uint32_t *timestamp;    /**< Timestamp array: [buff_size] */
    uint32_t *seq_id;       /**< Frame sequence ID array: [buff_size] */
} csi_data_buff_t;

/**
 * @brief CSI window context structure
 *
 * Manages ring buffer and sliding window operations for CSI data processing
 */
typedef struct {
    uint32_t buff_size;         /**< Ring buffer capacity (number of frames) */
    uint32_t handle_window;     /**< Target processing window size (number of frames) */
    uint32_t window_start_seq;  /**< Current window start frame absolute sequence number */
    uint32_t next_seq;          /**< Next frame absolute sequence number to write */
    uint32_t last_timestamp;    /**< Last processed frame timestamp (ms) */
    bool ts_delta_relaxed;      /**< True when timestamp-delta threshold is temporarily relaxed */
} csi_window_ctx_t;

/**
 * @brief Radar context structure
 *
 * Main context structure containing all radar system state and configuration
 */
typedef struct {
    EventGroupHandle_t          task_exit_group;     /**< Event group for task exit synchronization */
    QueueHandle_t               csi_info_queue;      /**< Queue for CSI filtered information (peer dispatch) */
    QueueHandle_t               csi_data_queue;      /**< Queue for CSI window indices (peer dispatch) */
    esp_radar_dec_config_t      dec_config;          /**< Decoder configuration */
    esp_radar_csi_config_t      csi_config;         /**< CSI configuration */
    esp_radar_wifi_config_t     wifi_config;        /**< WiFi configuration */
    esp_radar_espnow_config_t   espnow_config;       /**< ESP-NOW configuration */
    bool                        init_flag;          /**< Initialization flag */
    bool                        run_flag;           /**< Running flag */
    uint32_t                    buff_size;          /**< Ring buffer capacity shared by all peers */
    uint32_t                    handle_window;      /**< Sliding window size shared by all peers */
    struct esp_radar_peer      *default_peer;       /**< Default peer (for legacy APIs) */
    struct esp_radar_peer      *peers[ESP_RADAR_MAX_PEERS]; /**< Registered peers */
    size_t                      peer_count;         /**< Number of registered peers */
    bool                        lltf_bit_mode;      /**< LLTF bit mode flag */
} radar_ctx_t;

/**
 * @brief CSI data buffer index structure
 *
 * Represents a window index in the ring buffer, can be accessed as struct or 32-bit integer
 */
typedef struct {
    union {
        struct {
            uint8_t begin;      /**< Window start position in ring buffer */
            uint8_t end;        /**< Window end position in ring buffer */
            uint8_t window;     /**< Window size */
            uint8_t reserved;   /**< Reserved byte */
        };
        uint32_t data;         /**< Access as 32-bit integer */
    };
} csi_data_buff_index_t;

#define CSI_WANDER_NUM              10
#define CSI_WANDER_THRESHOLD        0.002f
#define RADAR_BUFF_NUM            3
#define RADAR_OUTLIERS_THRESHOLD  0.005f

/**
 * @brief Radar calibration status enumeration
 */
typedef enum {
    RADAR_CALIBRATE_NO = 0,         /**< No calibration */
    RADAR_CALIBRATE_PROGRESS,       /**< Calibration in progress */
    RADAR_CALIBRATE_COMPLETE,       /**< Calibration completed */
} radar_calibrate_status_t;

/**
 * @brief Radar calibration structure
 *
 * Stores calibration data and statistics for radar training and detection
 */
typedef struct {
    radar_calibrate_status_t calibrate_status;  /**< Current calibration status */
    float *data[CSI_WANDER_NUM];                  /**< Array of training sample data pointers */
    size_t buff_size;                            /**< Buffer size for jitter calculation */
    float waveform_jitter_buff[RADAR_BUFF_NUM];  /**< Buffer for waveform jitter values */
    size_t data_num;                             /**< Number of training samples collected */
    float none_wander_sum;                       /**< Sum of wander values for non-static environment */
    float none_wander_count;                     /**< Count of wander values for non-static environment */
    float none_wander;                           /**< Current wander value for non-static environment */
    float static_wander;                         /**< Static environment wander value */
    uint16_t subcarrier_len;                     /**< Subcarrier length */
    esp_radar_train_action_t last_action;        /**< Latest decision taken by the training pipeline */
} radar_calibrate_t;

static radar_ctx_t s_ctx = {0};
static portMUX_TYPE s_peer_mux = portMUX_INITIALIZER_UNLOCKED;

typedef struct esp_radar_peer {
    uint8_t mac[6];
    bool is_wildcard;

    /* Runtime state per peer */
    csi_window_ctx_t  window_ctx;
    uint16_t          subcarrier_len;
    csi_data_buff_t  *csi_data_buff;

    /* Motion detection buffers per peer */
    float           **motion_dec_buff;
    bool              motion_dec_buff_allocated;
    uint32_t          motion_dec_buff_num;
    uint32_t          motion_dec_capacity;

    /* Training/calibration per peer */
    radar_calibrate_t *cal;
    float              waveform_wander_last;

    /* Per-peer sequence counter */
    uint32_t csi_seq;

    /* Per-peer outlier filter state */
    uint8_t outlier_streak;
} esp_radar_peer_t;

typedef struct {
    esp_radar_peer_t *peer;
    wifi_csi_filtered_info_t *filtered_info;
} csi_info_msg_t;

typedef struct {
    esp_radar_peer_t *peer;
    csi_data_buff_index_t index;
} csi_window_msg_t;

/* Forward declarations */
static esp_err_t esp_radar_peer_ensure_ring_buffers(esp_radar_peer_t *peer);
static esp_radar_peer_t *esp_radar_peer_find_exact_nolock(const uint8_t mac[6]);
static esp_radar_peer_t *esp_radar_peer_find_wildcard_nolock(void);
static bool esp_radar_mac_is_wildcard(const uint8_t mac[6]);
static esp_err_t esp_radar_mac_addr_filter(wifi_csi_info_t *info);
static esp_err_t esp_radar_sync_default_peer(const uint8_t mac[6]);
static void esp_radar_peer_free_motion_dec_buffers(esp_radar_peer_t *peer);
static void esp_radar_peer_free_ring_buffers(esp_radar_peer_t *peer);
static void radar_calibrate_free_entries(radar_calibrate_t *cal);

static esp_err_t esp_radar_peer_register(esp_radar_peer_t *peer)
{
    if (!peer) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t ret = ESP_ERR_INVALID_SIZE;
    portENTER_CRITICAL(&s_peer_mux);
    /* Reject duplicate wildcard */
    if (peer->is_wildcard && esp_radar_peer_find_wildcard_nolock()) {
        ret = ESP_ERR_INVALID_RESPONSE;
        goto OUT;
    }
    /* Reject duplicate exact */
    if (!peer->is_wildcard && esp_radar_peer_find_exact_nolock(peer->mac)) {
        ret = ESP_ERR_INVALID_RESPONSE;
        goto OUT;
    }
    for (size_t i = 0; i < ESP_RADAR_MAX_PEERS; ++i) {
        if (!s_ctx.peers[i]) {
            s_ctx.peers[i] = peer;
            s_ctx.peer_count++;
            ret = ESP_OK;
            goto OUT;
        }
    }
OUT:
    portEXIT_CRITICAL(&s_peer_mux);
    return ret;
}

static void esp_radar_peer_unregister(esp_radar_peer_t *peer)
{
    if (!peer) {
        return;
    }
    portENTER_CRITICAL(&s_peer_mux);
    for (size_t i = 0; i < ESP_RADAR_MAX_PEERS; ++i) {
        if (s_ctx.peers[i] == peer) {
            s_ctx.peers[i] = NULL;
            if (s_ctx.peer_count > 0) {
                s_ctx.peer_count--;
            }
            break;
        }
    }
    portEXIT_CRITICAL(&s_peer_mux);
}

static esp_radar_peer_t *esp_radar_peer_create(const uint8_t mac[6])
{
    if (!mac) {
        return NULL;
    }
    esp_radar_peer_t *peer = RADAR_MALLOC_RETRY(sizeof(esp_radar_peer_t));
    memset(peer, 0, sizeof(esp_radar_peer_t));
    memcpy(peer->mac, mac, 6);
    peer->is_wildcard = esp_radar_mac_is_wildcard(mac);
    peer->waveform_wander_last = 1.0f;
    peer->csi_seq = 0;
    peer->outlier_streak = 0;
    return peer;
}

static esp_err_t esp_radar_sync_default_peer(const uint8_t mac[6])
{
    if (!mac) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_ctx.default_peer) {
        if (s_ctx.peer_count != 0) {
            ESP_LOGI(TAG, "Skip legacy default peer creation because %u peer(s) already exist", (unsigned int)s_ctx.peer_count);
            return ESP_OK;
        }

        esp_radar_handle_t h = NULL;
        esp_err_t ret = esp_radar_new_peer(mac, &h);
        if (ret != ESP_OK || !h) {
            ESP_LOGW(TAG, "Failed to create default peer from filter_mac=" MACSTR, MAC2STR(mac));
            return (ret != ESP_OK) ? ret : ESP_FAIL;
        }
        s_ctx.default_peer = (esp_radar_peer_t *)h;
        ESP_LOGI(TAG, "Created default peer from filter_mac=" MACSTR, MAC2STR(mac));
        return ESP_OK;
    }

    bool is_wildcard = esp_radar_mac_is_wildcard(mac);
    if (s_ctx.default_peer->is_wildcard == is_wildcard &&
            memcmp(s_ctx.default_peer->mac, mac, sizeof(s_ctx.default_peer->mac)) == 0) {
        return ESP_OK;
    }

    esp_err_t ret = ESP_OK;
    portENTER_CRITICAL(&s_peer_mux);
    for (size_t i = 0; i < ESP_RADAR_MAX_PEERS; ++i) {
        esp_radar_peer_t *peer = s_ctx.peers[i];
        if (!peer || peer == s_ctx.default_peer) {
            continue;
        }
        if ((is_wildcard && peer->is_wildcard) ||
                (!is_wildcard && !peer->is_wildcard && memcmp(peer->mac, mac, sizeof(peer->mac)) == 0)) {
            ret = ESP_ERR_INVALID_RESPONSE;
            break;
        }
    }
    if (ret == ESP_OK) {
        memcpy(s_ctx.default_peer->mac, mac, sizeof(s_ctx.default_peer->mac));
        s_ctx.default_peer->is_wildcard = is_wildcard;
    }
    portEXIT_CRITICAL(&s_peer_mux);

    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "Synced default peer to filter_mac=" MACSTR, MAC2STR(mac));
    } else {
        ESP_LOGW(TAG, "Failed to sync default peer to filter_mac=" MACSTR, MAC2STR(mac));
    }
    return ret;
}

esp_err_t esp_radar_new_peer(const uint8_t mac[6], esp_radar_handle_t *out_handle)
{
    if (!mac || !out_handle) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_ctx.init_flag) {
        return ESP_ERR_INVALID_STATE;
    }

    esp_radar_peer_t *peer = esp_radar_peer_create(mac);
    if (!peer) {
        return ESP_ERR_NO_MEM;
    }

    esp_err_t ret = esp_radar_peer_register(peer);
    if (ret != ESP_OK) {
        RADAR_FREE(peer);
        return ret;
    }

    if (s_ctx.run_flag) {
        ret = esp_radar_peer_ensure_ring_buffers(peer);
        if (ret != ESP_OK) {
            esp_radar_peer_unregister(peer);
            RADAR_FREE(peer);
            return ret;
        }
    }

    *out_handle = (esp_radar_handle_t)peer;
    return ESP_OK;
}

esp_err_t esp_radar_del_peer(esp_radar_handle_t handle)
{
    esp_radar_peer_t *peer = (esp_radar_peer_t *)handle;
    if (!peer) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_ctx.run_flag) {
        return ESP_ERR_INVALID_STATE;
    }
    if (peer == s_ctx.default_peer) {
        return ESP_ERR_INVALID_STATE;
    }

    esp_radar_peer_unregister(peer);
    esp_radar_peer_free_motion_dec_buffers(peer);
    esp_radar_peer_free_ring_buffers(peer);
    if (peer->cal) {
        radar_calibrate_free_entries(peer->cal);
        RADAR_FREE(peer->cal);
    }
    RADAR_FREE(peer);
    return ESP_OK;
}
static esp_err_t esp_radar_extract_rx_ctrl_info(const wifi_pkt_rx_ctrl_t *rx_ctrl, esp_radar_rx_ctrl_info_t *info)
{
    if (!rx_ctrl || !info) {
        return ESP_ERR_INVALID_ARG;
    }

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
    info->rssi = rx_ctrl->rssi;
    info->rate = rx_ctrl->rate;
    info->rx_format = rx_ctrl->cur_bb_format;
    esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &info->agc_gain, &info->fft_gain);
    info->timestamp = rx_ctrl->timestamp;
    info->noise_floor = rx_ctrl->noise_floor;
    info->channel = rx_ctrl->channel;
    info->secondary_channel = rx_ctrl->second;
    switch (rx_ctrl->cur_bb_format) {
    case RX_FORMAT_11B:
    case RX_FORMAT_11G:  // RX_FORMAT_11A has the same value as RX_FORMAT_11G
        info->signal_mode = WIFI_SIGNAL_MODE_NON_HT;
        info->mcs = ((esp_wifi_htsig_t *)&rx_ctrl->he_siga1)->mcs;
        info->cwb = ((esp_wifi_htsig_t *)&rx_ctrl->he_siga1)->cbw;
        info->stbc = ((esp_wifi_htsig_t *)&rx_ctrl->he_siga1)->stbc;
        break;
    case RX_FORMAT_HT:
        info->signal_mode = WIFI_SIGNAL_MODE_HT;
        info->mcs = ((esp_wifi_htsig_t *)&rx_ctrl->he_siga1)->mcs;
        info->cwb = ((esp_wifi_htsig_t *)&rx_ctrl->he_siga1)->cbw;
        info->stbc = ((esp_wifi_htsig_t *)&rx_ctrl->he_siga1)->stbc;
        break;
    case RX_FORMAT_VHT:
        info->signal_mode = WIFI_SIGNAL_MODE_HT;
        info->mcs = ((esp_wifi_vht_siga1_t *)&rx_ctrl->he_siga1)->su_mcs;
        info->cwb = ((esp_wifi_vht_siga1_t *)&rx_ctrl->he_siga1)->cbw;
        info->stbc = ((esp_wifi_vht_siga1_t *)&rx_ctrl->he_siga1)->stbc;
        break;
    case RX_FORMAT_HE_SU:
        info->signal_mode = WIFI_SIGNAL_MODE_HE;
        info->mcs = ((esp_wifi_su_siga1_t *)&rx_ctrl->he_siga1)->he_mcs;
        info->cwb = ((esp_wifi_su_siga1_t *)&rx_ctrl->he_siga1)->bw;
        info->stbc = ((esp_wifi_su_siga2_t *)&rx_ctrl->he_siga2)->stbc;
        break;
    case RX_FORMAT_HE_MU:
        info->signal_mode = WIFI_SIGNAL_MODE_HE;
        info->mcs = ((esp_wifi_mu_siga1_t *)&rx_ctrl->he_siga1)->sigb_mcs;
        info->cwb = ((esp_wifi_mu_siga1_t *)&rx_ctrl->he_siga1)->bw;
        info->stbc = ((esp_wifi_mu_siga2_t *)&rx_ctrl->he_siga2)->stbc;
        break;
    default:
        return ESP_FAIL;
    }

#elif CONFIG_IDF_TARGET_ESP32 || CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32C3
    info->rssi = rx_ctrl->rssi;
    info->rate = rx_ctrl->rate;
    info->rx_format = rx_ctrl->sig_mode;
    if (rx_ctrl->sig_mode == RX_FORMAT_NON_HT) {
        info->signal_mode = WIFI_SIGNAL_MODE_NON_HT;
    } else {
        info->signal_mode = WIFI_SIGNAL_MODE_HT;
    }
#if CONFIG_IDF_TARGET_ESP32 || CONFIG_IDF_TARGET_ESP32S2
    info->agc_gain = 0;
    info->fft_gain = 0;
#else
    esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &info->agc_gain, &info->fft_gain);
#endif
    info->timestamp = rx_ctrl->timestamp;
    info->noise_floor = rx_ctrl->noise_floor;
    info->channel = rx_ctrl->channel;
    info->secondary_channel = rx_ctrl->secondary_channel;
    info->mcs = rx_ctrl->mcs;
    info->cwb = rx_ctrl->cwb;
    info->stbc = rx_ctrl->stbc;
#endif
    return ESP_OK;
}

static bool esp_radar_mac_is_wildcard(const uint8_t mac[6])
{
    return (mac && ADDR_IS_FULL(mac));
}

static esp_radar_peer_t *esp_radar_peer_find_exact_nolock(const uint8_t mac[6])
{
    if (!mac) {
        return NULL;
    }
    for (size_t i = 0; i < ESP_RADAR_MAX_PEERS; ++i) {
        esp_radar_peer_t *p = s_ctx.peers[i];
        if (p && !p->is_wildcard && memcmp(p->mac, mac, 6) == 0) {
            return p;
        }
    }
    return NULL;
}

static esp_radar_peer_t *esp_radar_peer_find_wildcard_nolock(void)
{
    for (size_t i = 0; i < ESP_RADAR_MAX_PEERS; ++i) {
        esp_radar_peer_t *p = s_ctx.peers[i];
        if (p && p->is_wildcard) {
            return p;
        }
    }
    return NULL;
}

static esp_radar_peer_t *esp_radar_select_peer(const wifi_csi_info_t *info)
{
    if (!info) {
        return NULL;
    }

    /* Legacy compatibility:
     * If user only uses esp_radar_init() (no additional peers created),
     * keep the original filtering behavior based on esp_radar_mac_addr_filter().
     */
    if (s_ctx.peer_count == 1 && s_ctx.default_peer) {
        if (esp_radar_mac_addr_filter((wifi_csi_info_t *)info) != ESP_OK) {
            return NULL;
        }
        return s_ctx.default_peer;
    }

    /* Destination MAC filtering (shared config) */
    if (s_ctx.csi_config.filter_dmac_flag && memcmp(info->dmac, s_ctx.csi_config.filter_dmac, 6) != 0) {
        ESP_LOGD("esp_radar_filter", "Dest MAC mismatch - dmac: " MACSTR ", filter_dmac: " MACSTR,
                 MAC2STR(info->dmac), MAC2STR(s_ctx.csi_config.filter_dmac));
        return NULL;
    }

#if WIFI_CSI_SEND_NULL_DATA_ENABLE
    /* An empty filter is only treated as a NULL-data payload filter when ACK
     * capture is disabled.  With dump_ack_en enabled, ACK metadata on ESP32-S3
     * can have payload lengths other than 14 and must remain eligible. */
    if (ADDR_IS_EMPTY(s_ctx.csi_config.filter_mac) &&
            !s_ctx.csi_config.dump_ack_en && info->payload_len != 14) {
        ESP_LOGD("esp_radar_filter", "Not null data packet - payload_len: %d", info->payload_len);
        return NULL;
    }
#endif

    esp_radar_peer_t *peer = NULL;
    portENTER_CRITICAL(&s_peer_mux);
    peer = esp_radar_peer_find_exact_nolock(info->mac);
    if (!peer) {
        peer = esp_radar_peer_find_wildcard_nolock();
    }
    portEXIT_CRITICAL(&s_peer_mux);
    return peer;
}

static esp_err_t esp_radar_mac_addr_filter(wifi_csi_info_t *info)
{
    static const char *TAG_FILTER = "esp_radar_filter";

    if ((!ADDR_IS_FULL(s_ctx.csi_config.filter_mac) &&  memcmp(info->mac, s_ctx.csi_config.filter_mac, 6) != 0)
#if WIFI_CSI_SEND_NULL_DATA_ENABLE
            || (ADDR_IS_EMPTY(s_ctx.csi_config.filter_mac) &&
                !s_ctx.csi_config.dump_ack_en && info->payload_len != 14)
#endif
       ) {
        if ((ADDR_IS_EMPTY(s_ctx.csi_config.filter_mac) &&
                !s_ctx.csi_config.dump_ack_en && info->payload_len != 14)) {
            ESP_LOGD(TAG_FILTER, "Not null data packet - payload_len: %d", info->payload_len);
            return ESP_FAIL;
        }
        ESP_LOGD(TAG_FILTER, "Source MAC mismatch - mac: " MACSTR ", filter_mac: " MACSTR,  MAC2STR(info->mac), MAC2STR(s_ctx.csi_config.filter_mac));
        return ESP_FAIL;
    }

    if (s_ctx.csi_config.filter_dmac_flag && memcmp(info->dmac, s_ctx.csi_config.filter_dmac, 6) != 0) {
        ESP_LOGD(TAG_FILTER, "Dest MAC mismatch - dmac: " MACSTR ", filter_dmac: " MACSTR, MAC2STR(info->dmac), MAC2STR(s_ctx.csi_config.filter_dmac));
        return ESP_FAIL;
    }

    return ESP_OK;
}

static void copy_subcarrier_data(wifi_csi_filtered_info_t *out_filtered_info, const int8_t *src_buf, const sub_carrier_range_t *range_array, uint16_t target_bytes, uint16_t *field_len)
{
    uint8_t i = 0;
    while (*field_len < target_bytes) {
        uint16_t size = range_array[i].stop - range_array[i].start;

        if (s_ctx.lltf_bit_mode) {
            int16_t *dest = (int16_t *)(out_filtered_info->valid_data + out_filtered_info->valid_len);
            const uint16_t *src = (const uint16_t *)(src_buf + range_array[i].start);
            for (uint16_t j = 0; j < size / 2; j++) {
                dest[j] = ((int16_t)(src[j] << 4)) >> 4;
            }
        } else {
            memcpy(out_filtered_info->valid_data + out_filtered_info->valid_len, src_buf + range_array[i].start, size);
        }

        *field_len += size;
        out_filtered_info->valid_len += size;
        i++;
    }
}

static esp_err_t esp_radar_rebuild_csi_data(const wifi_csi_info_t *info, const esp_radar_rx_ctrl_info_t *rx_ctrl_info, wifi_csi_filtered_info_t **out_filtered_info)
{
    static const char *TAG_FILTER_CSI = "esp_radar_csi_data_rebuild";
    if (!info || !rx_ctrl_info || !out_filtered_info) {
        return ESP_ERR_INVALID_ARG;
    }

    *out_filtered_info = NULL;

    for (size_t i = 0; i < sub_carrier_table_size; ++i) {
        bool int16_mismatch = (s_ctx.lltf_bit_mode && sub_carrier_table[i].total_bytes != info->len);

        bool int8_mismatch = (!s_ctx.lltf_bit_mode &&
                              (rx_ctrl_info->signal_mode != sub_carrier_table[i].signal_mode ||
                               rx_ctrl_info->cwb != sub_carrier_table[i].channel_bandwidth ||
                               rx_ctrl_info->stbc != sub_carrier_table[i].stbc
#if CONFIG_IDF_TARGET_ESP32 || CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32C3
                               || rx_ctrl_info->secondary_channel != sub_carrier_table[i].second
#endif
                              ));

        if (int16_mismatch || int8_mismatch) {
            continue;
        }

        const csi_sub_carrier_table_t *sub_carrier_index =  sub_carrier_table + i;
        *out_filtered_info = RADAR_MALLOC_RETRY(sizeof(wifi_csi_filtered_info_t) + sub_carrier_index->valid_bytes);
        memset(*out_filtered_info, 0, sizeof(wifi_csi_filtered_info_t) + sub_carrier_index->valid_bytes);

        wifi_csi_filtered_info_t *filtered_info = *out_filtered_info;

        filtered_info->rx_ctrl_info = *rx_ctrl_info;
        filtered_info->rx_seq = info->rx_seq;
        filtered_info->data_type = s_ctx.lltf_bit_mode ? WIFI_CSI_DATA_TYPE_INT16 : WIFI_CSI_DATA_TYPE_INT8;
        filtered_info->raw_data = info->buf;
        filtered_info->raw_len  = info->len;
        memcpy(filtered_info->mac, info->mac, 6);
        memcpy(filtered_info->dmac, info->dmac, 6);

        if (sub_carrier_index->lltf_bytes > 0) {
            copy_subcarrier_data(filtered_info, info->buf, sub_carrier_index->lltf,
                                 sub_carrier_index->lltf_bytes, &filtered_info->valid_lltf_len);
        }

        if (sub_carrier_index->ht_ltf_bytes > 0) {
            copy_subcarrier_data(filtered_info, info->buf, sub_carrier_index->ht_ltf,
                                 sub_carrier_index->ht_ltf_bytes, &filtered_info->valid_ht_ltf_len);
        }

        if (sub_carrier_index->stbc_ht_ltf_bytes > 0) {
            copy_subcarrier_data(filtered_info, info->buf, sub_carrier_index->stbc_ht_ltf,
                                 sub_carrier_index->stbc_ht_ltf_bytes, &filtered_info->valid_stbc_ht_ltf_len);
        }
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
        if (sub_carrier_index->he_ltf_bytes > 0) {
            copy_subcarrier_data(filtered_info, info->buf, sub_carrier_index->he_ltf,
                                 sub_carrier_index->he_ltf_bytes, &filtered_info->valid_he_ltf_len);
        }
        if (sub_carrier_index->stbc_he_ltf_bytes > 0) {
            copy_subcarrier_data(filtered_info, info->buf, sub_carrier_index->stbc_he_ltf,
                                 sub_carrier_index->stbc_he_ltf_bytes, &filtered_info->valid_stbc_he_ltf_len);
        }
#endif

        ESP_LOGD(TAG_FILTER_CSI, "raw_len: %d, valid_len: %d, valid_lltf_len: %d, valid_ht_ltf_len: %d, valid_stbc_ht_ltf_len: %d"
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
                 ", valid_he_ltf_len: %d, valid_stbc_he_ltf_len: %d"
#endif
                 , filtered_info->raw_len, filtered_info->valid_len, filtered_info->valid_lltf_len, filtered_info->valid_ht_ltf_len, filtered_info->valid_stbc_ht_ltf_len
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
                 , filtered_info->valid_he_ltf_len, filtered_info->valid_stbc_he_ltf_len
#endif
                );
        return ESP_OK;
    }

    ESP_LOGW(TAG_FILTER_CSI, "value fail, len: %d, secondary_channel: %d, sig_mode: %d, cwb: %d, stbc: %d",
             info->len, rx_ctrl_info->secondary_channel, rx_ctrl_info->rx_format, rx_ctrl_info->cwb, rx_ctrl_info->stbc);

    return ESP_FAIL;
}

static void esp_radar_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    static const char *TAG_CB = "esp_radar_csi_rx_cb";
    if (!info || !info->buf) {
        ESP_LOGE(TAG_CB, "<%s> esp_radar_csi_rx_cb", esp_err_to_name(ESP_ERR_INVALID_ARG));
        return;
    }

    /* Select peer to dispatch this packet */
    esp_radar_peer_t *peer = esp_radar_select_peer(info);
    if (!peer) {
        return;
    }

    esp_radar_rx_ctrl_info_t rx_ctrl_info;
    esp_radar_extract_rx_ctrl_info(&info->rx_ctrl, &rx_ctrl_info);
    ESP_LOGD(TAG_CB, "timestamp: %ld, mac: " MACSTR", channel: %d, secondary_channel: %d, rssi: %d, rx_format: %d, cwb: %d, rate: %d, mcs: %d, stbc: %d, noise_floor: %d, len: %d, agc_gain: %d, fft_gain: %d",
             rx_ctrl_info.timestamp, MAC2STR(info->mac), rx_ctrl_info.channel, rx_ctrl_info.secondary_channel, rx_ctrl_info.rssi, rx_ctrl_info.rx_format, rx_ctrl_info.cwb, rx_ctrl_info.rate, rx_ctrl_info.mcs,
             rx_ctrl_info.stbc, rx_ctrl_info.noise_floor, info->len, rx_ctrl_info.agc_gain, rx_ctrl_info.fft_gain);

    wifi_csi_filtered_info_t *filtered_info = NULL;
    ESP_RETURN_VOID_ON_ERROR(esp_radar_rebuild_csi_data(info, &rx_ctrl_info, &filtered_info), TAG_CB, "Failed to filter CSI data");

    filtered_info->seq_id = peer->csi_seq++;
    filtered_info->info = info;

    float compensate_gain = 0;
#if WIFI_CSI_PHY_GAIN_ENABLE
    esp_csi_gain_ctrl_record_rx_gain(rx_ctrl_info.agc_gain, rx_ctrl_info.fft_gain);
    if (s_ctx.csi_config.csi_compensate_en) {
        bool samples_are_16bit = (filtered_info->data_type == WIFI_CSI_DATA_TYPE_INT16);
        esp_csi_gain_ctrl_compensate_rx_gain(filtered_info->valid_data, filtered_info->valid_len, samples_are_16bit,
                                             &compensate_gain, rx_ctrl_info.agc_gain, rx_ctrl_info.fft_gain);
    }
#endif
    filtered_info->rx_gain_compensation = compensate_gain;
    if (s_ctx.csi_config.csi_filtered_cb) {
        s_ctx.csi_config.csi_filtered_cb(s_ctx.csi_config.csi_filtered_cb_ctx, filtered_info);
    }
    if (!s_ctx.run_flag) {
        static bool run_flag_warned = false;
        if (!run_flag_warned) {
            ESP_LOGW(TAG_CB, "esp_radar not running, CSI data dropped");
        }
        RADAR_FREE(filtered_info);
    } else {
        csi_info_msg_t msg = {
            .peer = peer,
            .filtered_info = filtered_info,
        };
        if (!s_ctx.csi_info_queue || xQueueSend(s_ctx.csi_info_queue, &msg, 0) == pdFALSE) {
            ESP_LOGW(TAG_CB, "Failed to send CSI data to queue, data dropped");
            RADAR_FREE(filtered_info);
        }
    }
}
#define my_hypotf(a, b) sqrtf((a) * (a) + (b) * (b))

static esp_err_t esp_radar_get_ltf_data(const wifi_csi_filtered_info_t *filtered_info, void **data_ptr, uint16_t *data_len)
{
    if (!filtered_info || !data_ptr || !data_len) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t *ptr = (uint8_t *)filtered_info->valid_data;
    uint16_t offset = 0;

    switch (s_ctx.dec_config.ltf_type) {
    case RADAR_LTF_TYPE_LLTF:
        *data_ptr = ptr;
        *data_len = filtered_info->valid_lltf_len;
        break;

    case RADAR_LTF_TYPE_HTLTF:
        offset = filtered_info->valid_lltf_len;
        *data_ptr = ptr + offset;
        *data_len = filtered_info->valid_ht_ltf_len;
        break;

    case RADAR_LTF_TYPE_STBC_HTLTF:
        offset = filtered_info->valid_lltf_len + filtered_info->valid_ht_ltf_len;
        *data_ptr = ptr + offset;
        *data_len = filtered_info->valid_stbc_ht_ltf_len;
        break;

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
    case RADAR_LTF_TYPE_HELTF:
        offset = filtered_info->valid_lltf_len + filtered_info->valid_ht_ltf_len +
                 filtered_info->valid_stbc_ht_ltf_len;
        *data_ptr = ptr + offset;
        *data_len = filtered_info->valid_he_ltf_len;
        break;

    case RADAR_LTF_TYPE_STBC_HELTF:
        offset = filtered_info->valid_lltf_len + filtered_info->valid_ht_ltf_len +
                 filtered_info->valid_stbc_ht_ltf_len + filtered_info->valid_he_ltf_len;
        *data_ptr = ptr + offset;
        *data_len = filtered_info->valid_stbc_he_ltf_len;
        break;
#endif

    default:
        ESP_LOGE(TAG, "Unsupported LTF type: %d", s_ctx.dec_config.ltf_type);
        return ESP_ERR_NOT_SUPPORTED;
    }

    if (*data_len == 0) {
        ESP_LOGW(TAG, "LTF type %d has no data", s_ctx.dec_config.ltf_type);
        return ESP_ERR_NOT_FOUND;
    }

    return ESP_OK;
}

static esp_err_t csi_outlier_filter_process(esp_radar_peer_t *peer, uint16_t subcarrier_len)
{
    if (!peer) {
        return ESP_ERR_INVALID_ARG;
    }
    const uint8_t threshold = s_ctx.dec_config.outliers_threshold;
    uint32_t curr_seq = peer->window_ctx.next_seq;
    const uint8_t max_streak = 3;
    uint8_t *outlier_streak = &peer->outlier_streak;
    if (curr_seq < 3) {
        *outlier_streak = 0;
        return ESP_OK;
    }

    float *hist0 = peer->csi_data_buff->amplitude[(curr_seq - 3) % peer->window_ctx.buff_size];
    float *hist1 = peer->csi_data_buff->amplitude[(curr_seq - 2) % peer->window_ctx.buff_size];
    float *hist2 = peer->csi_data_buff->amplitude[(curr_seq - 1) % peer->window_ctx.buff_size];
    float *curr  = peer->csi_data_buff->amplitude[curr_seq % peer->window_ctx.buff_size];

    uint32_t outliers_count = 0;

    for (int i = 0; i < subcarrier_len; ++i) {
        float ref = (hist0[i] + hist1[i] + hist2[i]) / 3.0f;
        float diff = curr[i] - ref;

        if (diff > threshold || diff < -threshold) {
            outliers_count++;
        }
    }
    bool is_outlier_frame = (outliers_count >= subcarrier_len / 2);

    if (!is_outlier_frame) {
        *outlier_streak = 0;
        return ESP_OK;
    }
    (*outlier_streak)++;

    if (*outlier_streak >= max_streak) {
        ESP_LOGW(TAG, "Consecutive outliers (%u), accept as new baseline, seq=%lu", *outlier_streak, curr_seq);
        *outlier_streak = 0;
        return ESP_OK;
    }
    ESP_LOGD(TAG, "Soft-updated outlier frame: %lu/%d at seq=%lu", outliers_count, subcarrier_len, curr_seq);

    return ESP_OK;
}

static esp_err_t csi_window_update(esp_radar_peer_t *peer, csi_data_buff_index_t *buff_index)
{
    if (!peer || !peer->csi_data_buff || peer->window_ctx.buff_size == 0) {
        return ESP_FAIL;
    }

    buff_index->begin  = peer->window_ctx.window_start_seq % peer->window_ctx.buff_size;
    buff_index->end    = peer->window_ctx.next_seq % peer->window_ctx.buff_size;
    buff_index->window = peer->window_ctx.next_seq - peer->window_ctx.window_start_seq;

    int32_t spent_time = peer->csi_data_buff->timestamp[buff_index->end] - peer->csi_data_buff->timestamp[buff_index->begin];
    int32_t time_tamp  = peer->csi_data_buff->timestamp[buff_index->end] - (int32_t)(peer->window_ctx.last_timestamp);
    int32_t strict_ts_limit = (int32_t)(s_ctx.dec_config.csi_handle_time / 2);
    int32_t ts_limit = peer->window_ctx.ts_delta_relaxed ? (int32_t)s_ctx.dec_config.csi_handle_time : strict_ts_limit;

    esp_err_t ret = ESP_FAIL;

    if (peer->window_ctx.ts_delta_relaxed && time_tamp >= 0 && time_tamp <= strict_ts_limit) {
        peer->window_ctx.ts_delta_relaxed = false;
        ESP_LOGI(TAG, "Timestamp delta recovered, restore strict threshold: ts_delta=%d, strict_limit=%d",
                 time_tamp, strict_ts_limit);
    }

    /* The first ring-buffer sample has no prior timestamp. Initialize the
     * baseline without reporting a false discontinuity after mode changes. */
    if (peer->window_ctx.next_seq == 0) {
        peer->window_ctx.window_start_seq = 0;
        goto UPDATE_STATE;
    }

    if (time_tamp < 0 || time_tamp > ts_limit) {
        ESP_LOGI(TAG, "Timestamp delta out of range, clear window: ts_delta=%d, ts_limit=%d, spent_time=%d, csi_handle_time=%d, end=%d, last=%d, window=%d, handle_window=%d",
                 time_tamp, ts_limit, spent_time, s_ctx.dec_config.csi_handle_time, peer->csi_data_buff->timestamp[buff_index->end], peer->window_ctx.last_timestamp,
                 buff_index->window, peer->window_ctx.handle_window);

        if (time_tamp > strict_ts_limit) {
            peer->window_ctx.ts_delta_relaxed = true;
        }
        peer->window_ctx.window_start_seq = peer->window_ctx.next_seq;
        goto UPDATE_STATE;
    }

    if (spent_time >= (int32_t)(s_ctx.dec_config.csi_handle_time * 2) || buff_index->window >= peer->window_ctx.handle_window / 2) {
        if (buff_index->window < peer->window_ctx.handle_window / 3) {
            peer->window_ctx.window_start_seq = peer->window_ctx.next_seq;
            ESP_LOGI(TAG, "Flush window but drop: too small, window=%d, spent_time=%d, csi_handle_time=%d, handle_window=%d",
                     buff_index->window, spent_time, s_ctx.dec_config.csi_handle_time, peer->window_ctx.handle_window);
            goto UPDATE_STATE;
        }
        ESP_LOGD(TAG, "Flush half window ok. window=%d, ts_delta=%d, spent_time=%d, csi_handle_time=%d, handle_window=%d, begin=%d, end=%d",
                 buff_index->window, time_tamp, spent_time, s_ctx.dec_config.csi_handle_time, peer->window_ctx.handle_window, buff_index->begin, buff_index->end);
        ret = ESP_OK;
        peer->window_ctx.window_start_seq += buff_index->window / 2;
    }

UPDATE_STATE:
    peer->window_ctx.last_timestamp = peer->csi_data_buff->timestamp[buff_index->end];
    peer->window_ctx.next_seq++;
    return ret;
}

static void csi_prepare_amplitude(esp_radar_peer_t *peer, uint16_t subcarrier_len)
{
    if (!peer || !peer->csi_data_buff) {
        return;
    }
    size_t pointer_bytes = (size_t)peer->window_ctx.buff_size * sizeof(float *);
    size_t data_bytes    = (size_t)peer->window_ctx.buff_size * subcarrier_len * sizeof(float);
    size_t total_bytes   = pointer_bytes + data_bytes;
    uint8_t *amplitude_block = RADAR_MALLOC_RETRY(total_bytes);
    memset(amplitude_block, 0, total_bytes);

    peer->csi_data_buff->amplitude = (float **)amplitude_block;
    float *data_base = (float *)(amplitude_block + pointer_bytes);
    for (uint32_t i = 0; i < peer->window_ctx.buff_size; i++) {
        peer->csi_data_buff->amplitude[i] = data_base + (i * subcarrier_len);
    }
}

static inline float csi_transform_amplitude(float amplitude)
{
    return s_ctx.dec_config.amplitude_log_enabled ? log10f(amplitude) : amplitude;
}

static void csi_write_frame_to_ring(esp_radar_peer_t *peer, const void *ltf_data, uint16_t subcarrier_len, wifi_csi_filtered_info_t *filtered_info)
{
    if (!peer || !peer->csi_data_buff || !peer->csi_data_buff->amplitude ||
            !peer->csi_data_buff->seq_id || !peer->csi_data_buff->timestamp) {
        return;
    }

    uint32_t write_index = peer->window_ctx.next_seq % peer->window_ctx.buff_size;

    peer->csi_data_buff->seq_id[write_index] = filtered_info->seq_id;
    peer->csi_data_buff->timestamp[write_index] = filtered_info->rx_ctrl_info.timestamp / 1000;

    float *csi_data = peer->csi_data_buff->amplitude[write_index];
    const uint8_t step = s_ctx.dec_config.sub_carrier_step_size;
    const uint8_t *data = (const uint8_t *)ltf_data;

    for (int i = 0; i < subcarrier_len; i++) {
        if (filtered_info->data_type == WIFI_CSI_DATA_TYPE_INT16) {
            /* Extract 12-bit signed values from packed format:
             * Data format: [low_byte_0, high_byte_0, low_byte_1, high_byte_1, ...]
             * Each 12-bit value is sign-extended: left shift 4 bits then arithmetic right shift 4 bits
             */
            int16_t imag = ((int16_t)(((int16_t)data[i * step * 4 + 1]) << 12) >> 4 | (uint8_t)data[i * step * 4]) >> 4;
            int16_t real = ((int16_t)(((int16_t)data[i * step * 4 + 3]) << 12) >> 4 | (uint8_t)data[i * step * 4 + 2]) >> 4;
            csi_data[i] = csi_transform_amplitude(my_hypotf(real, imag));
        } else {
            csi_data[i] = csi_transform_amplitude(my_hypotf(((const int8_t *)ltf_data)[i * step * 2],
                                                            ((const int8_t *)ltf_data)[i * step * 2 + 1]));
        }
    }
}

static void esp_radar_peer_free_motion_dec_buffers(esp_radar_peer_t *peer)
{
    if (!peer) {
        return;
    }
    if (!peer->motion_dec_buff) {
        peer->motion_dec_buff_allocated = false;
        peer->motion_dec_buff_num = 0;
        peer->motion_dec_capacity = 0;
        return;
    }
    uint32_t cap = peer->motion_dec_capacity;
    for (uint32_t i = 0; i < cap; ++i) {
        if (peer->motion_dec_buff[i]) {
            RADAR_FREE(peer->motion_dec_buff[i]);
            peer->motion_dec_buff[i] = NULL;
        }
    }
    RADAR_FREE(peer->motion_dec_buff);
    peer->motion_dec_buff = NULL;
    peer->motion_dec_buff_allocated = false;
    peer->motion_dec_buff_num = 0;
    peer->motion_dec_capacity = 0;
}

static void esp_radar_peer_free_ring_buffers(esp_radar_peer_t *peer)
{
    if (!peer || !peer->csi_data_buff) {
        return;
    }
    RADAR_FREE(peer->csi_data_buff->amplitude);
    RADAR_FREE(peer->csi_data_buff->timestamp);
    RADAR_FREE(peer->csi_data_buff->seq_id);
    RADAR_FREE(peer->csi_data_buff);
    peer->csi_data_buff = NULL;
    peer->subcarrier_len = 0;
    memset(&peer->window_ctx, 0, sizeof(peer->window_ctx));
    peer->csi_seq = 0;
    peer->outlier_streak = 0;
}

static esp_err_t esp_radar_peer_ensure_ring_buffers(esp_radar_peer_t *peer)
{
    if (!peer) {
        return ESP_ERR_INVALID_ARG;
    }
    if (peer->csi_data_buff) {
        return ESP_OK;
    }
    if (s_ctx.buff_size == 0 || s_ctx.handle_window == 0) {
        return ESP_ERR_INVALID_STATE;
    }

    peer->csi_data_buff = RADAR_MALLOC_RETRY(sizeof(csi_data_buff_t));
    memset(peer->csi_data_buff, 0, sizeof(csi_data_buff_t));

    peer->window_ctx.handle_window     = s_ctx.handle_window;
    peer->window_ctx.buff_size         = s_ctx.buff_size;
    peer->window_ctx.window_start_seq  = 0;
    peer->window_ctx.next_seq          = 0;
    peer->window_ctx.last_timestamp    = 0;
    peer->window_ctx.ts_delta_relaxed  = false;

    peer->csi_data_buff->amplitude = NULL;
    peer->csi_data_buff->timestamp = RADAR_MALLOC_RETRY(peer->window_ctx.buff_size * sizeof(uint32_t));
    memset(peer->csi_data_buff->timestamp, 0, peer->window_ctx.buff_size * sizeof(uint32_t));
    peer->csi_data_buff->seq_id = RADAR_MALLOC_RETRY(peer->window_ctx.buff_size * sizeof(uint32_t));
    memset(peer->csi_data_buff->seq_id, 0, peer->window_ctx.buff_size * sizeof(uint32_t));

    peer->subcarrier_len = 0;
    peer->csi_seq = 0;
    peer->outlier_streak = 0;
    esp_radar_peer_free_motion_dec_buffers(peer);
    return ESP_OK;
}

static void csi_preprocessing_task(void *arg)
{
    csi_info_msg_t msg = {0};
    wifi_csi_filtered_info_t *filtered_info = NULL;
    esp_radar_peer_t *peer = NULL;

    while (xQueueReceive(s_ctx.csi_info_queue, &msg, portMAX_DELAY)) {
        peer = msg.peer;
        filtered_info = msg.filtered_info;
        if (!s_ctx.run_flag) {
            if (filtered_info) {
                RADAR_FREE(filtered_info);
            }
            break;
        }
        if (!peer || !filtered_info) {
            continue;
        }
        if (esp_radar_peer_ensure_ring_buffers(peer) != ESP_OK) {
            ESP_LOGW(TAG, "Peer ring buffer not ready (peer=" MACSTR "), frame dropped", MAC2STR(peer->mac));
            goto FREE_MEM;
        }
        void *ltf_data = NULL;
        uint16_t ltf_len = 0;
        uint16_t subcarrier_len = 0;
        if (esp_radar_get_ltf_data(filtered_info, &ltf_data, &ltf_len) != ESP_OK) {
            goto FREE_MEM;
        }

        uint8_t component_bytes = (filtered_info->data_type == WIFI_CSI_DATA_TYPE_INT16) ? sizeof(int16_t) : sizeof(int8_t);
        subcarrier_len = (uint16_t)((ltf_len / (2U * component_bytes)) / s_ctx.dec_config.sub_carrier_step_size);
        if (peer->subcarrier_len == 0) {
            peer->subcarrier_len = subcarrier_len;
            csi_prepare_amplitude(peer, subcarrier_len);
            ESP_LOGI(TAG, "First frame detected (peer=" MACSTR "): type=%d, LTF length=%d, subcarrier length=%d, step size=%d, allocated buffer: %lu x %d",
                     MAC2STR(peer->mac), s_ctx.dec_config.ltf_type, ltf_len, subcarrier_len, s_ctx.dec_config.sub_carrier_step_size, peer->window_ctx.buff_size, subcarrier_len);
        } else if (subcarrier_len != peer->subcarrier_len) {
            ESP_LOGE(TAG, "Frame length mismatch detected (peer=" MACSTR ")! Expected type=%d, subcarrier_len=%d, got=%d (LTF len=%d). Discarding frame.",
                     MAC2STR(peer->mac), s_ctx.dec_config.ltf_type, peer->subcarrier_len, subcarrier_len, ltf_len);
            goto FREE_MEM;
        }

        csi_write_frame_to_ring(peer, ltf_data, subcarrier_len, filtered_info);
        if (s_ctx.dec_config.outliers_threshold > 0) {
            if (csi_outlier_filter_process(peer, subcarrier_len) != ESP_OK) {
                goto FREE_MEM;
            }
        }

        csi_data_buff_index_t buff_index = {0};
        if (csi_window_update(peer, &buff_index) == ESP_OK) {
            csi_window_msg_t win_msg = {
                .peer = peer,
                .index = buff_index,
            };
            if (!s_ctx.csi_data_queue || xQueueSend(s_ctx.csi_data_queue, &win_msg, portMAX_DELAY) == pdFALSE) {
                ESP_LOGW(TAG, "The buffer is full");
            }
        }

FREE_MEM:
        RADAR_FREE(filtered_info);
    }

    ESP_LOGI(TAG, "csi_preprocessing_task stopped for radar reconfiguration");
    xEventGroupSetBits(s_ctx.task_exit_group, CSI_PREPROCESSING_EXIT_BIT);
    vTaskDelete(NULL);
}

static void radar_calibrate_free_entries(radar_calibrate_t *cal)
{
    if (!cal) {
        return;
    }
    for (int i = 0; i < CSI_WANDER_NUM; ++i) {
        if (cal->data[i]) {
            RADAR_FREE(cal->data[i]);
            cal->data[i] = NULL;
        }
    }
}

static void radar_calibrate_reset_stats(radar_calibrate_t *cal)
{
    if (!cal) {
        return;
    }

    cal->buff_size = 0;
    cal->none_wander_sum = 0.0f;
    cal->none_wander_count = 0.0f;
    cal->none_wander = 0.0f;
    cal->static_wander = 0.0f;
    cal->subcarrier_len = 0;
    cal->last_action = ESP_RADAR_TRAIN_ACTION_IDLE;
}

static esp_radar_train_status_t radar_train_status_to_public(radar_calibrate_status_t status)
{
    switch (status) {
    case RADAR_CALIBRATE_PROGRESS:
        return ESP_RADAR_TRAIN_STATUS_PROGRESS;
    case RADAR_CALIBRATE_COMPLETE:
        return ESP_RADAR_TRAIN_STATUS_COMPLETE;
    case RADAR_CALIBRATE_NO:
    default:
        return ESP_RADAR_TRAIN_STATUS_NONE;
    }
}

esp_err_t esp_radar_train_start_ex(esp_radar_handle_t handle)
{
    esp_radar_peer_t *peer = (esp_radar_peer_t *)handle;
    if (!peer) {
        return ESP_ERR_INVALID_ARG;
    }

    if (!peer->cal) {
        peer->cal = RADAR_MALLOC_RETRY(sizeof(radar_calibrate_t));
        if (!peer->cal) {
            return ESP_ERR_NO_MEM;
        }
        memset(peer->cal, 0, sizeof(radar_calibrate_t));
        peer->cal->data_num = 0;
    }

    radar_calibrate_t *cal = peer->cal;

    radar_calibrate_reset_stats(cal);
    /* Start a new training session: reset sample counter while keeping allocated buffers for reuse */
    cal->data_num = 0;
    cal->calibrate_status = RADAR_CALIBRATE_PROGRESS;
    cal->last_action = ESP_RADAR_TRAIN_ACTION_IDLE;
    peer->waveform_wander_last = 1.0f;
    peer->motion_dec_buff_num = 0;
    ESP_LOGI(TAG_TRAIN, "esp_radar_train_start (peer=" MACSTR ")", MAC2STR(peer->mac));
    return ESP_OK;
}

esp_err_t esp_radar_train_remove_ex(esp_radar_handle_t handle)
{
    esp_radar_peer_t *peer = (esp_radar_peer_t *)handle;
    if (!peer) {
        return ESP_ERR_INVALID_ARG;
    }

    if (!peer->cal) {
        return ESP_OK;
    }

    radar_calibrate_free_entries(peer->cal);
    radar_calibrate_reset_stats(peer->cal);
    peer->cal->data_num = 0;
    peer->cal->calibrate_status = RADAR_CALIBRATE_NO;
    peer->cal->last_action = ESP_RADAR_TRAIN_ACTION_IDLE;
    peer->waveform_wander_last = 0.0f;
    ESP_LOGI(TAG_TRAIN, "esp_radar_train_remove (peer=" MACSTR ")", MAC2STR(peer->mac));
    return ESP_OK;
}

esp_err_t esp_radar_train_stop_ex(esp_radar_handle_t handle, float *wander_threshold, float *jitter_threshold)
{
    esp_radar_peer_t *peer = (esp_radar_peer_t *)handle;
    /* Make output deterministic even on failure */
    if (wander_threshold) {
        *wander_threshold = 0.0f;
    }
    if (jitter_threshold) {
        *jitter_threshold = 0.0f;
    }
    if (!peer) {
        return ESP_ERR_INVALID_ARG;
    }

    if (!peer->cal) {
        return ESP_ERR_NOT_SUPPORTED;
    }

    if (!peer->cal->data_num || peer->cal->none_wander_count == 0.0f) {
        /* Exit training mode even if thresholds can't be produced, to avoid being stuck in PROGRESS */
        peer->cal->calibrate_status = RADAR_CALIBRATE_NO;
        return ESP_ERR_INVALID_STATE;
    }

    peer->cal->calibrate_status = RADAR_CALIBRATE_COMPLETE;
    /* Same scale as esp_radar_motion_dec: wander/jitter in [0,1], smaller = more stable. */
    float wander_threshold_value = peer->cal->none_wander_sum / peer->cal->none_wander_count;
    float jitter_threshold_value = peer->cal->static_wander;
    if (wander_threshold) {
        *wander_threshold = wander_threshold_value;
    }
    if (jitter_threshold) {
        *jitter_threshold = jitter_threshold_value;
    }

    ESP_LOGI(TAG_TRAIN, "esp_radar_train_stop (peer=" MACSTR "), data_num=%u, sum=%.6f, count=%.0f, wander_th=%.6f, jitter_th=%.6f",
             MAC2STR(peer->mac), (unsigned int)peer->cal->data_num, peer->cal->none_wander_sum, peer->cal->none_wander_count,
             wander_threshold_value, jitter_threshold_value);
    return ESP_OK;
}

esp_err_t esp_radar_train_start(void)
{
    if (!s_ctx.default_peer) {
        return ESP_ERR_INVALID_STATE;
    }
    return esp_radar_train_start_ex((esp_radar_handle_t)s_ctx.default_peer);
}

esp_err_t esp_radar_train_remove(void)
{
    if (!s_ctx.default_peer) {
        return ESP_ERR_INVALID_STATE;
    }
    return esp_radar_train_remove_ex((esp_radar_handle_t)s_ctx.default_peer);
}

esp_err_t esp_radar_train_stop(float *wander_threshold, float *jitter_threshold)
{
    if (!s_ctx.default_peer) {
        return ESP_ERR_INVALID_STATE;
    }
    return esp_radar_train_stop_ex((esp_radar_handle_t)s_ctx.default_peer, wander_threshold, jitter_threshold);
}

esp_err_t esp_radar_train_get_diag(esp_radar_train_diag_t *diag)
{
    if (!s_ctx.default_peer) {
        return ESP_ERR_INVALID_STATE;
    }
    return esp_radar_train_get_diag_ex((esp_radar_handle_t)s_ctx.default_peer, diag);
}

static esp_err_t csi_detection_compute_motion_dec(esp_radar_peer_t *peer, uint16_t cols, const csi_data_buff_index_t *buff_index, float *motion_dec_buff_current)
{
    if (!peer || !peer->csi_data_buff || !peer->csi_data_buff->amplitude) {
        return ESP_ERR_INVALID_STATE;
    }
    float (*csi_data_0)[cols] = (float (*)[cols])peer->csi_data_buff->amplitude[buff_index->begin];
    float (*csi_data_1)[cols] = (float (*)[cols])peer->csi_data_buff->amplitude[0];

    uint16_t csi_data_0_len = (buff_index->begin <= buff_index->end) ? buff_index->window : (peer->window_ctx.buff_size - buff_index->begin);
    uint16_t csi_data_1_len = buff_index->window - csi_data_0_len;

    if (csi_data_0_len == 0) {
        return ESP_FAIL;
    }

    return esp_radar_motion_dec_compute(cols, csi_data_0_len, csi_data_0, csi_data_1_len, csi_data_1, motion_dec_buff_current);
}

static void csi_detection_compute_wander(esp_radar_peer_t *peer, wifi_radar_info_t *radar_info,
                                         float *motion_dec_buff_current, uint16_t cols)
{
    if (!peer || !peer->cal || peer->cal->data_num == 0) {
        radar_info->waveform_wander = 0.0f;
        return;
    }

    radar_info->waveform_wander = 1.0f;
    for (size_t i = 0; i < peer->cal->data_num && i < CSI_WANDER_NUM; ++i) {
        float *record = peer->cal->data[i % CSI_WANDER_NUM];
        if (!record) {
            continue;
        }
        float waveform_wander_val = esp_radar_motion_dec_wander_compute(record, motion_dec_buff_current, cols);
        if (radar_info->waveform_wander > waveform_wander_val) {
            radar_info->waveform_wander = waveform_wander_val;
        }
    }
}

static void csi_training_collect_sample(esp_radar_peer_t *peer, wifi_radar_info_t *radar_info, float **motion_dec_buff, uint16_t cols)
{
    uint8_t move_buffer_size = s_ctx.dec_config.dec_window_size;
    if (!peer || !peer->cal) {
        return;
    }

    size_t idx_first = (peer->cal->buff_size + RADAR_BUFF_NUM - 2) % RADAR_BUFF_NUM;
    size_t idx_second = (peer->cal->buff_size + RADAR_BUFF_NUM - 1) % RADAR_BUFF_NUM;
    size_t idx_third = peer->cal->buff_size % RADAR_BUFF_NUM;

    peer->cal->waveform_jitter_buff[idx_third] = radar_info->waveform_jitter;
    peer->cal->buff_size++;

    if (peer->cal->buff_size < RADAR_BUFF_NUM) {
        peer->cal->last_action = ESP_RADAR_TRAIN_ACTION_WAIT_BUFFER;
        return;
    }

    float first = peer->cal->waveform_jitter_buff[idx_first];
    float second = peer->cal->waveform_jitter_buff[idx_second];
    float third = peer->cal->waveform_jitter_buff[idx_third];

    if ((first - second > RADAR_OUTLIERS_THRESHOLD) && (third - second > RADAR_OUTLIERS_THRESHOLD)) {
        peer->cal->last_action = ESP_RADAR_TRAIN_ACTION_DISCARD_OUTLIER;
        ESP_LOGI(TAG_TRAIN, "Jitter outlier detected: %.4f < %.4f, %.4f", second, first, third);
        return;
    }

    if (peer->cal->static_wander < radar_info->waveform_jitter) {
        peer->cal->static_wander = second;
    }

    if (peer->waveform_wander_last > CSI_WANDER_THRESHOLD) {
        float *motion_dec_prev = motion_dec_buff[(peer->motion_dec_buff_num + move_buffer_size - 2) % move_buffer_size];
        if (motion_dec_prev) {
            size_t index = peer->cal->data_num % CSI_WANDER_NUM;

            if (!peer->cal->data[index]) {
                peer->cal->data[index] = RADAR_MALLOC_RETRY((size_t)cols * sizeof(float));
            }

            memcpy(peer->cal->data[index], motion_dec_prev, (size_t)cols * sizeof(float));
            peer->cal->data_num++;
            peer->cal->none_wander = 0.0f;
            radar_info->waveform_wander = 0.0f;
            peer->cal->last_action = ESP_RADAR_TRAIN_ACTION_COLLECT_SAMPLE;

            ESP_LOGI(TAG_TRAIN, "Training sample collected: num=%zu, wander=%.4f",
                     peer->cal->data_num, peer->waveform_wander_last);
        }
    } else {
        peer->cal->last_action = ESP_RADAR_TRAIN_ACTION_ACCUMULATE_BACKGROUND;
        peer->cal->none_wander = peer->waveform_wander_last;
        if (peer->waveform_wander_last > 0.00001f) {
            peer->cal->none_wander_sum += peer->waveform_wander_last;
            peer->cal->none_wander_count += 1.0f;

            ESP_LOGI(TAG_TRAIN, "Training stats: sum=%.4f, count=%.0f, avg=%.4f",
                     peer->cal->none_wander_sum, peer->cal->none_wander_count,
                     peer->cal->none_wander_sum / peer->cal->none_wander_count);
        }
    }

    peer->waveform_wander_last = radar_info->waveform_wander;
}

esp_err_t esp_radar_train_get_diag_ex(esp_radar_handle_t handle, esp_radar_train_diag_t *diag)
{
    esp_radar_peer_t *peer = (esp_radar_peer_t *)handle;
    if (!peer || !diag) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!peer->cal) {
        return ESP_ERR_NOT_SUPPORTED;
    }

    memset(diag, 0, sizeof(*diag));
    diag->status = radar_train_status_to_public(peer->cal->calibrate_status);
    diag->last_action = peer->cal->last_action;
    diag->template_sample_count = (uint32_t)peer->cal->data_num;
    diag->background_stats_count = (uint32_t)peer->cal->none_wander_count;
    diag->background_stats_sum = peer->cal->none_wander_sum;
    diag->background_stats_avg = (peer->cal->none_wander_count > 0.0f)
                                 ? (peer->cal->none_wander_sum / peer->cal->none_wander_count)
                                 : 0.0f;
    diag->last_basis_wander = peer->waveform_wander_last;
    diag->static_jitter_reference = peer->cal->static_wander;
    diag->thresholds_ready = (peer->cal->calibrate_status == RADAR_CALIBRATE_COMPLETE);
    return ESP_OK;
}

static void csi_detection_task(void *arg)
{
    uint8_t move_buffer_size = s_ctx.dec_config.dec_window_size;
    uint16_t cols = 0;
    csi_window_msg_t msg = {0};
    uint32_t motion_failure_count = 0;
    uint32_t last_motion_failure_log_ms = 0;

    while (xQueueReceive(s_ctx.csi_data_queue, &msg, portMAX_DELAY)) {
        esp_radar_peer_t *peer = msg.peer;
        csi_data_buff_index_t buff_index = msg.index;
        if (!s_ctx.run_flag) {
            break;
        }
        if (!peer) {
            continue;
        }
        cols = peer->subcarrier_len;
        if (!peer->csi_data_buff || !peer->csi_data_buff->amplitude || cols == 0) {
            ESP_LOGW(TAG_DETECTION, "CSI buffer not ready");
            continue;
        }

        if (!peer->motion_dec_buff_allocated) {
            peer->motion_dec_buff = RADAR_MALLOC_RETRY(move_buffer_size * sizeof(float *));
            memset(peer->motion_dec_buff, 0, move_buffer_size * sizeof(float *));
            for (int i = 0; i < move_buffer_size; ++i) {
                peer->motion_dec_buff[i] = RADAR_MALLOC_RETRY(cols * sizeof(float));
            }
            peer->motion_dec_buff_allocated = true;
            peer->motion_dec_capacity = move_buffer_size;
            ESP_LOGI(TAG_DETECTION, "Allocated motion detection buffer: %d x %d", move_buffer_size, cols);
        }

        wifi_radar_info_t radar_info = {
            .waveform_jitter = 0,
            .waveform_wander = 0,
        };

        uint32_t timestamp_start = esp_log_timestamp();

        float *motion_dec_buff_current = peer->motion_dec_buff[peer->motion_dec_buff_num % move_buffer_size];

        esp_err_t motion_ret = csi_detection_compute_motion_dec(
            peer, cols, &buff_index, motion_dec_buff_current);
        if (motion_ret != ESP_OK) {
            motion_failure_count++;
            uint32_t now_ms = esp_log_timestamp();
            if (last_motion_failure_log_ms == 0 || now_ms - last_motion_failure_log_ms >= 5000) {
                ESP_LOGW(TAG_DETECTION,
                         "Motion detection calculation skipped: error=%s, failures=%u, rows=%u",
                         esp_err_to_name(motion_ret), motion_failure_count, buff_index.window);
                last_motion_failure_log_ms = now_ms;
            }
            continue;
        }
        peer->motion_dec_buff_num++;

        radar_info.waveform_jitter = esp_radar_motion_dec_compute_jitter(
                                         motion_dec_buff_current, peer->motion_dec_buff, cols, move_buffer_size, peer->motion_dec_buff_num);

        if (peer->cal) {
            if (peer->cal->subcarrier_len == 0) {
                peer->cal->subcarrier_len = cols;
            } else if (peer->cal->subcarrier_len != cols) {
                ESP_LOGW(TAG_TRAIN, "Subcarrier length changed from %u to %u, reset training data",
                         peer->cal->subcarrier_len, cols);
                esp_radar_train_remove_ex(peer);
                peer->cal->subcarrier_len = cols;
            }

            csi_detection_compute_wander(peer, &radar_info, motion_dec_buff_current, cols);

            if (peer->cal->calibrate_status == RADAR_CALIBRATE_PROGRESS && peer->motion_dec_buff_num >= 2) {
                csi_training_collect_sample(peer, &radar_info, peer->motion_dec_buff, cols);
            }
        }

        int32_t time_spent  = peer->csi_data_buff->timestamp[buff_index.end] - peer->csi_data_buff->timestamp[buff_index.begin];
        if (time_spent > 0) {
            ESP_LOGD(TAG_DETECTION, "det_time: %u/%u, free_heap: %d, wander: %f, jitter: %f, window: %d, begin: %d, end: %d, freq: %dHz",
                     time_spent, esp_log_timestamp() - timestamp_start, esp_get_free_heap_size(),
                     radar_info.waveform_wander, radar_info.waveform_jitter, buff_index.window,
                     buff_index.begin, buff_index.end, buff_index.window * 1000 / time_spent);
        }

        if (s_ctx.dec_config.wifi_radar_cb_ex) {
            s_ctx.dec_config.wifi_radar_cb_ex(s_ctx.dec_config.wifi_radar_cb_ex_ctx, peer->mac, &radar_info);
        }
        if (s_ctx.dec_config.wifi_radar_cb && peer == s_ctx.default_peer) {
            s_ctx.dec_config.wifi_radar_cb(s_ctx.dec_config.wifi_radar_cb_ctx, &radar_info);
        }
    }

    ESP_LOGI(TAG_DETECTION, "csi_detection_task stopped for radar reconfiguration");
    xEventGroupSetBits(s_ctx.task_exit_group, CSI_DETECTION_EXIT_BIT);
    vTaskDelete(NULL);
}
esp_err_t esp_radar_start()
{
    if (s_ctx.run_flag) {
        return ESP_OK;
    }
    if (!s_ctx.init_flag) {
        return ESP_ERR_INVALID_STATE;
    }

    s_ctx.run_flag = true;
    s_ctx.csi_info_queue = xQueueCreate(8, sizeof(csi_info_msg_t));
    s_ctx.csi_data_queue = xQueueCreate(8, sizeof(csi_window_msg_t));
    s_ctx.task_exit_group  = xEventGroupCreate();
    if (s_ctx.dec_config.csi_handle_time < s_ctx.csi_config.csi_recv_interval * (s_ctx.dec_config.dec_window_size)) {
        ESP_LOGE(TAG, "csi_handle_time is too short, will set to %d", s_ctx.csi_config.csi_recv_interval * (s_ctx.dec_config.dec_window_size));
        s_ctx.dec_config.csi_handle_time = s_ctx.csi_config.csi_recv_interval * (s_ctx.dec_config.dec_window_size);
    }
    s_ctx.handle_window     = (s_ctx.dec_config.csi_handle_time / s_ctx.csi_config.csi_recv_interval) * 2;
    s_ctx.buff_size         = (s_ctx.dec_config.csi_handle_time / s_ctx.csi_config.csi_recv_interval) * 2 + 20;

    ESP_LOGI(TAG, "[%s, %d] csi_recv_interval: %d, csi_handle_time: %d, csi_handle_window: %d, csi_handle_buffer: %d",
             __func__, __LINE__,
             s_ctx.csi_config.csi_recv_interval, s_ctx.dec_config.csi_handle_time,
             (int)s_ctx.handle_window, (int)s_ctx.buff_size);

    /* Reset and allocate buffers for all registered peers */
    for (size_t i = 0; i < ESP_RADAR_MAX_PEERS; ++i) {
        esp_radar_peer_t *p = s_ctx.peers[i];
        if (!p) {
            continue;
        }
        esp_radar_peer_free_motion_dec_buffers(p);
        esp_radar_peer_free_ring_buffers(p);
        esp_radar_peer_ensure_ring_buffers(p);
    }

    xTaskCreate(csi_detection_task, "csi_handle", 3 * 1024, NULL, s_ctx.dec_config.csi_handle_priority, NULL);
    xTaskCreate(csi_preprocessing_task, "csi_combine", 3 * 1024, NULL, s_ctx.dec_config.csi_combine_priority, NULL);

    return ESP_OK;
}

esp_err_t esp_radar_stop()
{
    if (!s_ctx.run_flag) {
        return ESP_OK;
    }
    csi_info_msg_t info_msg = {0};
    csi_window_msg_t win_msg = {0};
    csi_info_msg_t info_recv = {0};
    csi_window_msg_t win_recv = {0};
    s_ctx.run_flag = false;
    xQueueSend(s_ctx.csi_info_queue, &info_msg, 0);
    xQueueSend(s_ctx.csi_data_queue, &win_msg, 0);

    xEventGroupWaitBits(s_ctx.task_exit_group, CSI_DETECTION_EXIT_BIT | CSI_PREPROCESSING_EXIT_BIT,
                        pdTRUE, pdTRUE, portMAX_DELAY);

    if (s_ctx.csi_info_queue) {
        while (xQueueReceive(s_ctx.csi_info_queue, &info_recv, 0)) {
            if (info_recv.filtered_info) {
                RADAR_FREE(info_recv.filtered_info);
            }
        }
    }

    if (s_ctx.csi_data_queue) {
        while (xQueueReceive(s_ctx.csi_data_queue, &win_recv, 0)) ;
        vQueueDelete(s_ctx.csi_data_queue);
        s_ctx.csi_data_queue = NULL;
    }

    if (s_ctx.csi_info_queue) {
        vQueueDelete(s_ctx.csi_info_queue);
        s_ctx.csi_info_queue = NULL;
    }

    vEventGroupDelete(s_ctx.task_exit_group);
    s_ctx.task_exit_group = NULL;

    /* Free per-peer runtime buffers */
    for (size_t i = 0; i < ESP_RADAR_MAX_PEERS; ++i) {
        esp_radar_peer_t *p = s_ctx.peers[i];
        if (!p) {
            continue;
        }
        esp_radar_peer_free_motion_dec_buffers(p);
        esp_radar_peer_free_ring_buffers(p);
    }
    s_ctx.buff_size = 0;
    s_ctx.handle_window = 0;
    return ESP_OK;
}

esp_err_t esp_radar_csi_init(esp_radar_csi_config_t *config)
{
    if (!config) {
        return ESP_ERR_INVALID_ARG;
    }
    wifi_csi_config_t wifi_csi_config;
    s_ctx.lltf_bit_mode = false;
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
    wifi_csi_config = (wifi_csi_config_t) {
        .enable                   = true,
        .acquire_csi_legacy       = config->acquire_csi_lltf,
        .acquire_csi_force_lltf   = config->acquire_csi_lltf,
        .acquire_csi_ht20         = config->acquire_csi_ht20,
        .acquire_csi_ht40         = config->acquire_csi_ht40,
        .acquire_csi_vht          = config->acquire_csi_vht,
        .acquire_csi_su           = config->acquire_csi_su,
        .acquire_csi_mu           = config->acquire_csi_mu,
        .acquire_csi_dcm          = config->acquire_csi_dcm,
        .acquire_csi_beamformed   = config->acquire_csi_beamformed,
        .acquire_csi_he_stbc_mode = config->acquire_csi_he_stbc_mode,
        .val_scale_cfg            = config->val_scale_cfg,
        .dump_ack_en              = config->dump_ack_en,
        .reserved                 = 0
    };
    if (config->acquire_csi_lltf) {
        ESP_LOGW(TAG, "%s: LLTF collection is enabled, other collection settings will be ignored", CONFIG_IDF_TARGET);
    }
    s_ctx.lltf_bit_mode = config->acquire_csi_lltf;
#elif CONFIG_IDF_TARGET_ESP32C6
    wifi_csi_config = (wifi_csi_config_t) {
        .enable                 = true,
        .acquire_csi_legacy     = config->acquire_csi_lltf,
        .acquire_csi_ht20       = config->acquire_csi_ht20,
        .acquire_csi_ht40       = config->acquire_csi_ht40,
        .acquire_csi_su         = config->acquire_csi_su,
        .acquire_csi_mu         = config->acquire_csi_mu,
        .acquire_csi_dcm        = config->acquire_csi_dcm,
        .acquire_csi_beamformed = config->acquire_csi_beamformed,
        .acquire_csi_he_stbc    = config->acquire_csi_he_stbc_mode,
        .val_scale_cfg          = config->val_scale_cfg,
        .dump_ack_en            = config->dump_ack_en,
        .reserved               = 0
    };
#elif CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32C3 || CONFIG_IDF_TARGET_ESP32
    wifi_csi_config = (wifi_csi_config_t) {
        .lltf_en                = config->lltf_en,
        .htltf_en               = config->htltf_en,
        .stbc_htltf2_en         = config->stbc_htltf2_en,
        .ltf_merge_en           = config->ltf_merge_en,
        .channel_filter_en      = config->channel_filter_en,
        .manu_scale             = config->manu_scale,
        .shift                  = config->shift,
        .dump_ack_en            = config->dump_ack_en,
    };
#else
    ESP_LOGE(TAG, "CSI functionality is not supported for %s.", CONFIG_IDF_TARGET);
    return ESP_ERR_NOT_SUPPORTED;
#endif
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&wifi_csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(esp_radar_csi_rx_cb, config->csi_filtered_cb_ctx));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    memcpy(&s_ctx.csi_config, config, sizeof(esp_radar_csi_config_t));

    return ESP_OK;
}

esp_err_t esp_radar_wifi_init(esp_radar_wifi_config_t *config)
{
    static bool s_wifi_inited = false;
    if (!config) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_wifi_inited) {
        ESP_ERROR_CHECK(esp_event_loop_create_default());
        ESP_ERROR_CHECK(esp_netif_init());
        s_wifi_inited = true;
    }
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    if (!ADDR_IS_EMPTY(config->mac_address)) {
        ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, config->mac_address));
    }

#if !CONFIG_IDF_TARGET_ESP32C5
    if (config->band_mode == WIFI_BAND_MODE_5G_ONLY || config->band_mode == WIFI_BAND_MODE_AUTO) {
        ESP_LOGW(TAG, "%s does not support 5GHz band_mode, forcing to WIFI_BAND_MODE_2G_ONLY", CONFIG_IDF_TARGET);
        config->band_mode = WIFI_BAND_MODE_2G_ONLY;
    }
    if (config->protocols.ghz_5g != 0) {
        ESP_LOGW(TAG, "%s does not support 5GHz protocols, clearing 5GHz protocol configuration", CONFIG_IDF_TARGET);
        config->protocols.ghz_5g = 0;
    }
    if (config->bandwidths.ghz_5g != 0) {
        ESP_LOGW(TAG, "%s does not support 5GHz bandwidths, clearing 5GHz bandwidth configuration", CONFIG_IDF_TARGET);
        config->bandwidths.ghz_5g = 0;
    }
#endif
    esp_netif_create_default_wifi_sta();
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(config->band_mode));
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &config->protocols));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &config->bandwidths));
    bool is_20mhz = (config->channel <= 13 && config->bandwidths.ghz_2g == WIFI_BW_HT20) ||
                    (config->channel > 13 && config->bandwidths.ghz_5g == WIFI_BW_HT20);
#else
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &config->protocols));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, config->bandwidths.ghz_2g));
    ESP_ERROR_CHECK(esp_wifi_start());
    bool is_20mhz = (config->bandwidths.ghz_2g == WIFI_BW_HT20);
#endif

    if (is_20mhz) {
        if (config->second_chan != WIFI_SECOND_CHAN_NONE) {
            ESP_LOGW(TAG, "20MHz bandwidth configured but secondary channel is set to %d, forcing to NONE (channel: %d)",
                     config->second_chan, config->channel);
        }
        ESP_ERROR_CHECK(esp_wifi_set_channel(config->channel, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(config->channel, config->second_chan));
    }
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    memcpy(&s_ctx.wifi_config, config, sizeof(esp_radar_wifi_config_t));
    return ESP_OK;
}

esp_err_t esp_radar_espnow_init(esp_radar_espnow_config_t *config)
{
    if (!config) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_ctx.wifi_config.channel == 0) {
        ESP_LOGW(TAG, "channel is not set, please first run esp_radar_wifi_init");
        return ESP_ERR_INVALID_STATE;
    }
    esp_now_peer_info_t peer = {
        .channel   = s_ctx.wifi_config.channel,
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
    };
    memcpy(peer.peer_addr, config->peer_addr, sizeof(peer.peer_addr));

    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)config->pmk));
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    wifi_phy_mode_t phy_mode = 0;
    if (s_ctx.wifi_config.channel <= 13) {
        switch (s_ctx.wifi_config.protocols.ghz_2g) {
        case WIFI_PROTOCOL_11B:
            ESP_LOGW(TAG, "11b not supported csi");
            phy_mode = WIFI_PHY_MODE_11B;
            break;
        case WIFI_PROTOCOL_11G:
            phy_mode = WIFI_PHY_MODE_11G;
            break;
        case WIFI_PROTOCOL_11N:
            if (s_ctx.wifi_config.bandwidths.ghz_2g == WIFI_BW_HT20) {
                phy_mode = WIFI_PHY_MODE_HT20;
            } else {
                phy_mode = WIFI_PHY_MODE_HT40;
            }
            break;
        case WIFI_PROTOCOL_11AX:
            phy_mode = WIFI_PHY_MODE_HE20;
            break;
        }
    } else {
        switch (s_ctx.wifi_config.protocols.ghz_5g) {
        case WIFI_PROTOCOL_11A:
        case WIFI_PROTOCOL_11AC:
            if (s_ctx.wifi_config.bandwidths.ghz_5g == WIFI_BW_HT20) {
                phy_mode = WIFI_PHY_MODE_HT20;
            } else {
                phy_mode = WIFI_PHY_MODE_HT40;
            }
            break;
        case WIFI_PROTOCOL_11AX:
            phy_mode = WIFI_PHY_MODE_HE20;
            break;
        }
    }

    esp_now_rate_config_t rate_config = {
        .phymode = phy_mode,
        .rate = config->rate,
        .ersu = false,
        .dcm = false
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));
    memcpy(&s_ctx.espnow_config, config, sizeof(esp_radar_espnow_config_t));
    return ESP_OK;
}

esp_err_t esp_radar_get_config(esp_radar_config_t *config)
{
    if (!config) {
        return ESP_ERR_INVALID_ARG;
    }

    memcpy(&config->wifi_config, &s_ctx.wifi_config, sizeof(esp_radar_wifi_config_t));
    memcpy(&config->csi_config, &s_ctx.csi_config, sizeof(esp_radar_csi_config_t));
    memcpy(&config->espnow_config, &s_ctx.espnow_config, sizeof(esp_radar_espnow_config_t));
    memcpy(&config->dec_config, &s_ctx.dec_config, sizeof(esp_radar_dec_config_t));
    return ESP_OK;
}
esp_err_t esp_radar_wifi_reinit(esp_radar_wifi_config_t *config)
{
    if (!config) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_wifi_stop();
    esp_wifi_deinit();
    esp_err_t ret = esp_radar_wifi_init(config);
    if (ret != ESP_OK) {
        return ret;
    }
    return ESP_OK;
}
esp_err_t esp_radar_change_config(esp_radar_config_t *config)
{
    if (!config) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_radar_wifi_config_t *wifi_config = &config->wifi_config;
    esp_radar_csi_config_t *csi_config = &config->csi_config;
    esp_radar_espnow_config_t *espnow_config = &config->espnow_config;
    esp_radar_dec_config_t *dec_config = &config->dec_config;
    bool need_start = false;
    if (s_ctx.run_flag) {
        esp_radar_stop();
        need_start = true;
    }
    if (wifi_config && memcmp(wifi_config, &s_ctx.wifi_config, sizeof(esp_radar_wifi_config_t)) != 0) {
        esp_radar_wifi_reinit(wifi_config);
    }
    if (espnow_config && memcmp(espnow_config, &s_ctx.espnow_config, sizeof(esp_radar_espnow_config_t)) != 0) {
        esp_radar_espnow_init(espnow_config);
    }
    if (csi_config) {
        esp_err_t ret = esp_radar_sync_default_peer(csi_config->filter_mac);
        if (ret != ESP_OK) {
            return ret;
        }
        if (memcmp(csi_config, &s_ctx.csi_config, sizeof(esp_radar_csi_config_t)) != 0) {
            esp_radar_csi_init(csi_config);
        }
    }
    if (dec_config && memcmp(dec_config, &s_ctx.dec_config, sizeof(esp_radar_dec_config_t)) != 0) {
        esp_radar_dec_init(dec_config);
    }
    if (need_start) {
        esp_radar_start();
    }
    ESP_LOGI(TAG, "esp_radar_change_config: amplitude_log_enabled=%d", dec_config->amplitude_log_enabled);
    return ESP_OK;
}
esp_err_t esp_radar_dec_init(esp_radar_dec_config_t *config)
{
    if (!config) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_ctx.init_flag) {
        ESP_LOGW(TAG, "esp_radar decoder already initialized, updating config");
    } else {
        s_ctx.init_flag = true;
    }
    memcpy(&s_ctx.dec_config, config, sizeof(esp_radar_dec_config_t));
    if (s_ctx.dec_config.dec_window_size < RADAR_MOTION_DEC_WINDOW_DEFAULT) {
        ESP_LOGW(TAG, "dec_window_size < 2, fallback to default: %d", RADAR_MOTION_DEC_WINDOW_DEFAULT);
        s_ctx.dec_config.dec_window_size = RADAR_MOTION_DEC_WINDOW_DEFAULT;
    }

#if WIFI_CSI_PHY_GAIN_ENABLE
    esp_csi_gain_ctrl_reset_rx_gain_baseline();
#endif
    return ESP_OK;
}

esp_err_t esp_radar_init(esp_radar_config_t *config)
{
    if (!config) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_radar_wifi_init(&config->wifi_config);
    esp_radar_espnow_init(&config->espnow_config);
    esp_radar_csi_init(&config->csi_config);
    esp_radar_dec_init(&config->dec_config);
    /* Create default peer for legacy APIs */
    if (!s_ctx.default_peer) {
        esp_err_t ret = esp_radar_sync_default_peer(config->csi_config.filter_mac);
        if (ret != ESP_OK) {
            return ret;
        }
    }
    return ESP_OK;
}
esp_err_t esp_radar_deinit()
{
    if (s_ctx.run_flag) {
        esp_radar_stop();
    }
    ESP_ERROR_CHECK(esp_wifi_set_csi(false));

    /* Free all peers */
    for (size_t i = 0; i < ESP_RADAR_MAX_PEERS; ++i) {
        esp_radar_peer_t *p = s_ctx.peers[i];
        if (!p) {
            continue;
        }
        esp_radar_peer_free_motion_dec_buffers(p);
        esp_radar_peer_free_ring_buffers(p);
        if (p->cal) {
            radar_calibrate_free_entries(p->cal);
            RADAR_FREE(p->cal);
        }
        RADAR_FREE(p);
        s_ctx.peers[i] = NULL;
    }
    s_ctx.default_peer = NULL;
    s_ctx.peer_count = 0;
    memset(&s_ctx, 0, sizeof(s_ctx));

    return ESP_OK;
}
