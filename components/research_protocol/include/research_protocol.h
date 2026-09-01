#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define RESEARCH_FRAME_MAGIC UINT32_C(0x49534352) /* ASCII "RCSI" on the wire */
#define RESEARCH_FRAME_VERSION 1
#define RESEARCH_EXPERIMENT_ID_LEN 32
#define RESEARCH_FRAME_MAX_LEN 250
#define RESEARCH_FRAME_FLAG_BROADCAST UINT16_C(0x0001)
#define RESEARCH_FRAME_FLAG_MONOTONIC_CLOCK UINT16_C(0x0002)

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint8_t version;
    uint8_t header_len;
    uint16_t frame_len;
    uint32_t experiment_hash;
    uint32_t packet_seq;
    uint64_t tx_timestamp_us;
    uint16_t payload_length;
    uint16_t flags;
    uint32_t crc32;
    char experiment_id[RESEARCH_EXPERIMENT_ID_LEN];
} research_frame_header_t;

typedef enum {
    RESEARCH_FRAME_OK = 0,
    RESEARCH_FRAME_ERR_ARGUMENT,
    RESEARCH_FRAME_ERR_LENGTH,
    RESEARCH_FRAME_ERR_MAGIC,
    RESEARCH_FRAME_ERR_VERSION,
    RESEARCH_FRAME_ERR_HEADER,
    RESEARCH_FRAME_ERR_EXPERIMENT_ID,
    RESEARCH_FRAME_ERR_HASH,
    RESEARCH_FRAME_ERR_CRC,
} research_frame_result_t;

size_t research_frame_header_size(void);
bool research_experiment_id_valid(const char *experiment_id);
uint32_t research_experiment_hash(const char *experiment_id);
uint32_t research_crc32(const uint8_t *data, size_t length);

research_frame_result_t research_frame_encode(
    uint8_t *frame,
    size_t frame_capacity,
    size_t frame_length,
    const char *experiment_id,
    uint32_t packet_seq,
    uint64_t tx_timestamp_us,
    uint16_t flags);

research_frame_result_t research_frame_decode(
    const uint8_t *frame,
    size_t frame_length,
    research_frame_header_t *header_out);

const char *research_frame_result_name(research_frame_result_t result);

#ifdef __cplusplus
}
#endif
