import sys, time, requests

BASE  = "http://127.0.0.1:8000/api/v1"
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
print("  MODULE 8 - PORTFOLIO & PnL - Integration Test")
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

requests.post(f"{BASE}/funding/users/{admin_id}/credit", headers=H, json={
    "amount":50000000,"reference":"M8-FUND","notes":"test","reason":"Module 8 test"})
print("  Funded: Rs 5 crore")

sim = requests.post(f"{BASE}/market/simulator/start", headers=H)
print(f"  Simulator: {sim.json().get('status')}")
print("  Waiting 3s...")
time.sleep(3)

q = requests.get(f"{BASE}/quotes/NSE:RELIANCE", headers=H).json()
ltp = float(q.get("ltp", 1350))
print(f"  NSE:RELIANCE LTP = Rs {ltp:.2f}")

print("  Placing test orders...")
r1 = requests.post(f"{BASE}/orders", headers=H, json={
    "symbol":"NSE:RELIANCE","order_type":"market","side":"buy","product_type":"intraday","quantity":"10"})
print(f"  Order 1 (BUY RELIANCE intraday): {r1.json().get('status')}")

r2 = requests.post(f"{BASE}/orders", headers=H, json={
    "symbol":"NSE:TCS","order_type":"market","side":"buy","product_type":"delivery","quantity":"5"})
print(f"  Order 2 (BUY TCS delivery): {r2.json().get('status')}")

r3 = requests.post(f"{BASE}/orders", headers=H, json={
    "symbol":"CRYPTO:BTCUSDT","order_type":"market","side":"buy","product_type":"delivery","quantity":"0.01"})
print(f"  Order 3 (BUY 0.01 BTC): {r3.json().get('status')}")
time.sleep(1)

print("\n[ 1. Portfolio Summary ]")
r = check("GET /portfolio/summary", requests.get(f"{BASE}/portfolio/summary", headers=H))
if r.status_code == 200:
    d = r.json()
    print(f"       cash_balance   = Rs {float(d.get('cash_balance',0)):,.2f}")
    print(f"       margin_used    = Rs {float(d.get('margin_used',0)):,.2f}")
    print(f"       free_margin    = Rs {float(d.get('free_margin',0)):,.2f}")
    print(f"       unrealized_pnl = Rs {float(d.get('unrealized_pnl',0)):,.2f}")
    print(f"       realized_pnl   = Rs {float(d.get('realized_pnl',0)):,.2f}")
    print(f"       total_equity   = Rs {float(d.get('total_equity',0)):,.2f}")
    print(f"       open_positions = {d.get('open_positions')}")
    print(f"       prices_live    = {d.get('prices_live')}")

print("\n[ 2. Positions ]")
r = check("GET /portfolio/positions", requests.get(f"{BASE}/portfolio/positions", headers=H))
if r.status_code == 200:
    positions = r.json()
    print(f"       open positions: {len(positions)}")
    for p in positions:
        print(f"       {p['symbol']:20s} qty={p['net_qty']} avg={p.get('avg_cost','?')} ltp={p.get('current_ltp','?')} upnl={p.get('unrealized_pnl','?')}")

print("\n[ 3. Holdings ]")
r = check("GET /portfolio/holdings", requests.get(f"{BASE}/portfolio/holdings", headers=H))
if r.status_code == 200:
    holdings = r.json()
    print(f"       delivery holdings: {len(holdings)}")
    for h in holdings:
        print(f"       {h['symbol']:20s} qty={h['quantity']} invested=Rs {h.get('invested_value','?')}")

print("\n[ 4. Trades ]")
r = check("GET /portfolio/trades", requests.get(f"{BASE}/portfolio/trades", headers=H))
if r.status_code == 200:
    d = r.json()
    print(f"       total trades: {d['total']}")
    for t in d['trades'][:3]:
        print(f"       {t['symbol']:20s} {t['trade_type']:4s} qty={t['quantity']} @ Rs {t['price']}")
check("GET /portfolio/trades?page=1&size=5",
    requests.get(f"{BASE}/portfolio/trades?page=1&size=5", headers=H))

print("\n[ 5. PnL Breakdown ]")
r = check("GET /portfolio/pnl", requests.get(f"{BASE}/portfolio/pnl", headers=H))
if r.status_code == 200:
    d = r.json()
    print(f"       total_unrealized = Rs {float(d.get('total_unrealized_pnl',0)):,.2f}")
    print(f"       total_realized   = Rs {float(d.get('total_realized_pnl',0)):,.2f}")
    print(f"       equity_unrealized= Rs {float(d.get('equity_unrealized',0)):,.2f}")
    print(f"       crypto_unrealized= Rs {float(d.get('crypto_unrealized',0)):,.2f}")
    print(f"       total_brokerage  = Rs {float(d.get('total_brokerage',0)):,.2f}")

print("\n[ 6. Auth check ]")
check("GET /portfolio/summary without token (expect 401/403)",
    requests.get(f"{BASE}/portfolio/summary"), (401, 403))

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
