"""ap-mcp：MCP stdio Server（NDJSON JSON-RPC 2.0）

宿主 IDE（Trae / opencode / Codex）将其注册为 MCP 工具面；
ap 自身不调用大模型 —— 推理与编排由宿主 IDE agent 完成（token 计 IDE 订阅）。
"""
from __future__ import annotations
import json, sys
from .ctx import Ctx
from .tools import TOOLS, call_tool

PROTOCOL = "2024-11-05"
SERVER_INFO = {"name": "ap-mcp", "version": "0.1.0-alpha",
               "desc": "AlphaPocket 本地投研运行时工具面（V4-α）"}


def _tool_desc(name, t):
    return {"name": name, "description": t["desc"],
            "inputSchema": {"type": "object", "properties": {"args": {"type": "object"}}}}


def handle(msg: dict, ctx: Ctx) -> dict | None:
    m = msg.get("method", "")
    rid = msg.get("id")
    if m == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}}
    if m == "notifications/initialized":
        return None
    if m == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [_tool_desc(n, t) for n, t in TOOLS.items()]}}
    if m == "tools/call":
        p = msg.get("params", {})
        name = p.get("name")
        args = (p.get("arguments") or {}).get("args", {})
        try:
            res = call_tool(name, args, ctx)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, default=str)}],
                "isError": False}}
        except Exception as e:  # 工具异常不崩 server
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps({"error": str(e)}, ensure_ascii=False)}],
                "isError": True}}
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {m}"}}
    return None


def serve(ctx: Ctx | None = None):
    ctx = ctx or Ctx()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg, ctx)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
            sys.stdout.flush()
