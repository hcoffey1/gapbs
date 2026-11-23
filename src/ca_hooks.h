// Arena routing for custom_alloc builds (-DCUSTOM_ALLOC) and VA-range logging
// (-DCA_LOG, implied by CUSTOM_ALLOC). Without either flag this is plain new[].

#ifndef CA_HOOKS_H_
#define CA_HOOKS_H_

#include <cstddef>
#include <cstdio>
#include <new>

// Arena (-1 = default allocator) for pvector / GenIndex allocations.
static int ca_cur __attribute__((unused)) = -1;
// Arenas for the graph arrays the reader allocates (set before MakeGraph).
static int ca_out_neigh = -1, ca_in_neigh = -1, ca_index __attribute__((unused)) = -1;

#ifdef CUSTOM_ALLOC
#include "custom_alloc.h"
#ifndef CA_LOG
#define CA_LOG
#endif

// Memory is released with delete[]: valid because the binary links -ljemalloc
// (so free is jemalloc's) and every T here is trivially destructible.
template <typename T>
T* ca_new(size_t n, int arena = ca_cur) {
  if (arena < 0)
    return new T[n];
  T* p = static_cast<T*>(ca_malloc(arena, n * sizeof(T)));
  if (!p)
    throw std::bad_alloc();
  return p;
}
#else
template <typename T>
T* ca_new(size_t n, int = -1) { return new T[n]; }
#endif

// One "[ca] <name> <start> <bytes>" line per structure so plots can label VA.
inline void ca_log(const char* name, const void* p, size_t bytes) {
#ifdef CA_LOG
  fprintf(stderr, "[ca] %s %p %zu\n", name, p, bytes);
#else
  (void)name; (void)p; (void)bytes;
#endif
}

#endif  // CA_HOOKS_H_
