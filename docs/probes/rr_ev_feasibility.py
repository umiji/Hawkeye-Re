"""Is the doctrine's BUY region non-empty under its own base rates?

RR  = (base_target - entry) / (entry - stop)  >= 2.0
EV  = sum(p_i * (target_i/entry - 1))          >= +5%
Doctrine base rates: PEAD drift is 'low single-digit percent over 1-3 months';
'most catalyst trades resolve within +/-15% over 4-8 weeks'.
"""
from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import Scenario
from hawkeye.risk.sizing import build_position_plan

cfg = HawkeyeConfig()
entry = 100.0

print("Required base-case target to clear RR >= 2.0, by stop distance:")
print("  (stop distance is set by the Bull's price_below kill criterion;")
print("   the doctrine fallback is -8%, and >20% is vetoed outright)")
print()
print("  stop dist |  stop  | required base target | required base-case gain")
print("  ----------|--------|----------------------|------------------------")
for d in (2, 4, 6, 8, 10, 12, 15, 20):
    stop = entry * (1 - d / 100)
    req_target = entry + cfg.min_reward_risk * (entry - stop)
    print(f"    {d:5.0f}%  | {stop:6.2f} | {req_target:20.2f} | "
          f"{(req_target/entry-1)*100:+21.1f}%")

print()
print("Now: is there ANY base-rate-consistent scenario table that passes both?")
print("Doctrine says most catalyst trades resolve within +/-15% over 4-8 weeks.")
print()

def try_table(label, stop_pct, bear_t, base_t, bull_t, pb, pbase, pbull):
    stop = entry * (1 - stop_pct / 100)
    sc = [Scenario(name="bear", probability=pb, price_target=bear_t),
          Scenario(name="base", probability=pbase, price_target=base_t),
          Scenario(name="bull", probability=pbull, price_target=bull_t)]
    plan = build_position_plan(nav=100_000, entry_price=entry, stop_price=stop,
                               target_price=base_t, scenarios=sc, config=cfg)
    status = "APPROVED" if plan.approved else "VETOED (" + "; ".join(
        v.split(" below")[0] for v in plan.vetoes) + ")"
    print(f"  {label}")
    print(f"    stop -{stop_pct:.0f}%  targets {bear_t:.0f}/{base_t:.0f}/{bull_t:.0f}  "
          f"p={pb}/{pbase}/{pbull}")
    print(f"    -> EV={plan.expected_value_pct:+.2f}%  RR={plan.reward_risk:.2f}  {status}")
    print()

# (1) Textbook PEAD: modest drift, honest 50/50-ish odds, doctrine fallback stop
try_table("A. Doctrine-faithful PEAD (drift a few %, ~half lose)",
          8, 90, 104, 112, 0.40, 0.40, 0.20)

# (2) Same but generous bull tail, still inside +/-15%
try_table("B. Optimistic but still inside the +/-15% band",
          8, 92, 108, 115, 0.30, 0.45, 0.25)

# (3) Tight stop to buy RR, base case inside the band
try_table("C. Tight 4% stop (noise-level for an 8% ATR name)",
          4, 92, 108, 115, 0.30, 0.45, 0.25)

# (4) What it actually takes: base case +16% on an 8% stop
try_table("D. What the rules actually demand at an 8% stop",
          8, 92, 116, 130, 0.30, 0.45, 0.25)

print("Conclusion check: minimum base-case gain required at the 8% fallback")
print(f"stop is {cfg.min_reward_risk * 8:.0f}% — versus a doctrine that tells the Bull")
print("PEAD drift is 'low single-digit percent' and most trades resolve within +/-15%.")
