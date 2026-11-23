# See LICENSE.txt for license details.

CXX_FLAGS += -std=c++11 -O3 -Wall
PAR_FLAG = -fopenmp

ifneq (,$(findstring icpc,$(CXX)))
	PAR_FLAG = -openmp
endif

ifneq (,$(findstring sunCC,$(CXX)))
	CXX_FLAGS = -std=c++11 -xO3 -m64 -xtarget=native
	PAR_FLAG = -xopenmp
endif

ifneq ($(SERIAL), 1)
	CXX_FLAGS += $(PAR_FLAG)
endif

KERNELS = bc bfs cc cc_sv pr pr_spmv sssp tc
SUITE = $(KERNELS) converter

.PHONY: all
all: $(SUITE)

% : src/%.cc src/*.h
	$(CXX) $(CXX_FLAGS) $< -o $@

# Allocator variants of pr (stock targets above are unchanged):
#   pr_glibc: default allocator, logs structure VA ranges ([ca] lines on stderr)
#   pr_ca:    custom_alloc arenas; -ljemalloc on this link line makes free/delete[] jemalloc's
CUSTOM_ALLOC_DIR ?= $(HOME)/working/custom_alloc
pr_glibc : src/pr.cc src/*.h
	$(CXX) $(CXX_FLAGS) -DCA_LOG $< -o $@
pr_ca : src/pr.cc src/*.h $(CUSTOM_ALLOC_DIR)/libcustom_alloc.so
	$(CXX) $(CXX_FLAGS) -DCUSTOM_ALLOC -I$(CUSTOM_ALLOC_DIR)/include $< -o $@ \
	  -L$(CUSTOM_ALLOC_DIR) -Wl,-rpath,$(CUSTOM_ALLOC_DIR) -lcustom_alloc -ljemalloc -lnuma

# Testing
include test/test.mk

# Benchmark Automation
include benchmark/bench.mk


.PHONY: clean
clean:
	rm -f $(SUITE) pr_glibc pr_ca test/out/*
