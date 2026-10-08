"""Build a read-only property review from the captured Sales Report source records.

Run from the repository root. Does not alter classifications, awards, or commissions.
"""
from collections import Counter
from pathlib import Path
import html
import json
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / '8. Web Dashboard/data/sales_property_review_2026_source.json'
OUTPUT = ROOT / '8. Web Dashboard/data/sales_property_review_2026.html'


def text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def recorded_type(value):
    value = text(value).upper()
    if re.search(r'\bFACTORY\b|\bKILANG\b', value):
        return 'Factory'
    if 'RAKYAT' in value or 'RESIDENTIAL' in value:
        return 'Residential'
    if re.search(r'SHOP[ -]?LOT|COMMERCIAL', value):
        return 'Commercial'
    for word, label in [('GOV', 'Government'), ('NGO', 'NGO'), ('CORPORATE', 'Corporate')]:
        if word in value:
            return label
    return ''


def panels(row):
    """Keep conflicting counts visible; never sum duplicate descriptions."""
    candidates = []
    if row.get('linked_package_panels'):
        candidates.append((int(row['linked_package_panels']), 'Linked invoice package'))
    for item in row.get('items') or []:
        if item.get('package_panels'):
            candidates.append((int(item['package_panels']), 'Invoice-item package'))
        desc = str(item.get('description') or '')
        # A quantity followed by panel wattage, or a quantity x panel brand.
        for pattern in [r'\b(\d+)\s*[xX]\s*\d{3,4}\s*[wW]\b',
                        r'\b(\d+)\s*[xX]\s*(?:JINKO|LONGI|TRINA|JA\s+SOLAR|CANADIAN)\b']:
            for match in re.finditer(pattern, desc, re.I):
                candidates.append((int(match.group(1)), 'Invoice-item description'))
    if row.get('panel_qty'):
        candidates.append((int(row['panel_qty']), 'Invoice panel field'))
    counts = sorted({n for n, _ in candidates if n > 0})
    if not counts:
        return '', 'Not recovered'
    if len(counts) > 1:
        return ' / '.join(map(str, counts)), 'Conflicting package/item counts; check revisions or additions'
    return str(counts[0]), '; '.join(dict.fromkeys(s for n, s in candidates if n == counts[0]))


def assess(row):
    name = text(row.get('customer_name'))
    installation = text(row.get('installation_address'))
    if installation.lower() in ('same as registered address', 'same as registration address'):
        address = text(row.get('customer_address'))
        address_source = 'Customer address (SEDA says same as registered address)'
    elif installation:
        address, address_source = installation, 'SEDA installation address'
    else:
        address = text(row.get('invoice_address') or row.get('customer_address'))
        address_source = 'Customer/billing address; installation site unverified'
    structured = [(recorded_type(row.get(k)), f'{label}: {text(row.get(k))}')
                  for k, label in [('seda_nem_type', 'SEDA type'), ('referral_project_type', 'Referral project type')]]
    structured = [(kind, evidence) for kind, evidence in structured if kind]
    explicit = []
    if re.search(r'\bFACTORY\b|\bKILANG\b', name, re.I):
        explicit.append(('Factory', 'Customer name explicitly says Factory/Kilang'))
    if re.search(r'SH(?:O|A)P[ -]?LOT|\bCOMMERCIAL\b', name, re.I):
        explicit.append(('Commercial', 'Customer name explicitly says Shop Lot/Commercial'))
    business_use = re.search(r'\bHOTEL\b|\bRESORT\b|\b(?:PETROL|PERTOL)\s+STATION\b|\bCLINIC\b|\bKLINIK\b', name, re.I)
    if business_use and not explicit:
        explicit.append(('Commercial', f'Customer name describes a business premise: {business_use.group(0)}'))
    industrial = re.search(r'PERINDUSTRIAN|KAWASAN\s+INDUSTRI|INDUSTRIAL|\bKILANG\b', address, re.I)
    company = re.search(r'SDN\.?\s*BHD|\bBHD\b|\bENTERPRISE\b|\bTRADING\b|\bSYARIKAT\b|\bINDUSTRIES\b|\bMETAL\s+WORKS\b|\bMANUFACTUR', name, re.I)
    evidence = [v for _, v in structured + explicit]
    if industrial:
        evidence.append(f'Address has an industrial-location clue: {industrial.group(0)}; site use still needs verification')
    kinds = {k for k, _ in structured + explicit}
    if len(kinds) > 1:
        suggestion, confidence, status = 'Unclear', 'Low', 'Conflicting evidence'
        evidence.append('Recorded type and customer-name site label disagree; do not choose automatically')
    elif structured:
        suggestion = structured[0][0]
        confidence, status = 'High', 'Recorded type'
        if suggestion == 'Residential' and industrial:
            confidence, status = 'Low', 'Conflicting evidence'
    elif explicit:
        suggestion, confidence, status = explicit[0][0], 'Medium', 'Suggested — review'
    elif industrial:
        suggestion, confidence, status = 'Factory', 'Low', 'Suggested — review'
        evidence.append('Industrial location can also contain offices, shops, or warehouses')
    elif company:
        suggestion, confidence, status = 'Unclear', 'Low', 'Needs review'
        evidence.append('Company name alone cannot distinguish a home, commercial premise, or factory')
    elif re.search(r'PERTUBUHAN|KEBAJIKAN|YATIM|\bTEMPLE\b|\bMASJID\b|\bSURAU\b', name, re.I):
        suggestion, confidence, status = 'Unclear', 'Low', 'Needs review'
        evidence.append('Possible organisation/community premise; do not force into the three property types')
    elif re.search(r'\b(?:TAMAN|TMN|BANDAR|KAMPUNG|KG|BUNGALOW|BANGLO|TERES)\b', address, re.I):
        suggestion, confidence, status = 'Residential', 'Low', 'Suggested — review'
        evidence.append('Individual-style customer name and housing-locality address suggest residential use; shops may share this locality')
    else:
        suggestion, confidence, status = 'Unclear', 'Low', 'Needs review'
        evidence.append('No explicit property type or sufficiently specific address clue')
    if not address:
        evidence.append('No address available')
    count, count_source = panels(row)
    if '/' in count:
        evidence.append('Panel quantity needs review: package/item counts disagree')
    if re.search(r'\bCANCEL|\bREFUND', name, re.I):
        evidence.append('Customer name mentions cancellation/refund; verify sales eligibility separately')
    return dict(suggested_type=suggestion, confidence=confidence, review_status=status,
                reasons=evidence, address=address, address_source=address_source,
                recovered_panels=count, panel_source=count_source)


