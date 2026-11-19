import json, os, urllib.request, urllib.error
addr = os.environ.get("GATEWAY_ADDR", "127.0.0.1:8081")
tok = os.environ.get("GATEWAY_TOKEN", "")
paths = ["/healthz", "/v1/stats", "/stats", "/v1/pool/stats", "/", "/v1/lease/acquire"]
for p in paths:
    try:
        req = urllib.request.Request(f"http://{addr}{p}", headers={"Authorization": f"Bearer {tok}"}, method="GET")
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read().decode("utf-8")[:300]
            print(p, r.status, body)
    except urllib.error.HTTPError as e:
        print(p, "HTTP", e.code, e.read().decode("utf-8","ignore")[:200])
    except Exception as e:
        print(p, "ERR", type(e).__name__, str(e)[:120])
