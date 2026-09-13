import subprocess, time, json, urllib.request
PORT = 8102
TOK = "testtok"
srv = subprocess.Popen(
    [r"C:\anaconda3\python.exe", "tools/tdlas_mcp.py", "--http",
     "--host", "127.0.0.1", "--port", str(PORT), "--token", TOK],
    cwd=r"C:\Users\16565\Desktop\HITRAN\tdlas-mcp",
    stderr=subprocess.PIPE, text=True)
print("server starting...", flush=True)
time.sleep(3)
body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "tdlas_simulate",
                              "arguments": {"species": "CO2", "wn0": 6359.0,
                                            "x": 0.05, "L": 330.0}}}).encode()
req = urllib.request.Request(
    f"http://127.0.0.1:{PORT}/mcp", data=body,
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOK}"})
print("calling tools/call...", flush=True)
resp = json.loads(urllib.request.urlopen(req, timeout=180).read())
txt = resp["result"]["content"][0]["text"]
print("server-side has <workspace>:", "<workspace>" in txt, flush=True)
print("server-side leaks 16565:", "16565" in txt, flush=True)
srv.terminate()
