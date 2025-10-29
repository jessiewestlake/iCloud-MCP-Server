import asyncio
import base64
import datetime as dt
import os
from email.message import EmailMessage
from typing import Any, Dict, List

import pytest
from fastmcp import Client

os.environ.setdefault("APPLE_ID", "test@example.com")
os.environ.setdefault("ICLOUD_APP_PASSWORD", "password123")

import server  # noqa: E402  pylint: disable=wrong-import-position


class FakeSMTP:
    def __init__(self) -> None:
        self.sent_messages: List[EmailMessage] = []

    def send_message(self, msg: EmailMessage) -> None:
        if msg.get("Message-ID") is None:
            msg["Message-ID"] = "<fake-sent@local>"
        self.sent_messages.append(msg)

    def quit(self) -> None:  # pragma: no cover - nothing to clean up
        return


class FakeIMAP:
    def __init__(self, message_bytes: bytes, header_bytes: bytes) -> None:
        self._message_bytes = message_bytes
        self._header_bytes = header_bytes
        self.append_calls: List[bytes] = []

    def list(self) -> tuple[str, List[bytes]]:
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, List[bytes]]:
        return "OK", [b"1"]

    def uid(self, command: str, *args: Any) -> tuple[str, List[Any]]:
        if command == "SEARCH":
            return "OK", [b"1"]
        if command == "FETCH":
            if len(args) >= 2 and "HEADER.FIELDS" in str(args[1]):
                return "OK", [(b'1 (FLAGS (\\Seen) RFC822.SIZE 321)', self._header_bytes)]
            if len(args) >= 2 and "BODY.PEEK[]" in str(args[1]):
                return "OK", [(b'1 (FLAGS (\\Seen))', self._message_bytes)]
            if len(args) >= 2 and args[1] == '(BODY.PEEK[])':
                return "OK", [(b'1 (FLAGS (\\Seen))', self._message_bytes)]
        if command == "COPY":
            return "OK", [b"copied"]
        if command == "STORE":
            return "OK", [b"stored"]
        return "OK", []

    def append(self, mailbox: str, flags: str, timestamp: Any, raw_bytes: bytes) -> tuple[str, List[bytes]]:
        self.append_calls.append(raw_bytes)
        return "OK", []

    def expunge(self) -> tuple[str, List[bytes]]:
        return "OK", []

    def close(self) -> tuple[str, List[bytes]]:
        return "OK", []

    def logout(self) -> tuple[str, List[bytes]]:
        return "OK", []


class FakeParamValue:
    def __init__(self, value: dt.datetime, tzid: str = "America/New_York") -> None:
        self.value = value
        self.params: Dict[str, str] = {"TZID": tzid}


