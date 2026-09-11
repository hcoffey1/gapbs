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
#include "regent_regions/zones.h"

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


// Runs PageRank into caller-supplied score/contribution storage of
// g.num_nodes() entries each. Returns the number of iterations executed; the
// time spent in the iteration loop alone (after initialization) is stored in
// iterative_seconds when it is not null.
int PageRankPullGSCore(const Graph &g, int max_iters, double epsilon,
                       bool logging_enabled, ScoreT *scores,
                       ScoreT *outgoing_contrib,
                       double *iterative_seconds = nullptr) {
  const ScoreT init_score = 1.0f / g.num_nodes();
  const ScoreT base_score = (1.0f - kDamp) / g.num_nodes();
  #pragma omp parallel for
  for (NodeID n=0; n < g.num_nodes(); n++)
    scores[n] = init_score;
  #pragma omp parallel for
  for (NodeID n=0; n < g.num_nodes(); n++)
    outgoing_contrib[n] = init_score / g.out_degree(n);
  Timer iterative_timer;
  iterative_timer.Start();
  int iter = 0;
  for (; iter < max_iters; iter++) {
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
    if (error < epsilon) {
      iter++;
      break;
    }
  }
  iterative_timer.Stop();
  if (iterative_seconds != nullptr)
    *iterative_seconds = iterative_timer.Seconds();
  return iter;
}


pvector<ScoreT> PageRankPullGS(const Graph &g, int max_iters, double epsilon=0,
                               bool logging_enabled = false) {
  pvector<ScoreT> scores(g.num_nodes());
  pvector<ScoreT> outgoing_contrib(g.num_nodes());
  PageRankPullGSCore(g, max_iters, epsilon, logging_enabled, scores.data(),
                     outgoing_contrib.data());
  return scores;
}


