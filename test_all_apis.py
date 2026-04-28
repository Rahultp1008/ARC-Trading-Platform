import requests, time

BASE = "http://127.0.0.1:8000/api/v1"
results = []

def test(name, resp, expected=(200,201)):
    ok = resp.status_code in expected
    results.append(("PASS" if ok else "FAIL", name, resp.status_code))
    if not ok:
        try: print(f"  detail: {resp.json()}")
        except: pass
    return resp

print("\n" + "="*55)
print("  ARC TRADING - FULL API TEST")
print("="*55)

print("\n[ MODULE 1 - Auth ]")
r = test("POST /auth/login", requests.post(f"{BASE}/auth/login", json={"email":"admin@arc.com","password":"Admin@1234"}))
token = r.json().get("access_token","")
rt = r.json().get("refresh_token","")
H = {"Authorization": f"Bearer {token}"}
test("GET  /auth/me", requests.get(f"{BASE}/auth/me", headers=H))
r2 = test("POST /auth/refresh", requests.post(f"{BASE}/auth/refresh", json={"refresh_token": rt}))
H = {"Authorization": "Bearer " + r2.json().get("access_token", token)}
new_rt = r2.json().get("refresh_token", rt)

print("\n[ MODULE 2 - Users and Brokers ]")
test("GET  /admin/brokers", requests.get(f"{BASE}/admin/brokers", headers=H))
test("GET  /users", requests.get(f"{BASE}/users", headers=H))

import random, string
rand = "".join(random.choices(string.digits, k=6))
r3 = test("POST /users (create)", requests.post(f"{BASE}/users", headers=H, json={"email":f"user{rand}@arc.com","full_name":"Test User","password":"Test@123"}), (200,201))
uid = r3.json().get("id") if r3.status_code in (200,201) else None

if uid:
    test(f"GET  /users/{uid}", requests.get(f"{BASE}/users/{uid}", headers=H))
    test(f"PATCH /users/{uid}/status (suspend)", requests.patch(f"{BASE}/users/{uid}/status", headers=H, json={"status":"suspended","reason":"test suspend"}))
    test(f"PATCH /users/{uid}/status (activate)", requests.patch(f"{BASE}/users/{uid}/status", headers=H, json={"status":"active","reason":"test activate"}))

print("\n[ MODULE 3 - KYC ]")
test("GET  /kyc/upload-url pan_card", requests.get(f"{BASE}/kyc/upload-url?doc_type=pan_card", headers=H))
test("GET  /kyc/upload-url aadhaar", requests.get(f"{BASE}/kyc/upload-url?doc_type=aadhaar", headers=H))
test("GET  /kyc/queue", requests.get(f"{BASE}/kyc/queue", headers=H))

print("\n[ MODULE 4 - Instruments ]")
r4 = test("GET  /instruments (all)", requests.get(f"{BASE}/instruments", headers=H))
print(f"         total: {r4.json().get('total',0)} instruments")
iid = r4.json()["items"][0]["id"] if r4.json().get("items") else None
test("GET  /instruments?q=RELI", requests.get(f"{BASE}/instruments?q=RELI", headers=H))
test("GET  /instruments?instrument_type=equity", requests.get(f"{BASE}/instruments?instrument_type=equity", headers=H))
test("GET  /instruments?instrument_type=crypto", requests.get(f"{BASE}/instruments?instrument_type=crypto", headers=H))
if iid:
    test("GET  /instruments/id", requests.get(f"{BASE}/instruments/{iid}", headers=H))
    test("PATCH /instruments/toggle off", requests.patch(f"{BASE}/instruments/{iid}/toggle", headers=H, json={"is_active":False,"trading_allowed":False,"reason":"test"}))
    test("PATCH /instruments/toggle on", requests.patch(f"{BASE}/instruments/{iid}/toggle", headers=H, json={"is_active":True,"trading_allowed":True,"reason":"restore"}))

