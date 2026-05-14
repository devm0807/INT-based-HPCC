/*
 * hpcc_sender.c — C reimplementation of sender/hpcc_sender.py.
 *
 * Same wire format, same control loop, same CSV schema. The goal is to
 * push significantly higher pps than the Python sender (which caps
 * around 5 kpps due to GIL + time.sleep jitter) so we can run E1 at
 * 1 Gbps and confirm the BDP-hypothesis discussion in
 * docs/discussion.md.
 *
 * Threading:
 *   - main thread: ack listener (recvmmsg in a tight loop)
 *   - sender path: send is inline on a busy loop with usleep pacing
 *
 * Synchronization: single big mutex around controller state. Plenty of
 * room for finer-grained locking later; not needed at MTU-sized
 * packets / 100 kpps targets.
 *
 * Build:
 *   make
 *
 * Run inside the dev container (with reflector on the receiver):
 *   ./hpcc_sender 10.0.0.10 --duration 30 --log /tmp/c_hpcc.csv \
 *       --w-init 8 --padding 1400 --base-rtt 0.006
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <math.h>
#include <netinet/in.h>
#include <netinet/ip.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include "packet_format.h"

/* --- constants & defaults --- */
#define INFLIGHT_MAX  4096          /* hard cap; W_max << this */
#define HOP_TABLE_SZ  256           /* tiny per-(switch,port) cache */
#define EVENT_BUF_SZ  (1 << 22)     /* 4 M events */

static double DEFAULT_ETA = 0.99;
static double DEFAULT_WAI = 3.0;
static double DEFAULT_TAU = 0.05;
static int    DEFAULT_WINIT = 8;
static int    DEFAULT_WMAX  = 64;
static int    DEFAULT_WMIN  = 1;
#define QDEPTH_PKT_BYTES 1500

/* --- types --- */
struct inflight_entry {
    uint32_t seq;          /* 0xFFFFFFFF means "slot empty" */
    uint64_t send_ts_us;
    double   w_ref_snap;
    uint32_t incarnation_snap;
    int      rtx;
};

struct hop_key {
    uint16_t switch_id;
    uint16_t egress_port;
    bool     valid;
    uint64_t last_tstamp_us;
    uint64_t last_tx_bytes;
};

/* Single big event-log buffer; flushed to CSV at shutdown. */
struct event {
    uint64_t ts_us;
    char     kind;          /* 'S' send, 'X' rtx, 'A' ack, 'U' update, 'R' rto */
    uint32_t seq;
    uint64_t rtt_us;
    uint32_t hop_count;
    double   w;
    double   w_ref;
    uint32_t incarnation;
    double   u;
    double   u_smoothed;
    int64_t  cum_acked;
    uint32_t in_flight;
};

struct sender_state {
    /* config */
    const char *receiver_ip;
    uint16_t    data_port;
    uint16_t    ack_port;
    double      duration_s;
    int64_t     max_packets;        /* -1 = unlimited */
    size_t      padding;
    double      base_rtt_s;
    double      rto_s;
    double      eta;
    double      w_ai;
    double      tau_over_t;
    int         w_min;
    int         w_max;

    /* controller state */
    double   w;
    double   w_ref;
    uint32_t incarnation;
    double   u_smoothed;
    int64_t  last_update_seq;
    uint64_t last_update_us;

    /* in-flight tracking (hash table by seq % INFLIGHT_MAX) */
    struct inflight_entry inflight[INFLIGHT_MAX];
    int      in_flight_count;
    uint32_t next_seq;
    int64_t  cum_acked_seq;

    /* per-(switch,port) tx_byte_count anchor */
    struct hop_key hops[HOP_TABLE_SZ];

    /* I/O */
    int      sock;
    struct sockaddr_in dst;

    /* events */
    struct event *events;
    size_t        event_cap;
    atomic_size_t event_n;

    /* control */
    atomic_bool stop;
    pthread_mutex_t lock;
};

/* --- utilities --- */
static uint64_t now_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (uint64_t)ts.tv_sec * 1000000ULL + (uint64_t)(ts.tv_nsec / 1000);
}

static uint64_t now_us_monotonic(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000ULL + (uint64_t)(ts.tv_nsec / 1000);
}

