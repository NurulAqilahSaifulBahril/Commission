import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load env variables first
load_dotenv(Path("c:/Users/User/OneDrive/Documents/Commission/.env"))

sys.path.append(str(Path("c:/Users/User/OneDrive/Documents/Commission/7. Presentation")))

import build_commission_pack

def main():
    print("Fetching internal basic...")
    t1, t2, t3, t4, meta, lines = build_commission_pack.fetch_internal_basic(2026)
    
    # Search for Pua Yee Ling or Chan Jia Wei in lines
    for line in lines:
        agent = getattr(line, "agent_name", "")
        cust = getattr(line, "customer_name", "")
        if "pua" in str(agent).lower() or "jia wei" in str(agent).lower():
            print(f"FOUND IN INTERNAL BASIC: Agent: {agent}, Customer: {cust}")

    print("Fetching outsource basic...")
    try:
        out_dir = Path("c:/Users/User/OneDrive/Documents/Commission/1. Basic Commission/3. Python Script")
        # Load outsource basic commission dynamically
        out_mod = build_commission_pack._load_module("out_basic_commission", out_dir / "outsource_basic_commission.py")
        out_rows, out_summary = out_mod.build_report(2026)
        for r in out_rows:
            agent = getattr(r, "agent_name", "")
            cust = getattr(r, "customer_name", "")
            if "pua" in str(agent).lower() or "jia wei" in str(agent).lower():
                print(f"FOUND IN OUTSOURCE BASIC: Agent: {agent}, Customer: {cust}")
    except Exception as e:
        print(f"Outsource check error: {e}")

if __name__ == "__main__":
    main()
