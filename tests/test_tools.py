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

icalendar = pytest.importorskip("icalendar")
from icalendar import Alarm, Calendar, Event  # noqa: E402  pylint: disable=wrong-import-position

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


class FakeEvent:
    def __init__(self) -> None:
        start = dt.datetime(2024, 1, 1, 13, 0, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(hours=1)
        calendar = Calendar()
        calendar.add('prodid', '-//Tests//EN')
        calendar.add('version', '2.0')
        event = Event()
        event.add('uid', 'event-123')
        event.add('summary', 'Sprint Planning')
        event.add('description', 'Discuss upcoming work')
        event.add('dtstart', start)
        event.add('dtend', end)
        alarm = Alarm()
        alarm.add('action', 'DISPLAY')
        alarm.add('description', 'Initial Reminder')
        alarm.add('trigger', dt.timedelta(minutes=-30))
        event.add_component(alarm)
        calendar.add_component(event)
        self.data = calendar.to_ical().decode()
        self.saved = False
        self.deleted = False

    @property
    def component(self):
        cal = Calendar.from_ical(self.data)
        for comp in cal.walk('VEVENT'):
            return comp
        raise AssertionError("No VEVENT in fake event")

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
        self._event = FakeEvent()

    @property
    def event(self) -> FakeEvent:
        return self._event

    def search(self, *args: Any, **kwargs: Any) -> List[FakeEvent]:
        return [self._event]

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

    return {"calendar": fake_calendar, "event": fake_calendar.event}


def test_mcp_tools_return_structured_content(patched_server: Dict[str, Any]) -> None:
    calendar: FakeCalendar = patched_server["calendar"]
    fake_event: FakeEvent = patched_server["event"]
    client = Client(server.mcp)

    async def _exercise() -> None:
        async with client:
            def _structured(res) -> Dict[str, Any]:
                sc = res.structuredContent
                assert sc is not None
                return sc

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
            assert "mailboxes" in _structured(result)

            result = await client.call_tool_mcp("list_messages", {"mailbox": "INBOX"})
            assert not result.isError
            assert "messages" in _structured(result)

            result = await client.call_tool_mcp(
                "search_messages",
                {"mailbox": "INBOX", "query": "stub"},
            )
            assert not result.isError
            assert _structured(result).get("uids")

            result = await client.call_tool_mcp(
                "get_message",
                {"mailbox": "INBOX", "uid": "1"},
            )
            assert not result.isError
            assert _structured(result).get("uid") == "1"

            attachment_result = await client.call_tool_mcp(
                "download_attachment",
                {"mailbox": "INBOX", "uid": "1", "attachment_id": "0"},
            )
            assert not attachment_result.isError
            attachment_data = _structured(attachment_result)
            assert attachment_data.get("found") is True
            encoded_data = attachment_data["attachment"]["data"]
            assert base64.b64decode(encoded_data)

            result = await client.call_tool_mcp(
                "send_message",
                {"to": ["user@example.com"], "subject": "Test", "body": "Hi"},
            )
            assert not result.isError
            assert "messageId" in _structured(result)

            result = await client.call_tool_mcp(
                "create_draft",
                {"to": ["user@example.com"], "subject": "Draft", "body": "Hi"},
            )
            assert not result.isError
            assert "messageId" in _structured(result)

            result = await client.call_tool_mcp(
                "move_message",
                {"mailbox": "INBOX", "uid": "1", "dest_mailbox": "Archive"},
            )
            assert not result.isError
            assert _structured(result).get("success") is True

            result = await client.call_tool_mcp(
                "delete_message",
                {"mailbox": "INBOX", "uid": "1"},
            )
            assert not result.isError
            assert _structured(result).get("success") is True

            result = await client.call_tool_mcp(
                "archive_message",
                {"mailbox": "INBOX", "uid": "1"},
            )
            assert not result.isError
            assert _structured(result).get("success") is True

            result = await client.call_tool_mcp(
                "flag_message",
                {"mailbox": "INBOX", "uid": "1", "flag": "\\Seen", "value": True},
            )
            assert not result.isError
            assert _structured(result).get("success") is True

            result = await client.call_tool_mcp("list_calendars", {})
            assert not result.isError
            assert _structured(result).get("calendars")

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
            assert _structured(events_result).get("events")

            create_event_result = await client.call_tool_mcp(
                "create_event",
                {
                    "calendar_name_or_url": calendar.url,
                    "summary": "New Meeting",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "location": "Conference Room A",
                    "url": "https://example.com/new-meeting",
                    "reminders": [
                        {"minutes_before_start": 30, "description": "Prep"},
                    ],
                },
            )
            assert not create_event_result.isError
            assert _structured(create_event_result).get("created")
            assert calendar.saved_events
            created_calendar = Calendar.from_ical(calendar.saved_events[-1])
            created_events = list(created_calendar.walk('VEVENT'))
            assert created_events
            created_event = created_events[0]
            assert str(created_event.get('location')) == "Conference Room A"
            assert str(created_event.get('url')) == "https://example.com/new-meeting"
            created_alarms = [comp for comp in getattr(created_event, 'subcomponents', []) if getattr(comp, 'name', '').upper() == 'VALARM']
            trigger_values = {alarm['TRIGGER'].to_ical().decode() for alarm in created_alarms}
            assert '-PT30M' in trigger_values

            update_result = await client.call_tool_mcp(
                "update_event",
                {
                    "calendar_name_or_url": calendar.url,
                    "uid": "event-123",
                    "summary": "Updated Meeting",
                    "location": "Updated Room",
                    "reminders": [
                        {"minutes_before_start": 10, "description": "Heads up"},
                        {"minutes_before_end": 5, "description": "Wrap up"},
                    ],
                },
            )
            assert not update_result.isError
            assert _structured(update_result).get("success") is True
            updated_calendar = Calendar.from_ical(fake_event.data)
            updated_events = list(updated_calendar.walk('VEVENT'))
            assert updated_events
            updated_event = updated_events[0]
            assert str(updated_event.get('summary')) == "Updated Meeting"
            assert str(updated_event.get('description')) == "Discuss upcoming work"
            assert str(updated_event.get('location')) == "Updated Room"
            updated_alarms = [comp for comp in getattr(updated_event, 'subcomponents', []) if getattr(comp, 'name', '').upper() == 'VALARM']
            updated_triggers = {alarm['TRIGGER'].to_ical().decode() for alarm in updated_alarms}
            assert '-PT10M' in updated_triggers
            assert '-PT5M' in updated_triggers
            assert any(alarm['TRIGGER'].params.get('RELATED') == 'END' for alarm in updated_alarms)

            clear_reminders = await client.call_tool_mcp(
                "update_event",
                {
                    "calendar_name_or_url": calendar.url,
                    "uid": "event-123",
                    "reminders": [],
                },
            )
            assert not clear_reminders.isError
            assert _structured(clear_reminders).get("success") is True
            cleared_calendar = Calendar.from_ical(fake_event.data)
            cleared_events = list(cleared_calendar.walk('VEVENT'))
            assert cleared_events
            cleared_event = cleared_events[0]
            cleared_alarms = [comp for comp in getattr(cleared_event, 'subcomponents', []) if getattr(comp, 'name', '').upper() == 'VALARM']
            assert not cleared_alarms

            delete_result = await client.call_tool_mcp(
                "delete_event",
                {"calendar_name_or_url": calendar.url, "uid": "event-123"},
            )
            assert not delete_result.isError
            assert _structured(delete_result).get("success") is True

            search_result = await client.call_tool_mcp(
                "search_events",
                {"query": "meeting"},
            )
            assert not search_result.isError
            search_results = _structured(search_result).get("results")
            assert search_results

            fetch_result = await client.call_tool_mcp(
                "fetch_events",
                {"ids": [search_results[0]["id"]]},
            )
            assert not fetch_result.isError
            assert _structured(fetch_result).get("events")

    asyncio.run(_exercise())