def build():
    data = json.loads(SOURCE.read_text(encoding='utf-8'))
    sys.path.insert(0, str(ROOT / '4. EGA ESA Awards/3. Python script'))
    from full_internal_EGA_ESA_Awards import classify_property_type
    results = []
    for row in data['rows']:
        result = dict(row)
        result.update(assess(row))
        result['current_sales_report_type'] = classify_property_type(row)
        current = 'Commercial' if result['current_sales_report_type'] == 'Shop Lot' else result['current_sales_report_type']
        result['proposed_change'] = result['suggested_type'] != 'Unclear' and current != result['suggested_type']
        results.append(result)
    assert len(results) == len(data['rows'])
    assert len({r['bubble_id'] for r in results}) == len(results), 'Duplicate invoice identities'
    results.sort(key=lambda r: (not r['proposed_change'], r['review_status'] != 'Conflicting evidence',
                                r['confidence'] == 'Low', str(r.get('invoice_number'))))
    summary = {'total': len(results), 'current_types': dict(Counter(r['current_sales_report_type'] for r in results)),
               'suggested_types': dict(Counter(r['suggested_type'] for r in results)),
               'confidence': dict(Counter(r['confidence'] for r in results)),
               'proposed_changes': sum(r['proposed_change'] for r in results),
               'conflicts': sum(r['review_status'] == 'Conflicting evidence' for r in results),
               'panels_recovered': sum(bool(r['recovered_panels']) for r in results),
               'panel_conflicts': sum('/' in r['recovered_panels'] for r in results)}
    (OUTPUT.with_suffix('.json')).write_text(json.dumps({'scope':data['scope'], 'retrieved_at':data['retrieved_at'],
        'summary':summary, 'results':results}, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    e = lambda v: html.escape(str(v or ''))
    table = []
    for r in results:
        attrs = f'data-type="{e(r["suggested_type"])}" data-confidence="{e(r["confidence"])}" data-change="{str(r["proposed_change"]).lower()}" data-conflict="{str(r["review_status"] == "Conflicting evidence").lower()}"'
        table.append(f'<tr {attrs}><td><b>{e(r["invoice_number"])}</b><small>First payment: {e(r["first_payment_date"])}</small></td>'
                     f'<td>{e(r["customer_name"])}<small>Agent: {e(r["agent_name"])}</small></td>'
                     f'<td>{e(r["current_sales_report_type"])}</td><td><b>{e(r["suggested_type"])}</b><small>{e(r["review_status"])}</small></td>'
                     f'<td><span class="badge {e(r["confidence"].lower())}">{e(r["confidence"])}</span></td>'
                     f'<td>{e(r["recovered_panels"] or "Unknown")}<small>{e(r["panel_source"])}</small></td>'
                     f'<td>{e(r["address"] or "Unavailable")}<small>{e(r["address_source"])}</small></td>'
                     f'<td>{"<br>".join(e(reason) for reason in r["reasons"])}</td></tr>')
    document = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sales Report · Property review 2026</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f7fa;color:#142439;font:14px system-ui,sans-serif}main{max-width:1800px;margin:auto;padding:32px}h1{font-size:28px;margin:0 0 10px}p{line-height:1.55;max-width:1100px}.cards{display:flex;flex-wrap:wrap;gap:12px;margin:24px 0}.card{background:white;border:1px solid #dce3ec;border-radius:10px;padding:16px;min-width:160px}.card b{display:block;font-size:26px}.filters{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:20px 0}input,select{padding:10px;border:1px solid #bbc9d9;border-radius:6px}input[type=search]{min-width:300px}.scroll{overflow:auto;background:white;border:1px solid #dce3ec;border-radius:8px;max-height:75vh}table{border-collapse:collapse;width:100%;min-width:1250px}th{position:sticky;top:0;background:#173a57;color:white;text-align:left;padding:12px}td{padding:12px;border-bottom:1px solid #e4eaf1;vertical-align:top;line-height:1.45}td:nth-child(2){min-width:230px}td:nth-child(7){min-width:250px}td:nth-child(8){min-width:300px}small{display:block;color:#617489;font-size:11px;margin-top:5px}.badge{padding:4px 8px;border-radius:20px;font-size:12px}.high{background:#dcf5e7;color:#166534}.medium{background:#e4edff;color:#1e40af}.low{background:#fff1ce;color:#854d0e}tr[data-conflict=true]{background:#fff0ed}[hidden]{display:none!important}details{margin:18px 0}summary{cursor:pointer;font-weight:600}footer{color:#617489;margin-top:24px;font-size:12px}
</style><main><h1>Sales Report · Property review</h1>
<p>January–October 2026 · Suggestions for residential, commercial and factory sites. This review does not update the Sales Report, EP points, or commission calculations.</p>
<p>High means an explicit recorded project/SEDA type supports the suggestion, not an independently verified site. Medium uses explicit site wording in customer names. Low remains tentative. Company names, “Taman” addresses, and panel counts cannot establish actual property use on their own.</p>
<div class="cards">CARDS</div>
<details><summary>Evidence, scope and limitations</summary><p>Used: production invoice records, the Sales Report’s source query, SEDA installation addresses and types, referral project types, linked packages and invoice-item descriptions. Scope: SCOPE. Retrieved: RETRIEVED. This is a bounded invoice review, not an exhaustive customer/property inventory.</p>
<p>The invoice-level panel field is zero for every row; quantities shown here are recovered from packages/items. Different counts are displayed together rather than added. Customer-address fallbacks are labelled and may be billing addresses. TNB tariffs, site photographs and independently confirmed building use were not assessed. Generic company names are left unclear. Low-confidence residential suggestions rely on an individual-style name and a housing-locality address; commercial premises can share these addresses. Industrial-estate clues do not prove a factory. Shop Lot is included under Commercial for this review, while the existing Sales Report classification is shown separately.</p></details>
<div class="filters"><input id="search" type="search" placeholder="Search invoice, customer, agent or address" aria-label="Search invoices"><select id="type" aria-label="Suggested type"><option value="">All suggested types</option><option>Residential</option><option>Commercial</option><option>Factory</option><option>Unclear</option></select><select id="confidence" aria-label="Confidence"><option value="">All confidence levels</option><option>High</option><option>Medium</option><option>Low</option></select><label><input id="changes" type="checkbox"> Suggested changes only</label><label><input id="conflicts" type="checkbox"> Conflicting evidence only</label><span id="count"></span></div>
<div class="scroll"><table><thead><tr><th>Invoice</th><th>Customer / Agent</th><th>Current type</th><th>Suggested type</th><th>Confidence</th><th>Panels</th><th>Address</th><th>Reason / Review notes</th></tr></thead><tbody>ROWS</tbody></table></div>
<footer>Verify conflicting and tentative rows against the site survey or installation records before changing Sales Report classifications. Source records and a machine-readable review are saved beside this HTML file.</footer>
</main><script>
const rows=[...document.querySelectorAll('tbody tr')];const controls=['search','type','confidence','changes','conflicts'].map(id=>document.getElementById(id));
function filter(){const [s,t,c,ch,co]=controls;let shown=0;for(const r of rows){const keep=(!s.value||r.textContent.toLowerCase().includes(s.value.toLowerCase()))&&(!t.value||r.dataset.type===t.value)&&(!c.value||r.dataset.confidence===c.value)&&(!ch.checked||r.dataset.change==='true')&&(!co.checked||r.dataset.conflict==='true');r.hidden=!keep;shown+=keep}document.getElementById('count').textContent=`${shown} of ${rows.length} invoices`}
controls.forEach(c=>c.addEventListener('input',filter));filter();
</script></html>'''
    cards = [('Invoices reviewed', summary['total']), ('Suggested changes', summary['proposed_changes']),
             ('Conflicting evidence', summary['conflicts']), ('Panel counts recovered', summary['panels_recovered'])]
    cards += [(f'{k} suggestions', v) for k,v in summary['suggested_types'].items()]
    document = document.replace('CARDS', ''.join(f'<div class="card">{e(label)}<b>{value}</b></div>' for label,value in cards))
    document = document.replace('SCOPE', e(data['scope'])).replace('RETRIEVED', e(data['retrieved_at'])).replace('ROWS', ''.join(table))
    OUTPUT.write_text(document, encoding='utf-8')
    print(json.dumps(summary, indent=2))
    print('Saved:', OUTPUT)
    for r in results[:12]:
        print(r['invoice_number'], r['customer_name'], '=>', r['suggested_type'], r['confidence'])


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    build()