static double clampd(double v, double lo, double hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

static void log_event(struct sender_state *s, char kind, uint32_t seq,
                       uint64_t rtt_us, uint32_t hop_count, double u) {
    size_t i = atomic_fetch_add(&s->event_n, 1);
    if (i >= s->event_cap) return;
    struct event *e = &s->events[i];
    e->ts_us       = now_us();
    e->kind        = kind;
    e->seq         = seq;
    e->rtt_us      = rtt_us;
    e->hop_count   = hop_count;
    e->w           = s->w;
    e->w_ref       = s->w_ref;
    e->incarnation = s->incarnation;
    e->u           = u;
    e->u_smoothed  = s->u_smoothed;
    e->cum_acked   = s->cum_acked_seq;
    e->in_flight   = (uint32_t)s->in_flight_count;
}

/* --- hop table (linear probe; flush is via switch_id 0 sentinel) --- */
static struct hop_key *hop_lookup(struct sender_state *s, uint16_t swid,
                                  uint16_t port, bool create) {
    uint32_t h = ((uint32_t)swid * 31 + port) & (HOP_TABLE_SZ - 1);
    for (int i = 0; i < HOP_TABLE_SZ; i++) {
        struct hop_key *k = &s->hops[(h + i) & (HOP_TABLE_SZ - 1)];
        if (k->valid && k->switch_id == swid && k->egress_port == port) return k;
        if (!k->valid) {
            if (!create) return NULL;
            k->switch_id = swid;
            k->egress_port = port;
            k->valid = false;  /* not yet primed */
            return k;
        }
    }
    return NULL;
}

/* --- in-flight tracking --- */
static int inflight_idx(uint32_t seq) { return (int)(seq % INFLIGHT_MAX); }

static void inflight_insert(struct sender_state *s, uint32_t seq,
                            uint64_t send_ts) {
    int i = inflight_idx(seq);
    s->inflight[i].seq              = seq;
    s->inflight[i].send_ts_us       = send_ts;
    s->inflight[i].w_ref_snap       = s->w_ref;
    s->inflight[i].incarnation_snap = s->incarnation;
    s->inflight[i].rtx              = 0;
    s->in_flight_count++;
}

static struct inflight_entry *inflight_get(struct sender_state *s,
                                           uint32_t seq) {
    int i = inflight_idx(seq);
    if (s->inflight[i].seq == seq) return &s->inflight[i];
    return NULL;
}

static void inflight_clear_up_to(struct sender_state *s, uint32_t up_to) {
    /* Iterate seqs from cum_acked+1 up to up_to inclusive. */
    for (int64_t k = s->cum_acked_seq + 1; k <= (int64_t)up_to; k++) {
        int i = inflight_idx((uint32_t)k);
        if (s->inflight[i].seq == (uint32_t)k) {
            s->inflight[i].seq = 0xFFFFFFFFU;
            s->in_flight_count--;
        }
    }
}

/* --- send path --- */
static void send_one(struct sender_state *s, uint32_t seq) {
    uint8_t buf[2048];
    size_t n = data_packet_pack(buf, seq, now_us(), s->padding);
    ssize_t r = sendto(s->sock, buf, n, 0,
                       (struct sockaddr *)&s->dst, sizeof(s->dst));
    if (r < 0) {
        if (errno != EAGAIN && errno != EINTR) {
            fprintf(stderr, "sendto failed: %s\n", strerror(errno));
        }
        return;
    }
    uint64_t t = now_us();
    struct inflight_entry *e = inflight_get(s, seq);
    if (e && e->seq == seq) {
        e->send_ts_us = t;
        e->rtx++;
        log_event(s, 'X', seq, 0, 0, 0.0);
    } else {
        inflight_insert(s, seq, t);
        log_event(s, 'S', seq, 0, 0, 0.0);
    }
}

/* --- HPCC controller --- */
static double compute_u(struct sender_state *s,
                        const struct int_hop_host *hops, int n_hops) {
    double u_max = 0.0;
    for (int i = 0; i < n_hops; i++) {
        const struct int_hop_host *h = &hops[i];
        if (h->link_bps == 0) continue;
        struct hop_key *k = hop_lookup(s, h->switch_id, h->egress_port, true);
        if (!k) continue;
        if (!k->valid) {
            k->last_tstamp_us = h->egress_tstamp_us;
            k->last_tx_bytes  = h->tx_byte_count;
            k->valid          = true;
            continue;
        }
        int64_t dt = (int64_t)h->egress_tstamp_us - (int64_t)k->last_tstamp_us;
        int64_t db = (int64_t)h->tx_byte_count - (int64_t)k->last_tx_bytes;
        k->last_tstamp_us = h->egress_tstamp_us;
        k->last_tx_bytes  = h->tx_byte_count;
        if (dt <= 0 || db < 0) continue;
        double tx_rate_bps = ((double)db * 8.0 * 1e6) / (double)dt;
        double b_bytes = ((double)h->link_bps * s->base_rtt_s) / 8.0;
        if (b_bytes <= 0) continue;
        double qdepth_bytes = (double)h->qdepth * (double)QDEPTH_PKT_BYTES;
        double u = qdepth_bytes / b_bytes + tx_rate_bps / (double)h->link_bps;
        if (u > u_max) u_max = u;
    }
    return u_max;
}

static void maybe_update_window(struct sender_state *s) {
    uint64_t now_mono = now_us_monotonic();
    if (s->cum_acked_seq < s->last_update_seq + (int64_t)fmax(s->w_ref, 1)) return;
    if ((double)(now_mono - s->last_update_us) / 1e6 < s->base_rtt_s) return;

    double u_eff = s->u_smoothed < 1e-3 ? 1e-3 : s->u_smoothed;
    double w_new = s->w_ref / (u_eff / s->eta) + s->w_ai;
    w_new = clampd(w_new, (double)s->w_min, (double)s->w_max);
    if (w_new < s->w_ref) s->incarnation++;
    s->w_ref = w_new;
    s->w     = w_new;
    s->last_update_seq = s->cum_acked_seq;
    s->last_update_us  = now_mono;
    log_event(s, 'U', 0, 0, 0, u_eff);
}

/* --- ack listener thread --- */
static void *ack_thread(void *arg) {
    struct sender_state *s = (struct sender_state *)arg;
    uint8_t buf[4096];
    struct timeval tv = { .tv_sec = 0, .tv_usec = 100000 };
    setsockopt(s->sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    while (!atomic_load(&s->stop)) {
        ssize_t r = recv(s->sock, buf, sizeof(buf), 0);
        if (r < (ssize_t)(SHIM_SIZE + ACK_PAYLOAD_SIZE)) continue;

        struct shim_t *shim = (struct shim_t *)buf;
        uint8_t hop_count = shim->hop_count;
        if (hop_count > MAX_HOPS) continue;

        size_t hops_size = (size_t)hop_count * INT_HOP_SIZE;
        if ((size_t)r < SHIM_SIZE + hops_size + ACK_PAYLOAD_SIZE) continue;

        /* Decode hops to host order. */
        struct int_hop_host hops[MAX_HOPS];
        for (int i = 0; i < hop_count; i++) {
            int_hop_decode((const struct int_hop_t *)
                           (buf + SHIM_SIZE + i * INT_HOP_SIZE),
                           &hops[i]);
        }
        struct ack_payload_t *ack = (struct ack_payload_t *)
                                    (buf + SHIM_SIZE + hops_size);
        uint32_t ack_seq = ntohl(ack->ack_seq);

        pthread_mutex_lock(&s->lock);
        struct inflight_entry *e = inflight_get(s, ack_seq);
        uint64_t rtt_us = 0;
        if (e && e->seq == ack_seq) {
            rtt_us = now_us() - e->send_ts_us;
            inflight_clear_up_to(s, ack_seq);
            s->cum_acked_seq = ack_seq;
        } else if ((int64_t)ack_seq <= s->cum_acked_seq) {
            pthread_mutex_unlock(&s->lock);
            continue;  /* duplicate */
        }

        double u = compute_u(s, hops, hop_count);
        if (u > 0) {
            s->u_smoothed = (1.0 - s->tau_over_t) * s->u_smoothed
                          + s->tau_over_t * u;
        }
        log_event(s, 'A', ack_seq, rtt_us, hop_count, u);
        maybe_update_window(s);
        pthread_mutex_unlock(&s->lock);
    }
    return NULL;
}

/* --- RTO monitor: cheap, run inline once per main-loop pass --- */
static void rto_check(struct sender_state *s) {
    uint64_t now = now_us();
    uint64_t rto_us = (uint64_t)(s->rto_s * 1e6);
    bool fired = false;
    for (int i = 0; i < INFLIGHT_MAX; i++) {
        struct inflight_entry *e = &s->inflight[i];
        if (e->seq == 0xFFFFFFFFU) continue;
        if (now - e->send_ts_us < rto_us) continue;
        fired = true;
        break;
    }
    if (!fired) return;

    s->w_ref = (s->w_ref / 2.0 < s->w_min) ? s->w_min : (s->w_ref / 2.0);
    s->w     = s->w_ref;
    s->incarnation++;
    log_event(s, 'R', 0, 0, 0, 0.0);

    /* Re-send everything still in flight (go-back-N). */
    for (int i = 0; i < INFLIGHT_MAX; i++) {
        struct inflight_entry *e = &s->inflight[i];
        if (e->seq == 0xFFFFFFFFU) continue;
        send_one(s, e->seq);
    }
}

/* --- CSV dump --- */
static void dump_csv(struct sender_state *s, const char *path) {
    FILE *f = fopen(path, "w");
    if (!f) {
        fprintf(stderr, "failed to open %s\n", path);
        return;
    }
    fputs("ts_us,event,seq,rtt_us,hop_count,w,w_ref,incarnation,"
          "u,u_smoothed,cum_acked,in_flight\n", f);
    static const char *names[256];
    names['S'] = "send";  names['X'] = "rtx"; names['A'] = "ack";
    names['U'] = "update"; names['R'] = "rto";
    size_t n = atomic_load(&s->event_n);
    if (n > s->event_cap) n = s->event_cap;
    for (size_t i = 0; i < n; i++) {
        struct event *e = &s->events[i];
        const char *nm = names[(uint8_t)e->kind] ? names[(uint8_t)e->kind] : "?";
        fprintf(f, "%" PRIu64 ",%s,%u,%" PRIu64 ",%u,%.3f,%.3f,%u,%.5f,%.5f,"
                "%" PRId64 ",%u\n",
                e->ts_us, nm,
                e->kind == 'U' || e->kind == 'R' ? 0xFFFFFFFFU : e->seq,
                e->rtt_us, e->hop_count, e->w, e->w_ref, e->incarnation,
                e->u, e->u_smoothed, e->cum_acked, e->in_flight);
    }
    fclose(f);
}

/* --- main --- */
static void usage(const char *prog) {
    fprintf(stderr,
            "Usage: %s RECEIVER_IP [--duration SEC] [--log PATH] "
            "[--w-init N] [--w-max N] [--eta F] [--w-ai F] [--tau F] "
            "[--padding N] [--max-packets N] [--base-rtt SEC] "
            "[--data-port P] [--ack-port P]\n",
            prog);
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(argv[0]); return 1; }
    struct sender_state s = {0};
    s.receiver_ip = argv[1];
    s.data_port   = DATA_PORT;
    s.ack_port    = ACK_PORT;
    s.duration_s  = 30.0;
    s.max_packets = -1;
    s.padding     = 1400;
    s.base_rtt_s  = 0.006;
    s.w           = (double)DEFAULT_WINIT;
    s.w_ref       = (double)DEFAULT_WINIT;
    s.eta         = DEFAULT_ETA;
    s.w_ai        = DEFAULT_WAI;
    s.tau_over_t  = DEFAULT_TAU;
    s.w_min       = DEFAULT_WMIN;
    s.w_max       = DEFAULT_WMAX;
    s.u_smoothed  = 1.0;
    s.cum_acked_seq   = -1;
    s.last_update_seq = -1;
    const char *log_path = "/tmp/c_hpcc.csv";

    static struct option opts[] = {
        {"duration",    required_argument, 0, 'd'},
        {"log",         required_argument, 0, 'l'},
        {"w-init",      required_argument, 0,  1 },
        {"w-max",       required_argument, 0,  2 },
        {"eta",         required_argument, 0,  3 },
        {"w-ai",        required_argument, 0,  4 },
        {"tau",         required_argument, 0,  5 },
        {"padding",     required_argument, 0,  6 },
        {"max-packets", required_argument, 0,  7 },
        {"base-rtt",    required_argument, 0,  8 },
        {"data-port",   required_argument, 0,  9 },
        {"ack-port",    required_argument, 0, 10 },
        {0, 0, 0, 0},
    };
    int c;
    while ((c = getopt_long(argc - 1, argv + 1, "d:l:", opts, NULL)) != -1) {
        switch (c) {
        case 'd':  s.duration_s = atof(optarg); break;
        case 'l':  log_path = optarg; break;
        case 1:    s.w = s.w_ref = atof(optarg); break;
        case 2:    s.w_max = atoi(optarg); break;
        case 3:    s.eta = atof(optarg); break;
        case 4:    s.w_ai = atof(optarg); break;
        case 5:    s.tau_over_t = atof(optarg); break;
        case 6:    s.padding = (size_t)atoi(optarg); break;
        case 7:    s.max_packets = atoll(optarg); break;
        case 8:    s.base_rtt_s = atof(optarg); break;
        case 9:    s.data_port = (uint16_t)atoi(optarg); break;
        case 10:   s.ack_port  = (uint16_t)atoi(optarg); break;
        default:   usage(argv[0]); return 1;
        }
    }
    s.rto_s = 3.0 * s.base_rtt_s;
    if (s.rto_s < 0.020) s.rto_s = 0.020;
    if (s.rto_s > 1.0)   s.rto_s = 1.0;

    /* Events buffer. */
    s.event_cap = EVENT_BUF_SZ;
    s.events = calloc(s.event_cap, sizeof(struct event));
    if (!s.events) { perror("calloc events"); return 1; }

    /* In-flight slots: mark all empty. */
    for (int i = 0; i < INFLIGHT_MAX; i++) {
        s.inflight[i].seq = 0xFFFFFFFFU;
    }

    /* Socket. */
    s.sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (s.sock < 0) { perror("socket"); return 1; }
    int reuse = 1;
    setsockopt(s.sock, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
    int tos = 0x02;  /* ECT(0) */
    setsockopt(s.sock, IPPROTO_IP, IP_TOS, &tos, sizeof(tos));
    struct sockaddr_in src = {.sin_family = AF_INET,
                              .sin_addr.s_addr = htonl(INADDR_ANY),
                              .sin_port = htons(s.ack_port)};
    if (bind(s.sock, (struct sockaddr *)&src, sizeof(src)) < 0) {
        perror("bind"); return 1;
    }
    s.dst.sin_family = AF_INET;
    s.dst.sin_port   = htons(s.data_port);
    if (inet_pton(AF_INET, s.receiver_ip, &s.dst.sin_addr) != 1) {
        fprintf(stderr, "bad receiver IP %s\n", s.receiver_ip); return 1;
    }

    pthread_mutex_init(&s.lock, NULL);
    atomic_store(&s.stop, false);
    s.last_update_us = now_us_monotonic();

    pthread_t ack_t;
    pthread_create(&ack_t, NULL, ack_thread, &s);

    /* Combined send + occasional RTO check. */
    uint64_t end_us = now_us_monotonic() + (uint64_t)(s.duration_s * 1e6);
    uint64_t next_rto_check = now_us_monotonic() + (uint64_t)(s.rto_s * 1e6 / 4);
    while (now_us_monotonic() < end_us) {
        pthread_mutex_lock(&s.lock);
        int w_int = (int)s.w;
        while (s.in_flight_count < w_int &&
               (s.max_packets < 0 || (int64_t)s.next_seq < s.max_packets)) {
            send_one(&s, s.next_seq);
            s.next_seq++;
        }
        bool done = (s.max_packets >= 0 &&
                     (int64_t)s.next_seq >= s.max_packets &&
                     s.in_flight_count == 0);
        if (now_us_monotonic() >= next_rto_check) {
            rto_check(&s);
            next_rto_check = now_us_monotonic() + (uint64_t)(s.rto_s * 1e6 / 4);
        }
        pthread_mutex_unlock(&s.lock);
        if (done) break;
        struct timespec ts = { 0, 100 * 1000 };
        nanosleep(&ts, NULL);
    }

    /* Drain. */
    usleep((useconds_t)(s.rto_s * 2e6));
    atomic_store(&s.stop, true);
    pthread_join(ack_t, NULL);

    dump_csv(&s, log_path);
    printf("sent=%u acked=%" PRId64 " final_w=%.3f u_smoothed=%.5f "
           "incarnations=%u events=%zu log=%s\n",
           s.next_seq, s.cum_acked_seq + 1, s.w, s.u_smoothed,
           s.incarnation, atomic_load(&s.event_n), log_path);
    free(s.events);
    return 0;
}
