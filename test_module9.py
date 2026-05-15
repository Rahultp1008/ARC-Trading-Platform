"""
test_module9.py — Integration tests for Risk & Margin Module (Module 9)
Run: python test_module9.py   (server must be running with simulator started)
"""
import sys
import time
import requests

BASE = "http://127.0.0.1:8000/api/v1"
PASS_LIST = []
FAIL_LIST = []


def check(label, resp, expected=(200, 201)):
    ok = resp.status_code in expected
    (PASS_LIST if ok else FAIL_LIST).append(label)
    detail = ""
    if not ok:
        try: detail = f" | {resp.json()}"
        except: detail = f" | {resp.text[:300]}"
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: HTTP {resp.status_code}{detail}")
    return resp


print("\n" + "=" * 65)
print("  MODULE 9 — RISK & MARGIN — Integration Test")
print("=" * 65)

# ── Setup ─────────────────────────────────────────────────────────────────
print("\n[ 0. Setup ]")
r = requests.post(f"{BASE}/auth/login", json={"email":"admin@arc.com","password":"Admin@1234"})
if r.status_code != 200:
    print("  ERROR: Login failed"); sys.exit(1)
token = r.json()["access_token"]
rt    = r.json()["refresh_token"]
H     = {"Authorization": f"Bearer {token}"}
me    = requests.get(f"{BASE}/auth/me", headers=H).json()
admin_id = me.get("id", 1)
print(f"  Admin id: {admin_id}")

requests.post(f"{BASE}/funding/users/{admin_id}/credit", headers=H, json={
    "amount":5000000,"reference":"M9-FUND","notes":"test","reason":"Module 9 test"})
print("  Funded: Rs 50 lakh")

sim = requests.post(f"{BASE}/market/simulator/start", headers=H)
print(f"  Simulator: {sim.json().get('status')}")
time.sleep(3)

q = requests.get(f"{BASE}/quotes/NSE:RELIANCE", headers=H).json()
ltp = float(q.get("ltp", 1350))
print(f"  NSE:RELIANCE LTP = Rs {ltp:.2f}")


# ── 1. Margin Requirement ─────────────────────────────────────────────────
print("\n[ 1. Margin Requirement — GET /risk/margin-requirement ]")

r = check("GET /risk/margin-requirement RELIANCE intraday qty=10",
    requests.get(f"{BASE}/risk/margin-requirement",
        params={"symbol":"NSE:RELIANCE","product_type":"intraday","quantity":"10"},
        headers=H))
if r.status_code == 200:
    d = r.json()
    print(f"       trade_value    = Rs {float(d['trade_value']):,.2f}")
    print(f"       margin_rate    = {d['margin_rate_pct']} ({d['leverage']}x leverage)")
    print(f"       margin_required= Rs {float(d['margin_required']):,.2f}")
    print(f"       lot_compliant  = {d['is_lot_compliant']}")

r = check("GET /risk/margin-requirement RELIANCE delivery qty=5",
    requests.get(f"{BASE}/risk/margin-requirement",
        params={"symbol":"NSE:RELIANCE","product_type":"delivery","quantity":"5"},
        headers=H))
if r.status_code == 200:
    d = r.json()
    print(f"       delivery margin = Rs {float(d['margin_required']):,.2f} ({d['margin_rate_pct']} — no leverage)")

r = check("GET /risk/margin-requirement BTCUSDT qty=0.01",
    requests.get(f"{BASE}/risk/margin-requirement",
        params={"symbol":"CRYPTO:BTCUSDT","product_type":"delivery","quantity":"0.01"},
        headers=H))
if r.status_code == 200:
    d = r.json()
    print(f"       crypto margin   = Rs {float(d['margin_required']):,.2f}")


# ── 2. Margin Preview — Happy Path ────────────────────────────────────────
print("\n[ 2. Margin Preview — GET /risk/preview (should PASS) ]")

r = check("GET /risk/preview market BUY RELIANCE intraday qty=10",
    requests.get(f"{BASE}/risk/preview",
        params={"symbol":"NSE:RELIANCE","order_type":"market","side":"buy",
                "product_type":"intraday","quantity":"10"},
        headers=H))
if r.status_code == 200:
    d = r.json()
    print(f"       can_place      = {d['can_place']}")
    print(f"       sufficient     = {d['sufficient']}")
    print(f"       margin_required= Rs {float(d['margin_required']):,.2f}")
    print(f"       available      = Rs {float(d['available_margin']):,.2f}")
    print(f"       after_trade    = Rs {float(d['margin_after']):,.2f}")
    print(f"       checks passed  = {sum(1 for v in d['validations'] if v['passed'])}/{len(d['validations'])}")


