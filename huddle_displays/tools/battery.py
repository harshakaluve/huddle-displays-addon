#!/usr/bin/env python3
"""Battery model for one panel. Re-run it with your own measured numbers.

Measure the two that matter with a USB power meter or an INA219 in series:
  * sleep_ua  -- current with the panel in deep sleep (the dominant term)
  * refresh_s -- how long the full 7.5" refresh actually takes on your unit
"""
from dataclasses import dataclass


@dataclass
class Model:
    capacity_mah: float = 2000
    usable_frac: float = 0.90        # you don't get the last 10% of a LiPo

    # awake phases, seconds x milliamps
    connect_s: float = 2.0           # boot + wifi assoc (fast_connect, static IP)
    connect_ma: float = 85
    http_s: float = 0.8              # /plan + 48 KB BMP on a LAN
    http_ma: float = 85
    refresh_s: float = 4.5           # full e-paper refresh; the panel's own draw
    refresh_ma: float = 45
    tail_s: float = 0.7
    tail_ma: float = 30

    sleep_ua: float = 800            # measure this. See the note below.

    def cycle_mah(self) -> float:
        mas = (self.connect_s * self.connect_ma + self.http_s * self.http_ma
               + self.refresh_s * self.refresh_ma + self.tail_s * self.tail_ma)
        return mas / 3600.0

    def nap_mah(self) -> float:
        """A weekend nap: wakes, asks, sleeps. No e-paper refresh."""
        mas = self.connect_s * self.connect_ma + 0.3 * self.http_ma
        return mas / 3600.0

    def days(self, cycles_per_day: float) -> float:
        per_day = cycles_per_day * self.cycle_mah() + self.sleep_ua / 1000.0 * 24
        return self.capacity_mah * self.usable_frac / per_day


SCENARIOS = {
    "24h Mon-Fri, 15 min (your choice)": 96 * 5 / 7,
    "08:00-20:00 Mon-Fri, 15 min":       48 * 5 / 7,
    "24h Mon-Fri, 30 min":               48 * 5 / 7,
    "08:00-20:00 Mon-Fri, 30 min":       24 * 5 / 7,
}

if __name__ == "__main__":
    print("Per-cycle cost")
    m = Model()
    print(f"  full cycle (with redraw): {m.cycle_mah()*1000:6.1f} uAh")
    print(f"  weekend nap (no redraw):  {m.nap_mah()*1000:6.1f} uAh\n")

    for sleep_ua, label in ((150, "good case, board power-gates well"),
                            (400, "middling"),
                            (800, "implied by Seeed's own 3-month claim")):
        print(f"sleep current {sleep_ua} uA  ({label})")
        m = Model(sleep_ua=sleep_ua)
        base = None
        for name, cpd in SCENARIOS.items():
            d = m.days(cpd)
            delta = "" if base is None else f"   {d/base - 1:+.0%} vs your choice"
            base = base or d
            print(f"   {name:36s} {d:5.0f} days ({d/7:4.1f} weeks){delta}")
        print()

    print("Takeaway: at this board's quiescent current the sleep term dominates,")
    print("so halving the refresh rate buys far less than you'd expect.")
