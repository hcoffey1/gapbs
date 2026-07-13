# See LICENSE.txt for license details.
CXX=$(CUSTOM_CC)

#CXX_FLAGS += -std=c++11 -O2 -Wall# -fno-inline
#ZRAY_LINK_FLAGS = -std=c++11 -Wall# -fno-inline
CXX_FLAGS += -std=c++11 -O3 -Wall #-Xclang -no-opaque-pointers
ZRAY_LINK_FLAGS = -std=c++11 -O3 #-Wall -Xclang -no-opaque-pointers
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

# ZRAY counter-placement optimization flags (opt -tool_pass). Overridable per build so the
# opt-study driver can sweep the three configurations. Defaults match the pass defaults.
#   baseline (one counter/BB): POSTDOMSET=false LOOPHOIST=false
#   post-dom:                   POSTDOMSET=true  LOOPHOIST=false
#   post-dom + hoist:           POSTDOMSET=true  LOOPHOIST=true
POSTDOMSET ?= true
LOOPHOIST  ?= true

# Counter-placement strategy: "zray" (default; uses POSTDOMSET/LOOPHOIST above)
# or "mst" (LLVM PGO minimum-spanning-tree edge placement; POSTDOMSET/LOOPHOIST
# are ignored). The MST arm additionally emits "$(ZRAY_LOGFILE).mst".
PLACEMENT ?= zray

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
	cd $*; $(CUSTOM_OPT) -enable-new-pm=0 -O3 -mem2reg -load $(ZRAY_BIN_PATH)/tool.so -tool_pass --full-scan --postdomset=$(POSTDOMSET) --loophoist=$(LOOPHOIST) --placement=$(PLACEMENT) ../$< -o ../$@ 2> ../src/$*_static.log

$(IR_BASE) : src/%.ll : src/%.cc src/*.h
	$(CXX) $(CXX_FLAGS) -S -emit-llvm $< -o $@
	#$(CXX) $(CXX_FLAGS) -I/usr/include/clang/15/include -S -emit-llvm $< -o $@

# Uninstrumented reference build: same -O2 -mem2reg pipeline but NO tool_pass and
# NO tool_dyn runtime, so it carries zero instrumentation overhead. Used as the
# execution-time baseline for slowdown comparison. Binary: <kernel>/<kernel>_uninstr.
UNINSTR = $(addsuffix _uninstr,$(SUITE))
.PHONY: $(UNINSTR)
$(UNINSTR) : %_uninstr : src/%_uninstr.ll
	mkdir -p $*
	cd $*; $(CXX) -L/usr/lib/llvm-14/lib/ $(ZRAY_LINK_FLAGS) ../$< -o $*_uninstr

src/%_uninstr.ll : src/%.ll
	$(CUSTOM_OPT) -enable-new-pm=0 -O3 -mem2reg $< -o $@

# Testing
include test/test.mk

# Benchmark Automation
include benchmark/bench.mk


.PHONY: clean
clean:
	rm -rf $(SUITE) test/out/* src/*.ll src/*_static.log
