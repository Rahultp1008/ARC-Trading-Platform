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
print("  MODULE 7 - EXECUTION ENGINE - Integration Test")
print("="*65)

print("\n[ 0. Setup ]")
r = requests.post(f"{BASE}/auth/login", json={"email":"admin@arc.com","password":"Admin@1234"})
if r.status_code != 200:
    print(f"  ERROR: Login failed"); sys.exit(1)
token = r.json()["access_token"]
rt    = r.json()["refresh_token"]
H     = {"Authorization": f"Bearer {token}"}
me    = requests.get(f"{BASE}/auth/me", headers=H).json()
admin_id = me.get("id", 1)
print(f"  Admin id: {admin_id}")
requests.post(f"{BASE}/funding/users/{admin_id}/credit", headers=H,
    json={"amount":50000000,"reference":"MODULE7-FUND","notes":"test","reason":"Module 7 test"})
print("  Funded: Rs 5 crore")
sim = requests.post(f"{BASE}/market/simulator/start", headers=H)
print(f"  Simulator: {sim.json().get('status')}")
time.sleep(3)
q = requests.get(f"{BASE}/quotes/NSE:RELIANCE", headers=H).json()
reliance_ltp = float(q.get("ltp", 2450))
print(f"  NSE:RELIANCE LTP = Rs {reliance_ltp:.2f}")

print("\n[ 1. Market Order - fills immediately at LTP ]")
r = check("POST /orders market BUY NSE:RELIANCE qty=10",
    requests.post(f"{BASE}/orders", headers=H, json={
        "symbol":"NSE:RELIANCE","order_type":"market",
        "side":"buy","product_type":"intraday","quantity":"10"}))
mkt_id = r.json().get("id") if r.status_code==201 else None
if mkt_id:
    d = r.json()
    print(f"       order_id={mkt_id} status={d['status']}")
    print(f"       filled_qty={d['filled_qty']} avg_price={d['average_price']}")
    print(f"       event trail: {[e['to_status'] for e in d.get('events',[])]}")
    if d['status']=='filled': print("       Market order filled immediately as expected")

print("\n[ 2. Limit Order - goes to QUEUED status ]")
tick = 0.05
limit_price = round(int(reliance_ltp * 0.85 / tick) * tick, 2)
print(f"  Setting limit price = Rs {limit_price:.2f} (15% below LTP)")
r = check(f"POST /orders limit BUY NSE:RELIANCE @ {limit_price}",
    requests.post(f"{BASE}/orders", headers=H, json={
        "symbol":"NSE:RELIANCE","order_type":"limit",
        "side":"buy","product_type":"delivery",
        "quantity":"5","price":str(limit_price)}))
lmt_id = r.json().get("id") if r.status_code==201 else None
if lmt_id:
    d = r.json()
    print(f"       order_id={lmt_id} status={d['status']}")
    if d['status']=='queued': print("       Limit order is QUEUED - waiting for limit watcher")

print("\n[ 3. SL/TP Order - goes to PENDING_TRIGGER ]")
sl_trigger = round(int(reliance_ltp * 0.90 / tick) * tick, 2)
r = check("POST /orders stop_loss_market NSE:RELIANCE",
    requests.post(f"{BASE}/orders", headers=H, json={
        "symbol":"NSE:RELIANCE","order_type":"stop_loss_market",
        "side":"sell","product_type":"intraday",
        "quantity":"5","trigger_price":str(sl_trigger)}))
sl_id = r.json().get("id") if r.status_code==201 else None
if sl_id:
    d = r.json()
    print(f"       order_id={sl_id} status={d['status']} trigger={sl_trigger}")
    if d['status']=='pending_trigger': print("       SL order is PENDING_TRIGGER - watcher monitoring")

print("\n[ 4. Order List ]")
r = check("GET /orders all", requests.get(f"{BASE}/orders", headers=H))
if r.status_code==200: print(f"       total={r.json()['total']}")
check("GET /orders?status=filled",          requests.get(f"{BASE}/orders?status=filled",          headers=H))
check("GET /orders?status=queued",          requests.get(f"{BASE}/orders?status=queued",          headers=H))
check("GET /orders?status=pending_trigger", requests.get(f"{BASE}/orders?status=pending_trigger", headers=H))

print("\n[ 5. Market Order Event Trail ]")
if mkt_id:
    r = check(f"GET /orders/{mkt_id}", requests.get(f"{BASE}/orders/{mkt_id}", headers=H))
    if r.status_code==200:
        d=r.json()
        print(f"       status={d['status']}")
        print(f"       filled_qty={d['filled_qty']}")
        print(f"       avg_price={d['average_price']}")
        print(f"       events: {[e['to_status'] for e in d['events']]}")

print("\n[ 6. Limit Watcher Status ]")
r = check("GET /health", requests.get("http://127.0.0.1:8000/health"))
if r.status_code==200:
    d=r.json()
    print(f"       limit_watcher_running={d.get('limit_watcher_running')}")
    print(f"       simulator_running={d.get('simulator_running')}")
    print(f"       redis_connected={d.get('redis_connected')}")

print("\n[ 7. Cleanup ]")
requests.post(f"{BASE}/market/simulator/stop", headers=H)
requests.post(f"{BASE}/auth/logout", headers=H, json={"refresh_token":rt})
print("  Simulator stopped. Logged out.")

print("\n"+"="*65)
print(f"  PASSED: {len(PASS_LIST)}  |  FAILED: {len(FAIL_LIST)}  |  TOTAL: {len(PASS_LIST)+len(FAIL_LIST)}")
print("="*65)
if FAIL_LIST:
    print("\nFailed:")
    for f in FAIL_LIST: print(f"  x  {f}")
else:
    print("\n  All tests passed!")
