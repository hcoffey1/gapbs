// Copyright (c) 2015, The Regents of the University of California (Regents)
// See LICENSE.txt for license details

#include <algorithm>
#include <cinttypes>
#include <iostream>
#include <unordered_map>
#include <vector>

#include "benchmark.h"
#include "bitmap.h"
#include "builder.h"
#include "command_line.h"
#include "graph.h"
#include "pvector.h"

#ifdef ZRAY_STUDY
#include "zray.h"
extern "C" void zray_study_finalize();
#else
#define ZRAY_BEGIN(group_id) ((void)0)
#define ZRAY_END(group_id) ((void)0)
#endif

// ZRay regions of interest. One nesting level is instrumented per build,
// selected with -DZRAY_ROI=<level>; markers stay in the serial code and every
// OpenMP team thread reports its share of the enclosed parallel loops.
//   ZRAY_ROI_LOOPS  group 1 = hooking loop, group 2 = compression loop
//   ZRAY_ROI_FUNC   group 3 = one ShiloachVishkin call
//   ZRAY_ROI_APP    group 4 = the application from graph load to exit
#define ZRAY_ROI_LOOPS 1
#define ZRAY_ROI_FUNC 2
#define ZRAY_ROI_APP 3
#ifndef ZRAY_ROI
#define ZRAY_ROI ZRAY_ROI_LOOPS
#endif
#if ZRAY_ROI == ZRAY_ROI_LOOPS
#define ROI_HOOK(marker) marker(1)
#define ROI_COMPRESS(marker) marker(2)
#else
#define ROI_HOOK(marker) ((void)0)
#define ROI_COMPRESS(marker) ((void)0)
#endif
#if ZRAY_ROI == ZRAY_ROI_FUNC
#define ROI_FUNC(marker) marker(3)
#else
#define ROI_FUNC(marker) ((void)0)
#endif
#if ZRAY_ROI == ZRAY_ROI_APP
#define ROI_APP(marker) marker(4)
#else
#define ROI_APP(marker) ((void)0)
#endif

// The hooking loop's work per vertex is its out-degree, so the schedule matters
// on skewed graphs. CC_SV_DYNAMIC selects dynamic chunks of CC_SV_CHUNK
// vertices (default 16384); otherwise the loop keeps the static default.
#ifndef CC_SV_CHUNK
#define CC_SV_CHUNK 16384
#endif
#define CC_SV_PRAGMA(x) _Pragma(#x)
#ifdef CC_SV_DYNAMIC
#define CC_SV_HOOK_LOOP CC_SV_PRAGMA(omp parallel for schedule(dynamic, CC_SV_CHUNK))
#else
#define CC_SV_HOOK_LOOP _Pragma("omp parallel for")
#endif


/*
GAP Benchmark Suite
Kernel: Connected Components (CC)
Author: Scott Beamer

Will return comp array labelling each vertex with a connected component ID

This CC implementation makes use of the Shiloach-Vishkin [2] algorithm with
implementation optimizations from Bader et al. [1]. Michael Sutton contributed
a fix for directed graphs using the min-max swap from [3], and it also produces
more consistent performance for undirected graphs.

[1] David A Bader, Guojing Cong, and John Feo. "On the architectural
    requirements for efficient execution of graph algorithms." International
    Conference on Parallel Processing, Jul 2005.

[2] Yossi Shiloach and Uzi Vishkin. "An o(logn) parallel connectivity algorithm"
    Journal of Algorithms, 3(1):57–67, 1982.

[3] Kishore Kothapalli, Jyothish Soman, and P. J. Narayanan. "Fast GPU
    algorithms for graph connectivity." Workshop on Large Scale Parallel
    Processing, 2010.
*/


using namespace std;


