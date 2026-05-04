import sys, time, requests

BASE = "http://127.0.0.1:8000/api/v1"
PASS_LIST = []
FAIL_LIST = []

def check(label, resp, expected=(200, 201)):
    ok = resp.status_code in expected
    (PASS_LIST if ok else FAIL_LIST).append(label)
    detail = ""
    if not ok:
        try: detail = f" | {resp.json()}"
        except: detail = f" | {resp.text[:200]}"
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: HTTP {resp.status_code}{detail}")
    return resp

print("\n" + "="*65)
print("  MODULE 6 - ORDER MANAGEMENT - Integration Test")
print("="*65)

print("\n[ 0. Login + Fund Admin + Start Simulator ]")
r = requests.post(f"{BASE}/auth/login", json={"email":"admin@arc.com","password":"Admin@1234"})
if r.status_code != 200:
    print(f"  ERROR: Login failed"); sys.exit(1)
token = r.json()["access_token"]
rt    = r.json()["refresh_token"]
H     = {"Authorization": f"Bearer {token}"}
print("  OK: Logged in")

me = requests.get(f"{BASE}/auth/me", headers=H).json()
admin_id = me.get("id", 1)
print(f"  Admin user id: {admin_id}")

fr = requests.post(f"{BASE}/funding/users/{admin_id}/credit", headers=H,
    json={"amount":10000000,"reference":"FUND-TEST-001","notes":"test","reason":"Test funding"})
print(f"  Funded admin: HTTP {fr.status_code}")

sim = requests.post(f"{BASE}/market/simulator/start", headers=H)
print(f"  Simulator: {sim.json().get('status', sim.status_code)}")
print("  Waiting 3 seconds...")
time.sleep(3)

print("\n[ 1. Margin Preview ]")
r = check("GET /orders/margin-preview NSE:RELIANCE qty=10",
    requests.get(f"{BASE}/orders/margin-preview",
        params={"symbol":"NSE:RELIANCE","side":"buy","order_type":"market","quantity":"10"},
        headers=H))
if r.status_code == 200:
    d = r.json()
    print(f"       margin_required={d.get('margin_required')} sufficient={d.get('sufficient')}")

r = check("GET /orders/margin-preview CRYPTO:BTCUSDT qty=0.01",
    requests.get(f"{BASE}/orders/margin-preview",
        params={"symbol":"CRYPTO:BTCUSDT","side":"buy","order_type":"limit","quantity":"0.01","price":"65000"},
        headers=H))
if r.status_code == 200:
    d = r.json()
    print(f"       margin_required={d.get('margin_required')} sufficient={d.get('sufficient')}")

print("\n[ 2. Place Orders ]")
r = check("POST /orders market BUY NSE:RELIANCE",
    requests.post(f"{BASE}/orders", headers=H, json={
        "symbol":"NSE:RELIANCE","order_type":"market","side":"buy","product_type":"intraday","quantity":"10"}))
market_id = r.json().get("id") if r.status_code==201 else None
if market_id: print(f"       order_id={market_id} status={r.json().get('status')}")

r = check("POST /orders limit BUY NSE:RELIANCE price=2400",
    requests.post(f"{BASE}/orders", headers=H, json={
        "symbol":"NSE:RELIANCE","order_type":"limit","side":"buy","product_type":"delivery","quantity":"5","price":"2400.00"}))
limit_id = r.json().get("id") if r.status_code==201 else None
if limit_id: print(f"       order_id={limit_id} status={r.json().get('status')}")

r = check("POST /orders limit BUY NSE:INFY SL+TP",
    requests.post(f"{BASE}/orders", headers=H, json={
        "symbol":"NSE:INFY","order_type":"limit","side":"buy","product_type":"intraday",
        "quantity":"20","price":"1650.00","sl_price":"1620.00","tp_price":"1720.00"}))
sl_tp_id = r.json().get("id") if r.status_code==201 else None
if sl_tp_id:
    trigs = r.json().get("triggers",[])
    print(f"       order_id={sl_tp_id} triggers={len(trigs)}")

