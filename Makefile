# See LICENSE.txt for license details.
CXX=$(CUSTOM_CC)

#CXX_FLAGS += -std=c++11 -O2 -Wall# -fno-inline
#ZRAY_LINK_FLAGS = -std=c++11 -Wall# -fno-inline
CXX_FLAGS += -std=c++11 -O2 -Wall #-Xclang -no-opaque-pointers
ZRAY_LINK_FLAGS = -std=c++11 -O2 #-Wall -Xclang -no-opaque-pointers
PAR_FLAG = -fopenmp=libomp

ifneq (,$(findstring icpc,$(CXX)))
	PAR_FLAG = -openmp
endif

ifneq (,$(findstring sunCC,$(CXX)))
	CXX_FLAGS = -std=c++11 -xO3 -m64 -xtarget=native
	PAR_FLAG = -xopenmp
endif

ifneq ($(SERIAL), 1)
	CXX_FLAGS += $(PAR_FLAG)
	ZRAY_LINK_FLAGS += $(PAR_FLAG)
endif

KERNELS = bc bfs cc cc_sv pr pr_spmv sssp tc
SUITE = $(KERNELS) converter

.PHONY: all
all: $(SUITE)

# Explicit IR target lists so the rules below are static pattern rules rather than
# match-anything/implicit rules. GNU make will not chain implicit pattern rules three
# deep (src/%.cc -> %.ll -> %_optimized.ll -> %_linked.ll -> binary) for intermediates
# it hasn't already built, so `make <kernel>` fails with "No rule to make target
# 'src/%_linked.ll'". Enumerating the targets makes each IR file a first-class goal.
IR_BASE   = $(addprefix src/,$(addsuffix .ll,$(SUITE)))
IR_OPT    = $(addprefix src/,$(addsuffix _optimized.ll,$(SUITE)))
IR_LINKED = $(addprefix src/,$(addsuffix _linked.ll,$(SUITE)))
.SECONDARY: $(IR_BASE) $(IR_OPT) $(IR_LINKED)

$(SUITE) : % : src/%_linked.ll
	cd $*; $(CXX) -L/usr/lib/llvm-14/lib/ $(ZRAY_LINK_FLAGS) ../$< -o $@

$(IR_LINKED) : src/%_linked.ll : src/%_optimized.ll $(ZRAY_BIN_PATH)/tool_dyn.ll
	$(CUSTOM_LINK) $^ -S -o $@

$(IR_OPT) : src/%_optimized.ll : src/%.ll
	mkdir -p $*
	cd $*; $(CUSTOM_OPT) -enable-new-pm=0 -O2 -mem2reg -load $(ZRAY_BIN_PATH)/tool.so -tool_pass --full-scan ../$< -o ../$@

$(IR_BASE) : src/%.ll : src/%.cc src/*.h
	$(CXX) $(CXX_FLAGS) -S -emit-llvm $< -o $@
	#$(CXX) $(CXX_FLAGS) -I/usr/include/clang/15/include -S -emit-llvm $< -o $@

# Testing
include test/test.mk

# Benchmark Automation
include benchmark/bench.mk


.PHONY: clean
clean:
	rm -rf $(SUITE) test/out/* src/*.ll