template <typename ScoresT>
void PrintTopScores(const Graph &g, const ScoresT &scores) {
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
template <typename ScoresT>
bool PRVerifier(const Graph &g, const ScoresT &scores,
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


/*
Application-defined REGENT zones (opt-in, see common/regent_regions/zones.h):
  0 incoming_edges  final incoming neighbor lists traversed by the pull loop
  1 contributions   outgoing_contrib, gathered through neighbor ids
  2 scores          per-vertex scores, updated directly
  3 vertex_index    incoming and outgoing CSR pointer indices
Distinct outgoing neighbor payloads stay unmanaged: PageRank reads only their
index (for degrees); the verifier traverses them outside the measured trial.
An undirected graph aliases both CSR structures, so it has one neighbor array
and one index. Storage is relocated once after the final graph is built and
kept for the process lifetime; scores/contributions persist across trials.
*/
const std::vector<std::string> kZoneNames = {
    "incoming_edges", "contributions", "scores", "vertex_index"};

struct PageRankZones {
  workload_regions::Buffer<NodeID> in_neighs;
  workload_regions::Buffer<ScoreT> outgoing_contrib;
  workload_regions::Buffer<ScoreT> scores;
  workload_regions::Buffer<NodeID*> in_index;
  workload_regions::Buffer<NodeID*> out_index;   // directed graphs only

  // Copies the graph buffers PageRank touches into dedicated mappings and
  // hands them to the graph, then allocates the persistent workspace.
  void Prepare(Graph &g) {
    in_neighs.use_mapping();
    outgoing_contrib.use_mapping();
    scores.use_mapping();
    in_index.use_mapping();
    out_index.use_mapping();
    const int64_t num_in = g.in_neighbors_count();
    in_neighs.resize(num_in);
    const NodeID *old_in = g.in_neighbors_data();
    #pragma omp parallel for
    for (int64_t i = 0; i < num_in; i++)
      in_neighs[i] = old_in[i];
    in_index.resize(g.num_nodes() + 1);
    if (g.directed())
      out_index.resize(g.num_nodes() + 1);
    g.AdoptStorage(g.directed() ? out_index.data() : nullptr, in_index.data(),
                   in_neighs.data());
    scores.resize(g.num_nodes());
    outgoing_contrib.resize(g.num_nodes());
  }

  void Register(const Graph &g, const workload_regions::ZoneConfig &config) {
    in_neighs.register_with(config.registration, 0, config.regions[0]);
    outgoing_contrib.register_with(config.registration, 1, config.regions[1]);
    scores.register_with(config.registration, 2, config.regions[2]);
    in_index.register_with(config.registration, 3, config.regions[3]);
    if (g.directed())
      out_index.register_with(config.registration, 3, config.regions[3]);
  }

  void PrintManifest(const Graph &g, const CLPageRank &cli,
                     const workload_regions::ZoneConfig &config) const {
    using workload_regions::json_string;
    workload_regions::Manifest manifest("gapbs_pr", config);
    manifest.zone(kZoneNames[0]);
    manifest.buffer("in_neighbors", in_neighs);
    manifest.zone(kZoneNames[1]);
    manifest.buffer("outgoing_contrib", outgoing_contrib);
    manifest.zone(kZoneNames[2]);
    manifest.buffer("scores", scores);
    manifest.zone(kZoneNames[3]);
    manifest.buffer("in_index", in_index);
    if (g.directed()) {
      manifest.buffer("out_index", out_index);
      manifest.unmanaged("out_neighbors", g.num_edges() * sizeof(NodeID));
    }
    manifest.extra("\"input\":" + json_string(
        cli.filename() != "" ? cli.filename()
                             : (cli.uniform() ? "uniform" : "kron") +
                               std::string(" scale ") +
                               std::to_string(cli.scale()) + " degree " +
                               std::to_string(cli.degree())));
    manifest.extra("\"directed\":" + std::string(g.directed() ? "true" : "false"));
    manifest.extra("\"nodes\":" + std::to_string(g.num_nodes()));
    manifest.extra("\"edges\":" + std::to_string(g.num_edges()));
    manifest.extra("\"trials\":" + std::to_string(cli.num_trials()));
    manifest.extra("\"max_iters\":" + std::to_string(cli.max_iters()));
    manifest.extra("\"tolerance\":" + std::to_string(cli.tolerance()));
    manifest.extra("\"verification\":" +
                   std::string(cli.do_verify() ? "true" : "false"));
    manifest.print(config);
  }
};


// Mirrors BenchmarkKernel's output for the zoned layout: warmup trials are
// timed and reported but excluded from Average Time, the workspace persists,
// and each trial also reports its iteration count and iterative time.
void BenchmarkZoned(const CLPageRank &cli, const Graph &g,
                    const workload_regions::ZoneConfig &config,
                    PageRankZones &zones) {
  g.PrintStats();
  double total_seconds = 0;
  Timer trial_timer;
  for (int trial = -config.warmups; trial < cli.num_trials(); trial++) {
    double iterative_seconds = 0;
    auto begin = std::chrono::steady_clock::now();
    trial_timer.Start();
    int iterations = PageRankPullGSCore(g, cli.max_iters(), cli.tolerance(),
                                        cli.logging_en(), zones.scores.data(),
                                        zones.outgoing_contrib.data(),
                                        &iterative_seconds);
    trial_timer.Stop();
    auto end = std::chrono::steady_clock::now();
    workload_regions::zone_event("PR", "kernel_begin", trial, begin);
    workload_regions::zone_event("PR", "kernel_end", trial, end);
    std::cout << "PR_TRIAL {\"trial\":" << trial << ",\"iterations\":"
              << iterations << ",\"seconds\":" << trial_timer.Seconds()
              << ",\"iterative_seconds\":" << iterative_seconds << "}"
              << std::endl;
    if (trial < 0) {
      PrintTime("Warmup Time", trial_timer.Seconds());
    } else {
      PrintTime("Trial Time", trial_timer.Seconds());
      total_seconds += trial_timer.Seconds();
    }
    if (cli.do_analysis() && (trial == (cli.num_trials()-1)))
      PrintTopScores(g, zones.scores);
    if (cli.do_verify()) {
      trial_timer.Start();
      PrintLabel("Verification",
                 PRVerifier(g, zones.scores, cli.tolerance()) ? "PASS" : "FAIL");
      trial_timer.Stop();
      PrintTime("Verification Time", trial_timer.Seconds());
    }
  }
  PrintTime("Average Time", total_seconds / cli.num_trials());
}


int RunPageRank(int argc, char* argv[]) {
  workload_regions::ZoneConfig config =
      workload_regions::ZoneConfig::extract(argc, argv, kZoneNames);
  CLPageRank cli(argc, argv, "pagerank", 1e-4, 20);
  if (!cli.ParseArgs())
    return -1;
  Builder b(cli);
  Graph g = b.MakeGraph();
  if (!config.mapped()) {
    auto PRBound = [&cli] (const Graph &g) {
      return PageRankPullGS(g, cli.max_iters(), cli.tolerance(), cli.logging_en());
    };
    auto VerifierBound = [&cli] (const Graph &g, const pvector<ScoreT> &scores) {
      return PRVerifier(g, scores, cli.tolerance());
    };
    BenchmarkKernel(cli, g, PRBound, PrintTopScores<pvector<ScoreT>>,
                    VerifierBound);
    return 0;
  }
  if (cli.num_trials() <= 0)
    throw std::runtime_error("zoned runs need a positive trial count");
  // Zones outlive the graph: registered mappings are never unmapped, and
  // the graph's adopted storage must stay valid through its destructor.
  static PageRankZones zones;
  workload_regions::zone_event("PR", "prepare_begin", 0);
  zones.Prepare(g);
  workload_regions::zone_event("PR", "registration_begin", 0);
  if (config.application)
    zones.Register(g, config);
  zones.PrintManifest(g, cli, config);
  workload_regions::zone_event("PR", "regions_ready", 0);
  BenchmarkZoned(cli, g, config, zones);
  return 0;
}


int main(int argc, char* argv[]) {
  try {
    return RunPageRank(argc, argv);
  } catch (const std::exception &e) {
    std::cerr << "PR ERROR: " << e.what() << std::endl;
    return 1;
  }
}