class FakeComponent:
    def __init__(
        self,
        summary: str = "Sprint Planning",
        description: str = "Discuss upcoming work",
        uid: str = "event-123",
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> None:
        self.summary = summary
        self.description = description
        self.uid = uid
        self.start = start or dt.datetime(2024, 1, 1, 13, 0, tzinfo=dt.timezone.utc)
        self.end = end or (self.start + dt.timedelta(hours=1))
        self.recurrence_id = None
        self.dtstart_holder = FakeParamValue(self.start)
        self.dtend_holder = FakeParamValue(self.end)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "summary":
            return self.summary
        if key == "description":
            return self.description
        if key == "uid":
            return self.uid
        if key == "recurrence-id":
            return self.recurrence_id
        if key == "dtstart":
            return self.dtstart_holder
        if key == "dtend":
            return self.dtend_holder
        return default

    def decoded(self, key: str, default: Any = None) -> Any:
        if key == "dtstart":
            return self.start
        if key == "dtend":
            return self.end
        return default

    def __contains__(self, key: str) -> bool:
        return key in {"dtstart", "dtend", "summary", "description", "uid"}

    def __getitem__(self, key: str) -> FakeParamValue:
        if key == "dtstart":
            return self.dtstart_holder
        if key == "dtend":
            return self.dtend_holder
        raise KeyError(key)


class FakeEvent:
    def __init__(self) -> None:
        self.component = FakeComponent()
        self.data = "BEGIN:VCALENDAR\nEND:VCALENDAR"
        self.saved = False
        self.deleted = False

    def save(self) -> None:
        self.saved = True

    def delete(self) -> None:
        self.deleted = True


class FakeCalendar:
    def __init__(self) -> None:
        self.name = "Work"
        self.url = "https://example.com/cal/work"
        self.id = "work"
        self.saved_events: List[str] = []

    def search(self, *args: Any, **kwargs: Any) -> List[FakeEvent]:
        return [FakeEvent()]

    def save_event(self, ics_data: str) -> None:
        self.saved_events.append(ics_data)


@pytest.fixture(scope="module")
def sample_email_bytes() -> tuple[bytes, bytes]:
    msg = EmailMessage()
    msg["Subject"] = "Stub Subject"
    msg["From"] = "sender@example.com"
    msg["To"] = "receiver@example.com"
    msg.set_content("Plain body text")
    msg.add_attachment(
        b"attachment-bytes",
        maintype="application",
        subtype="octet-stream",
        filename="stub.bin",
    )
    raw_bytes = msg.as_bytes()
    separator = b"\r\n\r\n"
    if separator in raw_bytes:
        header_bytes = raw_bytes.split(separator, 1)[0] + separator
    else:
        header_bytes = raw_bytes
    return raw_bytes, header_bytes


@pytest.fixture()
def patched_server(
    monkeypatch: pytest.MonkeyPatch, sample_email_bytes: tuple[bytes, bytes]
) -> Dict[str, Any]:
    raw_bytes, header_bytes = sample_email_bytes

    monkeypatch.setattr(server, "_open_imap", lambda: FakeIMAP(raw_bytes, header_bytes))
    monkeypatch.setattr(server, "_open_smtp", lambda: FakeSMTP())

    fake_calendar = FakeCalendar()
    monkeypatch.setattr(server, "DAVClient", object())
    monkeypatch.setattr(server, "_caldav_all_calendars", lambda: [fake_calendar])
    monkeypatch.setattr(server, "_caldav_resolve_calendar", lambda name: fake_calendar)

    return {"calendar": fake_calendar}


def test_mcp_tools_return_structured_content(patched_server: Dict[str, Any]) -> None:
    calendar: FakeCalendar = patched_server["calendar"]
    client = Client(server.mcp)

    async def _exercise() -> None:
        async with client:
            tool_names = {tool.name for tool in await client.list_tools()}
            expected_names = {
                "list_mailboxes",
                "list_messages",
                "search_messages",
                "get_message",
                "download_attachment",
                "send_message",
                "create_draft",
                "move_message",
                "delete_message",
                "archive_message",
                "flag_message",
                "list_calendars",
                "list_events",
                "create_event",
                "update_event",
                "delete_event",
                "search_events",
                "fetch_events",
            }
            assert expected_names.issubset(tool_names)

            result = await client.call_tool_mcp("list_mailboxes", {})
            assert not result.isError
            assert "mailboxes" in result.structuredContent

            result = await client.call_tool_mcp("list_messages", {"mailbox": "INBOX"})
            assert not result.isError
            assert "messages" in result.structuredContent

            result = await client.call_tool_mcp(
                "search_messages",
                {"mailbox": "INBOX", "query": "stub"},
            )
            assert not result.isError
            assert result.structuredContent.get("uids")

            result = await client.call_tool_mcp(
                "get_message",
                {"mailbox": "INBOX", "uid": "1"},
            )
            assert not result.isError
            assert result.structuredContent.get("uid") == "1"

            attachment_result = await client.call_tool_mcp(
                "download_attachment",
                {"mailbox": "INBOX", "uid": "1", "attachment_id": "0"},
            )
            assert not attachment_result.isError
            assert attachment_result.structuredContent.get("found") is True
            encoded_data = attachment_result.structuredContent["attachment"]["data"]
            assert base64.b64decode(encoded_data)

            result = await client.call_tool_mcp(
                "send_message",
                {"to": ["user@example.com"], "subject": "Test", "body": "Hi"},
            )
            assert not result.isError
            assert "messageId" in result.structuredContent

            result = await client.call_tool_mcp(
                "create_draft",
                {"to": ["user@example.com"], "subject": "Draft", "body": "Hi"},
            )
            assert not result.isError
            assert "messageId" in result.structuredContent

            result = await client.call_tool_mcp(
                "move_message",
                {"mailbox": "INBOX", "uid": "1", "dest_mailbox": "Archive"},
            )
            assert not result.isError
            assert result.structuredContent.get("success") is True

            result = await client.call_tool_mcp(
                "delete_message",
                {"mailbox": "INBOX", "uid": "1"},
            )
            assert not result.isError
            assert result.structuredContent.get("success") is True

            result = await client.call_tool_mcp(
                "archive_message",
                {"mailbox": "INBOX", "uid": "1"},
            )
            assert not result.isError
            assert result.structuredContent.get("success") is True

            result = await client.call_tool_mcp(
                "flag_message",
                {"mailbox": "INBOX", "uid": "1", "flag": "\\Seen", "value": True},
            )
            assert not result.isError
            assert result.structuredContent.get("success") is True

            result = await client.call_tool_mcp("list_calendars", {})
            assert not result.isError
            assert result.structuredContent.get("calendars")

            start = dt.datetime.now(dt.timezone.utc)
            end = start + dt.timedelta(days=1)
            events_result = await client.call_tool_mcp(
                "list_events",
                {
                    "calendar_name_or_url": calendar.url,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
            )
            assert not events_result.isError
            assert events_result.structuredContent.get("events")

            create_event_result = await client.call_tool_mcp(
                "create_event",
                {
                    "calendar_name_or_url": calendar.url,
                    "summary": "New Meeting",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
            )
            assert not create_event_result.isError
            assert create_event_result.structuredContent.get("created")
            assert calendar.saved_events

            update_result = await client.call_tool_mcp(
                "update_event",
                {
                    "calendar_name_or_url": calendar.url,
                    "uid": "event-123",
                    "summary": "Updated Meeting",
                },
            )
            assert not update_result.isError
            assert update_result.structuredContent.get("success") is True

            delete_result = await client.call_tool_mcp(
                "delete_event",
                {"calendar_name_or_url": calendar.url, "uid": "event-123"},
            )
            assert not delete_result.isError
            assert delete_result.structuredContent.get("success") is True

            search_result = await client.call_tool_mcp(
                "search_events",
                {"query": "sprint"},
            )
            assert not search_result.isError
            search_results = search_result.structuredContent.get("results")
            assert search_results

            fetch_result = await client.call_tool_mcp(
                "fetch_events",
                {"ids": [search_results[0]["id"]]},
            )
            assert not fetch_result.isError
            assert fetch_result.structuredContent.get("events")

    asyncio.run(_exercise())
