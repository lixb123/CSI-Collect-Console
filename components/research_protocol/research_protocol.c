#include "research_protocol.h"

#include <ctype.h>
#include <string.h>

_Static_assert(sizeof(research_frame_header_t) == 64, "research frame header must remain 64 bytes");

size_t research_frame_header_size(void)
{
    return sizeof(research_frame_header_t);
}

bool research_experiment_id_valid(const char *experiment_id)
{
    if (!experiment_id || !experiment_id[0]) {
        return false;
    }
    size_t length = strnlen(experiment_id, RESEARCH_EXPERIMENT_ID_LEN);
    if (length == 0 || length >= RESEARCH_EXPERIMENT_ID_LEN) {
        return false;
    }
    for (size_t i = 0; i < length; ++i) {
        unsigned char ch = (unsigned char)experiment_id[i];
        if (!(isalnum(ch) || ch == '.' || ch == '_' || ch == '-')) {
            return false;
        }
    }
    return true;
}

uint32_t research_experiment_hash(const char *experiment_id)
{
    uint32_t hash = UINT32_C(2166136261);
    if (!experiment_id) {
        return hash;
    }
    for (const uint8_t *cursor = (const uint8_t *)experiment_id; *cursor; ++cursor) {
        hash ^= *cursor;
        hash *= UINT32_C(16777619);
    }
    return hash;
}

uint32_t research_crc32(const uint8_t *data, size_t length)
{
    uint32_t crc = UINT32_MAX;
    if (!data && length) {
        return 0;
    }
    for (size_t i = 0; i < length; ++i) {
        crc ^= data[i];
        for (unsigned bit = 0; bit < 8; ++bit) {
            uint32_t mask = (uint32_t)-(int32_t)(crc & 1U);
            crc = (crc >> 1) ^ (UINT32_C(0xEDB88320) & mask);
        }
    }
    return ~crc;
}

research_frame_result_t research_frame_encode(
    uint8_t *frame,
    size_t frame_capacity,
    size_t frame_length,
    const char *experiment_id,
    uint32_t packet_seq,
    uint64_t tx_timestamp_us,
    uint16_t flags)
{
    if (!frame || !experiment_id) {
        return RESEARCH_FRAME_ERR_ARGUMENT;
    }
    if (frame_length < sizeof(research_frame_header_t) ||
            frame_length > RESEARCH_FRAME_MAX_LEN || frame_length > frame_capacity) {
        return RESEARCH_FRAME_ERR_LENGTH;
    }
    if (!research_experiment_id_valid(experiment_id)) {
        return RESEARCH_FRAME_ERR_EXPERIMENT_ID;
    }

    memset(frame, 0, frame_length);
    research_frame_header_t *header = (research_frame_header_t *)frame;
    header->magic = RESEARCH_FRAME_MAGIC;
    header->version = RESEARCH_FRAME_VERSION;
    header->header_len = (uint8_t)sizeof(*header);
    header->frame_len = (uint16_t)frame_length;
    header->experiment_hash = research_experiment_hash(experiment_id);
    header->packet_seq = packet_seq;
    header->tx_timestamp_us = tx_timestamp_us;
    header->payload_length = (uint16_t)(frame_length - sizeof(*header));
    header->flags = flags;
    strncpy(header->experiment_id, experiment_id, sizeof(header->experiment_id) - 1);

    for (size_t i = sizeof(*header); i < frame_length; ++i) {
        frame[i] = (uint8_t)((packet_seq + (uint32_t)i) & UINT32_C(0xFF));
    }
    header->crc32 = 0;
    header->crc32 = research_crc32(frame, frame_length);
    return RESEARCH_FRAME_OK;
}

research_frame_result_t research_frame_decode(
    const uint8_t *frame,
    size_t frame_length,
    research_frame_header_t *header_out)
{
    if (!frame || !header_out) {
        return RESEARCH_FRAME_ERR_ARGUMENT;
    }
    if (frame_length < sizeof(research_frame_header_t) || frame_length > RESEARCH_FRAME_MAX_LEN) {
        return RESEARCH_FRAME_ERR_LENGTH;
    }

    research_frame_header_t header;
    memcpy(&header, frame, sizeof(header));
    if (header.magic != RESEARCH_FRAME_MAGIC) {
        return RESEARCH_FRAME_ERR_MAGIC;
    }
    if (header.version != RESEARCH_FRAME_VERSION) {
        return RESEARCH_FRAME_ERR_VERSION;
    }
    if (header.header_len != sizeof(research_frame_header_t) ||
            header.frame_len != frame_length ||
            header.payload_length != frame_length - sizeof(research_frame_header_t)) {
        return RESEARCH_FRAME_ERR_HEADER;
    }
    if (!memchr(header.experiment_id, '\0', sizeof(header.experiment_id)) ||
            !research_experiment_id_valid(header.experiment_id)) {
        return RESEARCH_FRAME_ERR_EXPERIMENT_ID;
    }
    if (header.experiment_hash != research_experiment_hash(header.experiment_id)) {
        return RESEARCH_FRAME_ERR_HASH;
    }

    uint8_t copy[RESEARCH_FRAME_MAX_LEN];
    memcpy(copy, frame, frame_length);
    ((research_frame_header_t *)copy)->crc32 = 0;
    if (research_crc32(copy, frame_length) != header.crc32) {
        return RESEARCH_FRAME_ERR_CRC;
    }
    *header_out = header;
    return RESEARCH_FRAME_OK;
}

const char *research_frame_result_name(research_frame_result_t result)
{
    switch (result) {
    case RESEARCH_FRAME_OK: return "ok";
    case RESEARCH_FRAME_ERR_ARGUMENT: return "argument";
    case RESEARCH_FRAME_ERR_LENGTH: return "length";
    case RESEARCH_FRAME_ERR_MAGIC: return "magic";
    case RESEARCH_FRAME_ERR_VERSION: return "version";
    case RESEARCH_FRAME_ERR_HEADER: return "header";
    case RESEARCH_FRAME_ERR_EXPERIMENT_ID: return "experiment_id";
    case RESEARCH_FRAME_ERR_HASH: return "hash";
    case RESEARCH_FRAME_ERR_CRC: return "crc";
    default: return "unknown";
    }
}