// The hooking condition (comp_u < comp_v) may not coincide with the edge's
// direction, so we use a min-max swap such that lower component IDs propagate
// independent of the edge's direction.
pvector<NodeID> ShiloachVishkin(const Graph &g) {
  ROI_FUNC(ZRAY_BEGIN);
  pvector<NodeID> comp(g.num_nodes());
  #pragma omp parallel for
  for (NodeID n=0; n < g.num_nodes(); n++)
    comp[n] = n;
  bool change = true;
  int num_iter = 0;
  while (change) {
    change = false;
    num_iter++;
    ROI_HOOK(ZRAY_BEGIN);
    CC_SV_HOOK_LOOP
    for (NodeID u=0; u < g.num_nodes(); u++) {
      for (NodeID v : g.out_neigh(u)) {
        NodeID comp_u = comp[u];
        NodeID comp_v = comp[v];
        if (comp_u == comp_v) continue;
        // Hooking condition so lower component ID wins independent of direction
        NodeID high_comp = comp_u > comp_v ? comp_u : comp_v;
        NodeID low_comp = comp_u + (comp_v - high_comp);
        if (high_comp == comp[high_comp]) {
          change = true;
          comp[high_comp] = low_comp;
        }
      }
    }
    ROI_HOOK(ZRAY_END);
    ROI_COMPRESS(ZRAY_BEGIN);
    #pragma omp parallel for
    for (NodeID n=0; n < g.num_nodes(); n++) {
      while (comp[n] != comp[comp[n]]) {
        comp[n] = comp[comp[n]];
      }
    }
    ROI_COMPRESS(ZRAY_END);
  }
  cout << "Shiloach-Vishkin took " << num_iter << " iterations" << endl;
  ROI_FUNC(ZRAY_END);
  return comp;
}


void PrintCompStats(const Graph &g, const pvector<NodeID> &comp) {
  cout << endl;
  unordered_map<NodeID, NodeID> count;
  for (NodeID comp_i : comp)
    count[comp_i] += 1;
  int k = 5;
  vector<pair<NodeID, NodeID>> count_vector;
  count_vector.reserve(count.size());
  for (auto kvp : count)
    count_vector.push_back(kvp);
  vector<pair<NodeID, NodeID>> top_k = TopK(count_vector, k);
  k = min(k, static_cast<int>(top_k.size()));
  cout << k << " biggest clusters" << endl;
  for (auto kvp : top_k)
    cout << kvp.second << ":" << kvp.first << endl;
  cout << "There are " << count.size() << " components" << endl;
}


// Verifies CC result by performing a BFS from a vertex in each component
// - Asserts search does not reach a vertex with a different component label
// - If the graph is directed, it performs the search as if it was undirected
// - Asserts every vertex is visited (degree-0 vertex should have own label)
bool CCVerifier(const Graph &g, const pvector<NodeID> &comp) {
  unordered_map<NodeID, NodeID> label_to_source;
  for (NodeID n : g.vertices())
    label_to_source[comp[n]] = n;
  Bitmap visited(g.num_nodes());
  visited.reset();
  vector<NodeID> frontier;
  frontier.reserve(g.num_nodes());
  for (auto label_source_pair : label_to_source) {
    NodeID curr_label = label_source_pair.first;
    NodeID source = label_source_pair.second;
    frontier.clear();
    frontier.push_back(source);
    visited.set_bit(source);
    for (auto it = frontier.begin(); it != frontier.end(); it++) {
      NodeID u = *it;
      for (NodeID v : g.out_neigh(u)) {
        if (comp[v] != curr_label)
          return false;
        if (!visited.get_bit(v)) {
          visited.set_bit(v);
          frontier.push_back(v);
        }
      }
      if (g.directed()) {
        for (NodeID v : g.in_neigh(u)) {
          if (comp[v] != curr_label)
            return false;
          if (!visited.get_bit(v)) {
            visited.set_bit(v);
            frontier.push_back(v);
          }
        }
      }
    }
  }
  for (NodeID n=0; n < g.num_nodes(); n++)
    if (!visited.get_bit(n))
      return false;
  return true;
}


int main(int argc, char* argv[]) {
  CLApp cli(argc, argv, "connected-components");
  if (!cli.ParseArgs())
    return -1;
  ROI_APP(ZRAY_BEGIN);
  Builder b(cli);
  Graph g = b.MakeGraph();
  // Lambdas rather than function pointers: BenchmarkKernel then calls the
  // kernel directly, which a ZRay region in main can follow into a clone.
  BenchmarkKernel(cli, g,
                  [](const Graph &g) { return ShiloachVishkin(g); },
                  [](const Graph &g, const pvector<NodeID> &comp) {
                    PrintCompStats(g, comp); },
                  [](const Graph &g, const pvector<NodeID> &comp) {
                    return CCVerifier(g, comp); });
  ROI_APP(ZRAY_END);
#ifdef ZRAY_STUDY
  // Flush the persistent OpenMP workers after all timed trials finish.
  zray_study_finalize();
#endif
  return 0;
}
