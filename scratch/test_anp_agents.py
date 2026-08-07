import sys
from pathlib import Path
REPO_ROOT = Path('.').resolve()
sys.path.append(str(REPO_ROOT / '7. Presentation'))
import build_commission_pack
build_commission_pack._load_env_files()

int_anp_summary, int_anp_detail, int_anp_meta = build_commission_pack.fetch_internal_anp(2026)
out_anp_summary, out_anp_detail, out_anp_meta = build_commission_pack.fetch_outsource_anp(2026)

print('Internal ANP agents:')
print(sorted(list(set(r.get('agent_name') for r in int_anp_detail))))

print('\nOutsource ANP agents:')
print(sorted(list(set(r.get('agent_name') for r in out_anp_detail))))
