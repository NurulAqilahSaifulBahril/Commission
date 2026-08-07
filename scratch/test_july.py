import sys
from pathlib import Path

sys.path.append(str(Path(r"c:\Users\User\OneDrive\Documents\Commission\8. Web Dashboard")))
import app

client = app.app.test_client()

endpoints = [
    "/api/commission?year=2026&month=7&agent_type=internal",
    "/api/commission?year=2026&month=7&agent_type=outsource",
    "/api/factory-rates?year=2026&month=7&agent_type=internal",
    "/api/factory-rates?year=2026&month=7&agent_type=outsource",
    "/api/special-cases?year=2026&month=7&agent_type=internal",
    "/api/special-cases?year=2026&month=7&agent_type=outsource",
]

for ep in endpoints:
    print(f"\nRequesting {ep}...")
    try:
        response = client.get(ep)
        print("Status:", response.status_code)
        print("Data:", response.get_data(as_text=True)[:200])
    except Exception as e:
        import traceback
        traceback.print_exc()
