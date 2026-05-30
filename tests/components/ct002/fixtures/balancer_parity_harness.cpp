// Host-gcc differential harness for LoadBalancer parity testing.
//
// Reads scenarios from stdin and prints the resulting per-phase target for
// each, so a Python test (test_balancer_parity.py) can drive the *same*
// scenarios through the canonical Python LoadBalancer and assert both stacks
// agree on the wire-observable target. This complements the hard-coded
// host_balancer_test.cpp gtest cases with broad, data-driven parity coverage.
//
// Line format (whitespace separated, one scenario per line):
//   <mode> <consumer_id> <grid_total> <manual_value> <fair> <n> \
//       [<cid> <device_type> <phase> <power>] x n
//   mode  : auto | manual | inactive
//   fair  : 0 or 1 (BalancerConfig.fair_distribution)
//   n     : number of consumer reports that follow
// Output per line: three floats "a b c" (the phase targets).

#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "esphome/components/ct002/balancer.h"

using esphome::ct002::BalancerConfig;
using esphome::ct002::ConsumerMode;
using esphome::ct002::ConsumerModeKind;
using esphome::ct002::ConsumerReport;
using esphome::ct002::LoadBalancer;
using esphome::ct002::ReportMap;

int main() {
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.empty()) continue;
    std::istringstream in(line);
    std::string mode_str, consumer_id;
    float grid_total = 0.0f, manual_value = 0.0f;
    int fair = 1, n = 0;
    in >> mode_str >> consumer_id >> grid_total >> manual_value >> fair >> n;

    ReportMap reports;
    for (int i = 0; i < n; ++i) {
      std::string cid, dev, phase;
      float power = 0.0f;
      in >> cid >> dev >> phase >> power;
      reports[cid] = ConsumerReport{dev, phase, power};
    }

    BalancerConfig cfg;
    cfg.fair_distribution = (fair != 0);

    // Fresh balancer per scenario, saturation disabled and a fixed clock so a
    // single compute_target call is fully deterministic across stacks.
    double clock = 0.0;
    LoadBalancer balancer(cfg, /*sat_alpha=*/0.15f, /*sat_min_target=*/20.0f,
                          /*sat_decay=*/0.995f, /*sat_grace=*/90.0f,
                          /*sat_stall=*/60.0f, /*sat_enabled=*/false,
                          [&clock]() { return clock; }, nullptr);

    ConsumerMode mode;
    if (mode_str == "manual") {
      mode = ConsumerMode{ConsumerModeKind::MANUAL, manual_value};
    } else if (mode_str == "inactive") {
      mode = ConsumerMode{ConsumerModeKind::INACTIVE};
    } else {
      mode = ConsumerMode{ConsumerModeKind::AUTO};
    }

    const auto out = balancer.compute_target(consumer_id, mode, reports, grid_total,
                                             /*inactive=*/{}, /*manual=*/{},
                                             /*sample_id=*/{});
    std::cout << out[0] << " " << out[1] << " " << out[2] << "\n";
  }
  return 0;
}