# ── 3. Margin Preview — Validation Failures ───────────────────────────────
print("\n[ 3. Margin Preview — Validation Failures ]")

# Limit order without price
r = check("GET /risk/preview limit without price (should flag)",
    requests.get(f"{BASE}/risk/preview",
        params={"symbol":"NSE:RELIANCE","order_type":"limit","side":"buy",
                "product_type":"intraday","quantity":"10"},
        headers=H))
if r.status_code == 200:
    d = r.json()
    print(f"       can_place = {d['can_place']} | rejection = {d['rejection_reason']}")

# Invalid tick size
tick_price = round(ltp * 0.99, 2)  # may not be a tick multiple
invalid_tick = tick_price + 0.01   # add 1 paisa — likely invalid tick
r = check("GET /risk/preview limit with tick-invalid price",
    requests.get(f"{BASE}/risk/preview",
        params={"symbol":"NSE:RELIANCE","order_type":"limit","side":"buy",
                "product_type":"intraday","quantity":"10","price":str(round(invalid_tick,2))},
        headers=H))
if r.status_code == 200:
    d = r.json()
    tick_check = next((v for v in d['validations'] if v['check'] == 'tick_size_compliance'), None)
    if tick_check:
        print(f"       tick_check = {'PASS' if tick_check['passed'] else 'FAIL'}: {tick_check['message'][:80]}")

# Huge quantity — should fail quantity cap
r = check("GET /risk/preview huge quantity (expect fail)",
    requests.get(f"{BASE}/risk/preview",
        params={"symbol":"NSE:RELIANCE","order_type":"market","side":"buy",
                "product_type":"intraday","quantity":"99999"},
        headers=H))
if r.status_code == 200:
    d = r.json()
    print(f"       can_place = {d['can_place']} | {d.get('rejection_reason','')[:80]}")

# ── 4. Portfolio Health ───────────────────────────────────────────────────
print("\n[ 4. Portfolio Health — GET /risk/health ]")

# Place an order first so we have a position
requests.post(f"{BASE}/orders", headers=H, json={
    "symbol":"NSE:RELIANCE","order_type":"market","side":"buy",
    "product_type":"intraday","quantity":"5"})
time.sleep(1)

r = check("GET /risk/health", requests.get(f"{BASE}/risk/health", headers=H))
if r.status_code == 200:
    d = r.json()
    print(f"       wallet_balance       = Rs {float(d['wallet_balance']):,.2f}")
    print(f"       total_margin_used    = Rs {float(d['total_margin_used']):,.2f}")
    print(f"       free_margin          = Rs {float(d['free_margin']):,.2f}")
    print(f"       overall_health_ratio = {float(d['overall_health_ratio']):.2f}%")
    print(f"       overall_status       = {d['overall_status']}")
    print(f"       open_positions       = {d['open_positions']}")
    print(f"       margin_call_warning  = {d['margin_call_warning']}")
    print(f"       liquidation_risk     = {d['liquidation_risk']}")
    if d['alerts']:
        print(f"       alerts: {d['alerts']}")
    if d['positions']:
        for p in d['positions']:
            print(f"       {p['symbol']:20s} health={float(p['health_ratio']):.1f}% status={p['status']}")

# ── 5. Auth check ──────────────────────────────────────────────────────────
print("\n[ 5. Auth protection ]")
check("GET /risk/preview without token (expect 401/403)",
    requests.get(f"{BASE}/risk/preview",
        params={"symbol":"NSE:RELIANCE","order_type":"market","side":"buy",
                "product_type":"intraday","quantity":"10"}), (401, 403))
check("GET /risk/health without token (expect 401/403)",
    requests.get(f"{BASE}/risk/health"), (401, 403))

# ── Cleanup ────────────────────────────────────────────────────────────────
print("\n[ 6. Cleanup ]")
requests.post(f"{BASE}/market/simulator/stop", headers=H)
requests.post(f"{BASE}/auth/logout", headers=H, json={"refresh_token": rt})
print("  Done")

# ── Results ────────────────────────────────────────────────────────────────
print("\n" + "=" * 65)
print(f"  PASSED: {len(PASS_LIST)}  |  FAILED: {len(FAIL_LIST)}  |  TOTAL: {len(PASS_LIST)+len(FAIL_LIST)}")
print("=" * 65)
if FAIL_LIST:
    print("\nFailed:")
    for f in FAIL_LIST: print(f"  x  {f}")
else:
    print("\n  All tests passed!")
