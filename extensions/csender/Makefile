# C HPCC sender build.
#
# Run inside the dev container (it has gcc):
#   make -C extensions/csender

CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -Wno-unused-parameter -pthread -std=gnu11
LDFLAGS ?= -pthread -lm

TARGETS := hpcc_sender

all: $(TARGETS)

hpcc_sender: hpcc_sender.c packet_format.h
	$(CC) $(CFLAGS) -o $@ hpcc_sender.c $(LDFLAGS)

clean:
	rm -f $(TARGETS)

.PHONY: all clean smoke

# Quick smoke: requires an already-running reflector on the receiver.
smoke: hpcc_sender
	@echo "run from the dev container with a topology + tables up:"
	@echo "  ./extensions/csender/hpcc_sender 10.0.0.10 --duration 5 \\"
	@echo "      --log /tmp/c_hpcc_smoke.csv --padding 1400 --base-rtt 0.006"
