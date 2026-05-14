# qsnap build target.
#
# Compiles extensions/qsnap/hpcc_qsnap.p4 -> /workspace/build/hpcc_qsnap.json.
# Then a controller invocation can load it the same way as the core hpcc.p4
# (register names use HpccQsnapEgress.* — see controller note below).

P4C    = p4c-bm2-ss --target bmv2 --arch v1model --p4v 16
SRC    = hpcc_qsnap.p4
BUILD  = /workspace/build/hpcc_qsnap.json

all: $(BUILD)

$(BUILD): $(SRC) /workspace/p4src/common/headers.p4 /workspace/p4src/common/parsers.p4
	mkdir -p /workspace/build
	$(P4C) -o $(BUILD) $(SRC)

.PHONY: all clean
clean:
	rm -f $(BUILD)
