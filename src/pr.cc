// Copyright (c) 2015, The Regents of the University of California (Regents)
// See LICENSE.txt for license details

#include <algorithm>
#include <iostream>
#include <vector>

#include "benchmark.h"
#include "builder.h"
#include "command_line.h"
#include "graph.h"
#include "pvector.h"

/*
GAP Benchmark Suite
Kernel: PageRank (PR)
Author: Scott Beamer

Will return pagerank scores for all vertices once total change < epsilon

This PR implementation uses the traditional iterative approach. It performs
updates in the pull direction to remove the need for atomics, and it allows
new values to be immediately visible (like Gauss-Seidel method). The prior PR
implementation is still available in src/pr_spmv.cc.
*/


using namespace std;

typedef float ScoreT;
const float kDamp = 0.85;


pvector<ScoreT> PageRankPullGS(const Graph &g, int max_iters, double epsilon=0,
                               bool logging_enabled = false) {
  const ScoreT init_score = 1.0f / g.num_nodes();
  const ScoreT base_score = (1.0f - kDamp) / g.num_nodes();
  pvector<ScoreT> scores(g.num_nodes(), init_score);
  pvector<ScoreT> outgoing_contrib(g.num_nodes());
  ca_log("scores", scores.data(), g.num_nodes() * sizeof(ScoreT));
  ca_log("outgoing_contrib", outgoing_contrib.data(), g.num_nodes() * sizeof(ScoreT));
  #pragma omp parallel for
  for (NodeID n=0; n < g.num_nodes(); n++)
    outgoing_contrib[n] = init_score / g.out_degree(n);
  for (int iter=0; iter < max_iters; iter++) {
    double error = 0;
    #pragma omp parallel for reduction(+ : error) schedule(dynamic, 16384)
    for (NodeID u=0; u < g.num_nodes(); u++) {
      ScoreT incoming_total = 0;
      for (NodeID v : g.in_neigh(u))
        incoming_total += outgoing_contrib[v];
      ScoreT old_score = scores[u];
      scores[u] = base_score + kDamp * incoming_total;
      error += fabs(scores[u] - old_score);
      outgoing_contrib[u] = scores[u] / g.out_degree(u);
    }
    if (logging_enabled)
      PrintStep(iter, error);
    if (error < epsilon)
      break;
  }
  return scores;
}


void PrintTopScores(const Graph &g, const pvector<ScoreT> &scores) {
  vector<pair<NodeID, ScoreT>> score_pairs(g.num_nodes());
  for (NodeID n=0; n < g.num_nodes(); n++) {
    score_pairs[n] = make_pair(n, scores[n]);
  }
  int k = 5;
  vector<pair<ScoreT, NodeID>> top_k = TopK(score_pairs, k);
  for (auto kvp : top_k)
    cout << kvp.second << ":" << kvp.first << endl;
}


// Verifies by asserting a single serial iteration in push direction has
//   error < target_error
bool PRVerifier(const Graph &g, const pvector<ScoreT> &scores,
                        double target_error) {
  const ScoreT base_score = (1.0f - kDamp) / g.num_nodes();
  pvector<ScoreT> incoming_sums(g.num_nodes(), 0);
  double error = 0;
  for (NodeID u : g.vertices()) {
    ScoreT outgoing_contrib = scores[u] / g.out_degree(u);
    for (NodeID v : g.out_neigh(u))
      incoming_sums[v] += outgoing_contrib;
  }
  for (NodeID n : g.vertices()) {
    error += fabs(base_score + kDamp * incoming_sums[n] - scores[n]);
    incoming_sums[n] = 0;
  }
  PrintTime("Total Error", error);
  return error < target_error;
}


int main(int argc, char* argv[]) {
  CLPageRank cli(argc, argv, "pagerank", 1e-4, 20);
  if (!cli.ParseArgs())
    return -1;
#ifdef CUSTOM_ALLOC
  // Arenas split by access pattern: rand = per-vertex scores/contrib (random
  // reads), stream = in-graph + indexes (streamed), cold = out_neighbors
  // (unused by the kernel). CA_NODES="rand,stream,cold" picks each arena's
  // NUMA node (default 0,0,0).
  int nr = 0, ns = 0, nc = 0;
  const char *nodes = getenv("CA_NODES");
  if (nodes && *nodes && (sscanf(nodes, "%d,%d,%d", &nr, &ns, &nc) != 3 ||
                nr < 0 || ns < 0 || nc < 0)) {
    fprintf(stderr, "CA_NODES must be \"rand,stream,cold\" node ids, got '%s'\n", nodes);
    return -1;
  }
  fprintf(stderr, "[ca] layout rand=%d stream=%d cold=%d\n", nr, ns, nc);
  int rand_arena = ca_arena_create(nr, MPOL_PREFERRED);
  int stream_arena = ca_arena_create(ns, MPOL_PREFERRED);
  int cold_arena = ca_arena_create(nc, MPOL_PREFERRED);
  if (rand_arena < 0 || stream_arena < 0 || cold_arena < 0) {
    perror("ca_arena_create");
    return -1;
  }
  ca_in_neigh = ca_index = stream_arena;
  ca_out_neigh = cold_arena;
#endif
  Builder b(cli);
  Graph g = b.MakeGraph();
#ifdef CUSTOM_ALLOC
  ca_cur = rand_arena;  // per-trial scores / outgoing_contrib
#endif
  auto PRBound = [&cli] (const Graph &g) {
    return PageRankPullGS(g, cli.max_iters(), cli.tolerance(), cli.logging_en());
  };
  auto VerifierBound = [&cli] (const Graph &g, const pvector<ScoreT> &scores) {
    return PRVerifier(g, scores, cli.tolerance());
  };
  BenchmarkKernel(cli, g, PRBound, PrintTopScores, VerifierBound);
  return 0;
}
