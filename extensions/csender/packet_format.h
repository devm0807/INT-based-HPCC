/*
 * packet_format.h — C mirror of sender/packet_format.py.
 *
 * Wire format pinned by p4src/common/headers.p4. Any field change there
 * MUST land here too. Layout, top to bottom on the UDP payload:
 *
 *   shim_t (4 B)
 *   int_hop_t × N (26 B each)
 *   data_payload_t or ack_payload_t (16 B)
 *
 * All multi-byte fields are big-endian (network byte order).
 */
#ifndef _INTCC_PACKET_FORMAT_H_
#define _INTCC_PACKET_FORMAT_H_

#include <stdint.h>
#include <string.h>
#include <arpa/inet.h>

#define DATA_PORT 50000
#define ACK_PORT  50001
#define MAX_HOPS  4

#define SHIM_FLAG_ECN_ECHO 0x01

#define SHIM_SIZE         4
#define INT_HOP_SIZE      26
#define DATA_PAYLOAD_SIZE 16
#define ACK_PAYLOAD_SIZE  16

/* htobe64 / be64toh are GNU; provide a portable fallback. */
#if defined(__APPLE__)
#  include <libkern/OSByteOrder.h>
#  define htobe64(x) OSSwapHostToBigInt64(x)
#  define be64toh(x) OSSwapBigToHostInt64(x)
#else
#  include <endian.h>
#endif

#pragma pack(push, 1)

struct shim_t {
    uint8_t flags;
    uint8_t hop_count;
    uint8_t max_hops;
    uint8_t reserved;
};

/* int_hop_t uses 48-bit fields that don't pack cleanly as a C struct;
 * we hold the 6-byte big-endian blobs inline. */
struct int_hop_t {
    uint16_t switch_id;     /* be16 */
    uint16_t ingress_port;  /* be16 */
    uint16_t egress_port;   /* be16 */
    uint32_t qdepth;        /* be32 */
    uint8_t  egress_tstamp_us[6];  /* u48 big-endian */
    uint8_t  tx_byte_count[6];     /* u48 big-endian */
    uint32_t link_bps;      /* be32 */
};

struct data_payload_t {
    uint32_t seq;         /* be32 */
    uint64_t send_ts_us;  /* be64 */
    uint32_t reserved;    /* be32 */
};

struct ack_payload_t {
    uint32_t ack_seq;     /* be32 */
    uint64_t recv_ts_us;  /* be64 */
    uint32_t reserved;    /* be32 */
};

#pragma pack(pop)

/* --- u48 helpers --- */
static inline void u48_pack(uint8_t out[6], uint64_t v) {
    out[0] = (v >> 40) & 0xFF;
    out[1] = (v >> 32) & 0xFF;
    out[2] = (v >> 24) & 0xFF;
    out[3] = (v >> 16) & 0xFF;
    out[4] = (v >>  8) & 0xFF;
    out[5] = (v      ) & 0xFF;
}

static inline uint64_t u48_unpack(const uint8_t in[6]) {
    return ((uint64_t)in[0] << 40) | ((uint64_t)in[1] << 32) |
           ((uint64_t)in[2] << 24) | ((uint64_t)in[3] << 16) |
           ((uint64_t)in[4] <<  8) | (uint64_t)in[5];
}

/* --- decoded forms (host byte order) --- */
struct int_hop_host {
    uint16_t switch_id;
    uint16_t ingress_port;
    uint16_t egress_port;
    uint32_t qdepth;
    uint64_t egress_tstamp_us;
    uint64_t tx_byte_count;
    uint32_t link_bps;
};

static inline void int_hop_decode(const struct int_hop_t *wire,
                                  struct int_hop_host *out) {
    out->switch_id        = ntohs(wire->switch_id);
    out->ingress_port     = ntohs(wire->ingress_port);
    out->egress_port      = ntohs(wire->egress_port);
    out->qdepth           = ntohl(wire->qdepth);
    out->egress_tstamp_us = u48_unpack(wire->egress_tstamp_us);
    out->tx_byte_count    = u48_unpack(wire->tx_byte_count);
    out->link_bps         = ntohl(wire->link_bps);
}

/* Build a data UDP payload at `dst`. Returns the byte count written
 * (= SHIM_SIZE + DATA_PAYLOAD_SIZE + padding for a 0-hop data packet). */
static inline size_t data_packet_pack(uint8_t *dst, uint32_t seq,
                                      uint64_t send_ts_us, size_t padding) {
    struct shim_t *s = (struct shim_t *)dst;
    s->flags = 0;
    s->hop_count = 0;
    s->max_hops = MAX_HOPS;
    s->reserved = 0;

    struct data_payload_t *p = (struct data_payload_t *)(dst + SHIM_SIZE);
    p->seq        = htonl(seq);
    p->send_ts_us = htobe64(send_ts_us);
    p->reserved   = 0;

    if (padding > 0) {
        memset(dst + SHIM_SIZE + DATA_PAYLOAD_SIZE, 0, padding);
    }
    return SHIM_SIZE + DATA_PAYLOAD_SIZE + padding;
}

#endif  /* _INTCC_PACKET_FORMAT_H_ */
