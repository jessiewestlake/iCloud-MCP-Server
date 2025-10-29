import asyncio
import datetime as dt
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from fastmcp import Client

# Load the user's iCloud credentials from the project-level .env file so the
# live tests authenticate with real iCloud services.
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_PATH, override=False)

REQUIRED_ENV_VARS = ("APPLE_ID", "ICLOUD_APP_PASSWORD")
MISSING_VARS = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
if MISSING_VARS:
    pytest.skip(
        "Missing required environment variables for live iCloud tests",
        allow_module_level=True,
    )

import server  # noqa: E402  pylint: disable=wrong-import-position

TEST_MAILBOX = os.environ.get("ICLOUD_TEST_MAILBOX", "INBOX")


def test_live_icloud_read_only_tools() -> None:
    """Exercise read-only MCP tools against the live iCloud account."""
    client = Client(server.mcp)

    async def _exercise() -> None:
        async with client:
            mailboxes_result = await client.call_tool_mcp("list_mailboxes", {})
            assert not mailboxes_result.isError
            mailboxes = mailboxes_result.structuredContent.get("mailboxes", [])
            assert isinstance(mailboxes, list)

            messages_result = await client.call_tool_mcp(
                "list_messages", {"mailbox": TEST_MAILBOX}
            )
            assert not messages_result.isError
            messages = messages_result.structuredContent.get("messages", [])
            if not messages:
                pytest.skip(f"Mailbox {TEST_MAILBOX} is empty; cannot validate message tools")

            first_msg = messages[0]
            uid = str(first_msg.get("uid") or "").strip()
            assert uid

            search_query = (
                (first_msg.get("subject") or first_msg.get("from") or "@")[:100]
                or "@"
            )
            search_result = await client.call_tool_mcp(
                "search_messages",
                {"mailbox": TEST_MAILBOX, "query": search_query},
            )
            assert not search_result.isError
            assert "uids" in search_result.structuredContent

            message_result = await client.call_tool_mcp(
                "get_message", {"mailbox": TEST_MAILBOX, "uid": uid}
            )
            assert not message_result.isError
            message_struct = message_result.structuredContent
            assert message_struct.get("uid") == uid

            attachments = message_struct.get("attachments") or []
            if attachments:
                attachment_id = str(attachments[0].get("attachment_id"))
                attachment_result = await client.call_tool_mcp(
                    "download_attachment",
                    {"mailbox": TEST_MAILBOX, "uid": uid, "attachment_id": attachment_id},
                )
                assert not attachment_result.isError
                assert attachment_result.structuredContent.get("found") is True
                payload = attachment_result.structuredContent.get("attachment", {}).get("data")
                assert payload

            calendars_result = await client.call_tool_mcp("list_calendars", {})
            assert not calendars_result.isError
            calendars = calendars_result.structuredContent.get("calendars", [])
            if not calendars:
                pytest.skip("No calendars available; cannot validate calendar tools")

            calendar_identifier = (
                calendars[0].get("url")
                or calendars[0].get("name")
                or calendars[0].get("id")
            )
            assert calendar_identifier
            calendar_identifier = str(calendar_identifier)

            now = dt.datetime.now(dt.timezone.utc)
            start = (now - dt.timedelta(days=30)).isoformat()
            end = (now + dt.timedelta(days=30)).isoformat()

            events_result = await client.call_tool_mcp(
                "list_events",
                {
                    "calendar_name_or_url": calendar_identifier,
                    "start": start,
                    "end": end,
                },
            )
            assert not events_result.isError
            events = events_result.structuredContent.get("events", [])

            search_events_result = await client.call_tool_mcp(
                "search_events", {"query": "meeting"}
            )
            assert not search_events_result.isError
            assert "results" in search_events_result.structuredContent

            fetch_ids = []
            if events:
                first_event = events[0]
                event_uid = first_event.get("uid")
                if event_uid:
                    fetch_ids.append(f"{calendar_identifier}|{event_uid}")
            search_hits = search_events_result.structuredContent.get("results") or []
            if not fetch_ids and search_hits:
                fetch_ids.append(search_hits[0].get("id"))
            fetch_ids = [fid for fid in fetch_ids if fid]

            if fetch_ids:
                fetch_result = await client.call_tool_mcp(
                    "fetch_events", {"ids": fetch_ids}
                )
                assert not fetch_result.isError
                assert "events" in fetch_result.structuredContent

    asyncio.run(_exercise())
