#pragma once

#include <cmath>

class FoxessPriority {
 public:
  enum class State {
    STARTING,
    FOX_CHARGE,
    MARSTEK_CHARGE,
    MARSTEK_DISCHARGE,
    FOX_DISCHARGE,
    FAULT,
  };

  // Called every 10 seconds. Returns -1 for no write, 0 for Self Use, 2 for Backup.
  int tick(float grid_w, float soc, float battery_w, int reported_mode) {
    if (!std::isfinite(grid_w) || !std::isfinite(soc) || !std::isfinite(battery_w) ||
        soc < 0.0f || soc > 100.0f || reported_mode < 0 || reported_mode > 2) {
      state_ = State::FAULT;
      pending_mode_ = -1;
      return -1;
    }

    if (pending_mode_ >= 0) {
      if (reported_mode == pending_mode_) {
        pending_mode_ = -1;
        pending_checks_ = 0;
      } else if (++pending_checks_ >= 6) {
        state_ = State::FAULT;
        pending_mode_ = -1;
        return -1;
      } else if (pending_checks_ == 2 || pending_checks_ == 4) {
        return pending_mode_;
      }
      if (pending_mode_ >= 0) return -1;
    }

    switch (state_) {
      case State::STARTING:
        state_ = (battery_w < -50.0f || grid_w < -150.0f) ? State::FOX_CHARGE
                                                           : State::MARSTEK_DISCHARGE;
        break;

      case State::FOX_CHARGE:
        if (battery_w > 100.0f) {
          if (++discharge_ticks_ >= 6) transition_(State::MARSTEK_DISCHARGE);
        } else {
          discharge_ticks_ = 0;
        }
        if (state_ == State::FOX_CHARGE && soc >= 97.0f && grid_w < -150.0f) {
          if (++export_ticks_ >= 12) transition_(State::MARSTEK_CHARGE);
        } else if (state_ == State::FOX_CHARGE) {
          export_ticks_ = 0;
        }
        break;

      case State::MARSTEK_CHARGE:
        if (soc < 92.0f) {
          transition_(State::FOX_CHARGE);
        } else if (grid_w > 100.0f) {
          if (++import_ticks_ >= 6) transition_(State::MARSTEK_DISCHARGE);
        } else {
          import_ticks_ = 0;
        }
        break;

      case State::MARSTEK_DISCHARGE:
        if (grid_w < -150.0f) {
          if (++export_ticks_ >= 6)
            transition_(soc >= 97.0f ? State::MARSTEK_CHARGE : State::FOX_CHARGE);
        } else {
          export_ticks_ = 0;
        }
        if (state_ == State::MARSTEK_DISCHARGE && grid_w > 300.0f) {
          if (++import_ticks_ >= 18) transition_(State::FOX_DISCHARGE);
        } else if (state_ == State::MARSTEK_DISCHARGE) {
          import_ticks_ = 0;
        }
        break;

      case State::FOX_DISCHARGE:
        if (grid_w < -150.0f) {
          if (++export_ticks_ >= 12)
            transition_(soc >= 97.0f ? State::MARSTEK_CHARGE : State::FOX_CHARGE);
        } else {
          export_ticks_ = 0;
        }
        break;

      case State::FAULT:
        return -1;
    }

    const int wanted = wanted_mode_();
    if (wanted >= 0 && reported_mode != wanted) {
      pending_mode_ = wanted;
      pending_checks_ = 0;
      return wanted;
    }
    return -1;
  }

  bool suppress_marstek_charge() const { return state_ == State::FOX_CHARGE; }
  bool faulted() const { return state_ == State::FAULT; }
  State state() const { return state_; }

  const char *state_name() const {
    switch (state_) {
      case State::STARTING: return "Starting";
      case State::FOX_CHARGE: return "FoxESS charging first";
      case State::MARSTEK_CHARGE: return "Marstek charging";
      case State::MARSTEK_DISCHARGE: return "Marstek discharging first";
      case State::FOX_DISCHARGE: return "FoxESS discharging last";
      case State::FAULT: return "Fault - automatic writes stopped";
    }
    return "Unknown";
  }

 private:
  int wanted_mode_() const {
    if (state_ == State::FOX_CHARGE || state_ == State::FOX_DISCHARGE) return 0;
    if (state_ == State::MARSTEK_CHARGE || state_ == State::MARSTEK_DISCHARGE) return 2;
    return -1;
  }

  void transition_(State next) {
    state_ = next;
    import_ticks_ = 0;
    export_ticks_ = 0;
    discharge_ticks_ = 0;
  }

  State state_{State::STARTING};
  int import_ticks_{0};
  int export_ticks_{0};
  int discharge_ticks_{0};
  int pending_mode_{-1};
  int pending_checks_{0};
};

inline FoxessPriority astra_foxess_priority;
