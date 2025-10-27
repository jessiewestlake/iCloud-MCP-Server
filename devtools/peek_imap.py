"""Utility script to inspect raw IMAP payloads."""

import asyncio
import base64
import json

import fastmcp


async def main() -> None:
    client = fastmcp.Client("http://127.0.0.1:8800/mcp", timeout=30)
    async with client:
        uid = input("UID: ")
        mailbox = input("Mailbox [INBOX]: ") or "INBOX"
        resp = await client.call_tool(
            "_peek_imap", {"mailbox": mailbox, "uid": uid, "section": "BODY.PEEK[]"}
        )
        payload = resp.structured_content or {}
        print("Structured:", json.dumps(payload, indent=2))
        for idx, item in enumerate(resp.content or []):
            if item.type == "text" and item.text:
                print(f"TEXT chunk[{idx}]\n{item.text}")
            elif item.type == "bytes" and item.data:
                print(f"BYTES chunk[{idx}] len={len(item.data)}")
                try:
                    raw = base64.b64decode(item.data)
                    print(raw[:200])
                except Exception as err:  # noqa: BLE001
                    print("Failed to decode bytes chunk:", err)


if __name__ == "__main__":
    asyncio.run(main())
