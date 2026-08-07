import sys
from pathlib import Path

# Add dashboard to sys.path
sys.path.append(str(Path(r"c:\Users\User\OneDrive\Documents\Commission\8. Web Dashboard")))
import app

# Clear the cache first to force a live fetch from the Postgres proxy database
app.clear_cache()

client = app.app.test_client()

print("Requesting internal july with NO cache...")
try:
    response = client.get("/api/commission?year=2026&month=7&agent_type=internal")
    print("Status:", response.status_code)
    print("Data:", response.get_data(as_text=True)[:1000])
except Exception as e:
    import traceback
    traceback.print_exc()

print("\nRequesting outsource july with NO cache...")
try:
    response = client.get("/api/commission?year=2026&month=7&agent_type=outsource")
    print("Status:", response.status_code)
    print("Data:", response.get_data(as_text=True)[:1000])
except Exception as e:
    import traceback
    traceback.print_exc()
