import sys, json
sys.path.insert(0, r"C:\Users\16565\Desktop\HITRAN\tdlas-mcp\tools")
import tdlas_mcp as m

# 复刻 do_POST 的完整链路：_sanitize_paths(handle_request(req))
r = m._sanitize_paths(m.handle_request(
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
     "params": {"name": "tdlas_simulate",
                "arguments": {"species": "CO2", "wn0": 6359.0, "x": 0.05, "L": 330.0}}}))

txt = r["result"]["content"][0]["text"]
res = json.loads(txt)
leak = "16565" in txt or r"C:\Users" in txt
print("泄露 16565/C:\\Users:", leak)
print("出现 <workspace>:", "<workspace>" in txt)
# 展示脱敏后的 log 相关行
for ln in res.get("log", []):
    if "Hitran" in ln or "workspace" in ln or "home" in ln:
        print("  log:", ln[:140])
print("alpha_peak:", res.get("alpha_peak"), "n_lines:", res.get("n_lines_in_window"))
sys.exit(1 if leak else 0)
