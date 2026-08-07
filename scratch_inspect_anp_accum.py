import sys
sys.path.append("7. Presentation")
import build_commission_pack
build_commission_pack._load_env_files()

sum_rows, det_rows, _ = build_commission_pack.fetch_internal_anp(2026)

# Show summary row structure
print("=== SUMMARY ROW KEYS ===")
if sum_rows:
    print(list(sum_rows[0].keys()))
    print()

# Show detail rows for May (month 5), grouped by agent
print("=== DETAIL ROWS - MAY 2026 ===")
det5 = [r for r in det_rows if build_commission_pack._parse_month(r.get("invoice_date")) == 5]
print(f"Total detail rows for May: {len(det5)}")
print()

# Group by agent and show running accumulated
agents_seen = []
for r in det5:
    ag = r["agent_name"]
    if ag not in agents_seen:
        agents_seen.append(ag)

for ag in agents_seen[:3]:  # show first 3 agents
    ag_rows = [r for r in det5 if r["agent_name"] == ag]
    print(f"Agent: {ag}  (invoices in May: {len(ag_rows)})")
    for r in ag_rows:
        print(f"  invoice={r['invoice_number']:15s}  ep_points={r['ep_points']}  accumulated_ep_points={r['accumulated_ep_points']}  tier_comm={r['anp_commission_accumulated_tier']}")

    # Final accumulated value = max of accumulated_ep_points for this agent
    final_accum = max(float(r["accumulated_ep_points"]) for r in ag_rows)
    print(f"  --> FINAL accumulated_ep_points = {final_accum:,.2f}")

    # Compare with summary row
    sum_for_ag = next((r for r in sum_rows if r["agent_name"] == ag), None)
    if sum_for_ag:
        print(f"  --> Summary row accumulated_ep_points = {float(sum_for_ag['accumulated_ep_points']):,.2f}")
    print()
