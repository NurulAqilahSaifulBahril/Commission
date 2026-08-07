import sys, os, json
from pathlib import Path
REPO_ROOT = Path('.').resolve()
sys.path.append(str(REPO_ROOT / '8. Web Dashboard'))
import app
app.build_commission_pack._load_env_files()

# Preload
app.load_disk_cache()
if not app._data_cache:
    print("Pre-fetching data...")
    app.prefetch_data_worker(2026)

# Test internal agents search
sys.argv = ['app.py']
with app.app.test_client() as client:
    r_int = client.get('/api/agents/search?agent_type=internal&year=2026&month=5')
    r_out = client.get('/api/agents/search?agent_type=outsource&year=2026&month=5')
    
    int_agents = json.loads(r_int.data.decode())
    out_agents = json.loads(r_out.data.decode())
    
    print('Internal Search Result:')
    for a in int_agents:
        print(f"  {a['name']} ({a['agent_type']})")
        
    print('\nOutsource Search Result:')
    for a in out_agents:
        print(f"  {a['name']} ({a['agent_type']})")
