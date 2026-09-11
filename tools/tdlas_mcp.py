# TDLAS MCP 服务器骨架
# 开发中，后续逐步实现

import sys
import json

SERVER_INFO = {
    "name": "tdlas-mcp",
    "version": "0.1.0",
    "description": "TDLAS/WMS spectroscopic simulation MCP server",
    "protocolVersion": "2024-11-05",
    "capabilities": {
        "tools": {}
    }
}

DISPATCH = {}


def handle_request(req):
    """处理 MCP 请求"""
    method = req.get("method", "")
    params = req.get("params", {})
    req_id = req.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": SERVER_INFO}

    elif method == "tools/list":
        tools = [
            {
                "name": name,
                "description": fn.__doc__ or "",
                "inputSchema": {"type": "object", "properties": {}}
            }
            for name, fn in DISPATCH.items()
        ]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        if name in DISPATCH:
            try:
                result = DISPATCH[name](**args)
                return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": str(result)}]}}
            except Exception as e:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(e)}}
        else:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown tool: {name}"}}

    else:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


def main():
    """stdio MCP 服务器主循环"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            print(json.dumps(resp))
            sys.stdout.flush()
        except Exception as e:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": str(e)}}))
            sys.stdout.flush()


if __name__ == "__main__":
    main()
