#include "foxess_priority.h"

#include <cassert>

int main() {
  FoxessPriority charging;
  assert(charging.tick(-300, 50, -500, 0) == -1);
  assert(charging.state() == FoxessPriority::State::FOX_CHARGE);
  assert(charging.suppress_marstek_charge());
  for (int i = 0; i < 11; ++i) assert(charging.tick(-300, 97, -500, 0) == -1);
  assert(charging.tick(-300, 97, -500, 0) == 2);
  assert(charging.state() == FoxessPriority::State::MARSTEK_CHARGE);

  FoxessPriority discharging;
  assert(discharging.tick(200, 50, 0, 2) == -1);
  assert(discharging.state() == FoxessPriority::State::MARSTEK_DISCHARGE);
  for (int i = 0; i < 17; ++i) assert(discharging.tick(500, 50, 0, 2) == -1);
  assert(discharging.tick(500, 50, 0, 2) == 0);
  assert(discharging.state() == FoxessPriority::State::FOX_DISCHARGE);
  assert(discharging.tick(0, 50, 500, 0) == -1);  // readback accepted
  for (int i = 0; i < 11; ++i) assert(discharging.tick(-300, 80, -200, 0) == -1);
  assert(discharging.tick(-300, 80, -200, 0) == -1);
  assert(discharging.state() == FoxessPriority::State::FOX_CHARGE);

  FoxessPriority bad_readback;
  assert(bad_readback.tick(200, 50, 0, 0) == 2);
  for (int i = 0; i < 6; ++i) bad_readback.tick(200, 50, 0, 0);
  assert(bad_readback.faulted());
}