check("POST /orders market BUY CRYPTO:BTCUSDT",
    requests.post(f"{BASE}/orders", headers=H, json={
        "symbol":"CRYPTO:BTCUSDT","order_type":"market","side":"buy","product_type":"delivery","quantity":"0.01"}))

print("\n[ 3. Validation Failures ]")
check("POST /orders invalid symbol expect 404",
    requests.post(f"{BASE}/orders", headers=H, json={
        "symbol":"NSE:FAKESYMBOL99","order_type":"market","side":"buy","product_type":"intraday","quantity":"10"}),(404,))
check("POST /orders limit no price expect 422",
    requests.post(f"{BASE}/orders", headers=H, json={
        "symbol":"NSE:RELIANCE","order_type":"limit","side":"buy","product_type":"intraday","quantity":"10"}),(422,))
check("POST /orders zero qty expect 422",
    requests.post(f"{BASE}/orders", headers=H, json={
        "symbol":"NSE:RELIANCE","order_type":"market","side":"buy","product_type":"intraday","quantity":"0"}),(422,))

print("\n[ 4. Query Orders ]")
r = check("GET /orders all", requests.get(f"{BASE}/orders", headers=H))
if r.status_code==200: print(f"       total={r.json()['total']}")
check("GET /orders?status=filled",   requests.get(f"{BASE}/orders?status=filled",   headers=H))
check("GET /orders?status=queued",   requests.get(f"{BASE}/orders?status=queued",   headers=H))
check("GET /orders?symbol=NSE:RELIANCE", requests.get(f"{BASE}/orders?symbol=NSE:RELIANCE", headers=H))

print("\n[ 5. Order Detail ]")
if market_id:
    r = check(f"GET /orders/{market_id}", requests.get(f"{BASE}/orders/{market_id}", headers=H))
    if r.status_code==200:
        d=r.json()
        print(f"       status={d['status']} filled_qty={d['filled_qty']} avg={d['average_price']}")
        print(f"       events: {[e['to_status'] for e in d['events']]}")
if sl_tp_id:
    r = check(f"GET /orders/{sl_tp_id} SL+TP triggers", requests.get(f"{BASE}/orders/{sl_tp_id}", headers=H))
    if r.status_code==200:
        trigs=r.json().get("triggers",[])
        print(f"       triggers={len(trigs)}: {[(t['trigger_type'],t['trigger_price']) for t in trigs]}")
check("GET /orders/99999 expect 404", requests.get(f"{BASE}/orders/99999", headers=H),(404,))

print("\n[ 6. Cancel Order ]")
if limit_id:
    r = check(f"POST /orders/{limit_id}/cancel",
        requests.post(f"{BASE}/orders/{limit_id}/cancel", headers=H, json={"reason":"test cancel"}))
    if r.status_code==200: print(f"       status={r.json().get('status')} margin_blocked={r.json().get('margin_blocked')}")
    check(f"POST /orders/{limit_id}/cancel again expect 422",
        requests.post(f"{BASE}/orders/{limit_id}/cancel", headers=H),(422,))
if market_id:
    check(f"POST /orders/{market_id}/cancel filled expect 422",
        requests.post(f"{BASE}/orders/{market_id}/cancel", headers=H),(422,))

print("\n[ 7. Cleanup ]")
requests.post(f"{BASE}/market/simulator/stop", headers=H)
requests.post(f"{BASE}/auth/logout", headers=H, json={"refresh_token":rt})
print("  Done")

print("\n"+"="*65)
print(f"  PASSED: {len(PASS_LIST)}  |  FAILED: {len(FAIL_LIST)}  |  TOTAL: {len(PASS_LIST)+len(FAIL_LIST)}")
print("="*65)
if FAIL_LIST:
    print("\nFailed:")
    for f in FAIL_LIST: print(f"  x  {f}")
else:
    print("\n  All tests passed!")
