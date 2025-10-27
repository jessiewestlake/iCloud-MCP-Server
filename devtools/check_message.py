"""Utility script to inspect message bodies via the MCP server."""

import asyncio
import json

import fastmcp


async def main() -> None:
    client = fastmcp.Client("http://127.0.0.1:8800/mcp", timeout=30)
    async with client:
        listing = await client.call_tool("list_messages", {"mailbox": "INBOX", "limit": 1})
        struct = listing.structured_content or {}
        rows = struct.get("result") if isinstance(struct, dict) else struct
        print("LIST:", json.dumps(struct, indent=2))
        if not rows:
            print("No messages returned")
            return
        uid = rows[0].get("uid")
        if not uid:
            print("First message missing UID")
            return
        detail = await client.call_tool("get_message", {"mailbox": "INBOX", "uid": uid})
        print("DETAIL structured:", json.dumps(detail.structured_content, indent=2))
        for idx, item in enumerate(detail.content or []):
            value = getattr(item, "text", getattr(item, "data", None))
            print(f"DETAIL content[{idx}] type={item.type} value={value}")


if __name__ == "__main__":
    asyncio.run(main())