print("\n[ MODULE 10 - Funding ]")
tid = uid if uid else 2
test(f"POST /funding/credit", requests.post(f"{BASE}/funding/users/{tid}/credit", headers=H, json={"amount":50000,"reference":f"REF{rand}","notes":"test","reason":"deposit"}), (200,201))
test(f"GET  /funding/balance", requests.get(f"{BASE}/funding/users/{tid}/balance", headers=H))
test(f"POST /funding/debit", requests.post(f"{BASE}/funding/users/{tid}/debit", headers=H, json={"amount":5000,"reference":f"WTH{rand}","notes":"test","reason":"withdrawal"}), (200,201))
test(f"GET  /funding/logs", requests.get(f"{BASE}/funding/users/{tid}/logs", headers=H))
test(f"GET  /funding/ledger", requests.get(f"{BASE}/funding/users/{tid}/ledger", headers=H))
test("GET  /funding/me/balance", requests.get(f"{BASE}/funding/me/balance", headers=H))
test("GET  /funding/me/logs", requests.get(f"{BASE}/funding/me/logs", headers=H))

print("\n[ MODULE 5 - Market Data ]")
test("POST /market/simulator/start", requests.post(f"{BASE}/market/simulator/start", headers=H))
test("GET  /market/simulator/status", requests.get(f"{BASE}/market/simulator/status", headers=H))
test("GET  /market/status", requests.get(f"{BASE}/market/status", headers=H))
print("         waiting 3 seconds for ticks...")
time.sleep(3)
test("GET  /quotes (all)", requests.get(f"{BASE}/quotes", headers=H))
test("GET  /quotes?segment=CASH", requests.get(f"{BASE}/quotes?segment=CASH", headers=H))
test("GET  /quotes?segment=CRYPTO", requests.get(f"{BASE}/quotes?segment=CRYPTO", headers=H))
test("GET  /quotes/NSE:RELIANCE", requests.get(f"{BASE}/quotes/NSE:RELIANCE", headers=H))
test("GET  /quotes/NSE:RELIANCE/ltp", requests.get(f"{BASE}/quotes/NSE:RELIANCE/ltp", headers=H))
test("GET  /quotes/CRYPTO:BTCUSDT", requests.get(f"{BASE}/quotes/CRYPTO:BTCUSDT", headers=H))
test("GET  /quotes/CRYPTO:ETHUSDT/ltp", requests.get(f"{BASE}/quotes/CRYPTO:ETHUSDT/ltp", headers=H))
test("GET  /ohlc/NSE:RELIANCE?interval=1m", requests.get(f"{BASE}/ohlc/NSE:RELIANCE?interval=1m", headers=H))
test("GET  /ohlc/NSE:RELIANCE?interval=5m", requests.get(f"{BASE}/ohlc/NSE:RELIANCE?interval=5m", headers=H))
test("GET  /ohlc/CRYPTO:BTCUSDT?interval=1m", requests.get(f"{BASE}/ohlc/CRYPTO:BTCUSDT?interval=1m", headers=H))
test("GET  /ohlc/NSE:RELIANCE/series", requests.get(f"{BASE}/ohlc/NSE:RELIANCE/series?interval=1m", headers=H))
test("POST /market/simulator/stop", requests.post(f"{BASE}/market/simulator/stop", headers=H))

print("\n[ Cleanup ]")
test("POST /auth/logout", requests.post(f"{BASE}/auth/logout", headers=H, json={"refresh_token": new_rt}))

print("\n" + "="*55)
passed = [r for r in results if r[0]=="PASS"]
failed = [r for r in results if r[0]=="FAIL"]
for icon,name,code in results:
    print(f"  {icon}  {name}: {code}")
print("="*55)
print(f"  PASSED: {len(passed)}  |  FAILED: {len(failed)}  |  TOTAL: {len(results)}")
print("="*55)
if failed:
    print("\nFAILED:")
    for _,name,code in failed:
        print(f"  x {name}: {code}")
