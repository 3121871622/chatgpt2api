import json, os, urllib.request
addr = os.environ.get("GATEWAY_ADDR", "127.0.0.1:8081")
tok = os.environ.get("GATEWAY_TOKEN", "")
url = f"http://{addr}/stats"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
with urllib.request.urlopen(req, timeout=15) as r:
    print(r.read().decode("utf-8"))
