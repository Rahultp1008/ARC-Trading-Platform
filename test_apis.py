import requests, time
BASE = "http://127.0.0.1:8000/api/v1"

r = requests.post(f"{BASE}/auth/login", json={"email":"admin@arc.com","password":"Admin@1234"})
token = r.json()["access_token"]
rt = r.json()["refresh_token"]
H = {"Authorization": "Bearer " + token}

results = []
results.append(("POST /auth/login", r.status_code))

r = requests.post(f"{BASE}/auth/refresh", json={"refresh_token": rt})
results.append(("POST /auth/refresh", r.status_code))

r = requests.get(f"{BASE}/auth/me", headers=H)
results.append(("GET  /auth/me", r.status_code))

r = requests.get(f"{BASE}/admin/brokers", headers=H)
results.append(("GET  /admin/brokers", r.status_code))

r = requests.get(f"{BASE}/users", headers=H)
results.append(("GET  /users", r.status_code))

r = requests.get(f"{BASE}/kyc/queue", headers=H)
results.append(("GET  /kyc/queue", r.status_code))

r = requests.get(f"{BASE}/instruments", headers=H)
results.append(("GET  /instruments", r.status_code))

r = requests.post(f"{BASE}/funding/users/2/credit", json={"amount":50000,"reference":"TXN001","notes":"deposit","reason":"deposit"}, headers=H)
results.append(("POST /funding/credit", r.status_code))

r = requests.get(f"{BASE}/funding/users/2/balance", headers=H)
results.append(("GET  /funding/balance", r.status_code))

r = requests.post(f"{BASE}/market/simulator/start", headers=H)
results.append(("POST /market/simulator/start", r.status_code))

time.sleep(3)

r = requests.get(f"{BASE}/market/status", headers=H)
results.append(("GET  /market/status", r.status_code))

r = requests.get(f"{BASE}/quotes/NSE:RELIANCE", headers=H)
results.append(("GET  /quotes/NSE:RELIANCE", r.status_code))

r = requests.get(f"{BASE}/quotes/NSE:RELIANCE/ltp", headers=H)
results.append(("GET  /quotes/ltp", r.status_code))

r = requests.get(f"{BASE}/ohlc/NSE:RELIANCE?interval=1m", headers=H)
results.append(("GET  /ohlc", r.status_code))

r = requests.post(f"{BASE}/auth/logout", json={"refresh_token": rt}, headers=H)
results.append(("POST /auth/logout", r.status_code))

print()
passed = sum(1 for _, c in results if c in (200, 201))
failed = len(results) - passed
for name, code in results:
    icon = "PASS" if code in (200, 201) else "FAIL"
    print(f"{icon}  {name}: {code}")
print(f"\nResult: {passed} PASSED   {failed} FAILED   out of {len(results)} total")
