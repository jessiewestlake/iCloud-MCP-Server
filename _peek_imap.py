from server import _open_imap

MAILBOX = "INBOX"
UID = "167188"

imap = _open_imap()
try:
    status, _ = imap.select(MAILBOX, readonly=True)
    print("SELECT", status)
    for spec in ('(RFC822 FLAGS)', '(BODY.PEEK[] FLAGS)', '(BODY.PEEK[])', '(BODY[])'):
        status, data = imap.uid('FETCH', UID, spec)
        print("SPEC", spec, "status", status, "len", len(data))
        for idx, part in enumerate(data):
            print(f"  PART[{idx}] type", type(part))
            if isinstance(part, tuple):
                header, payload = part
                print("   header bytes?", isinstance(header, bytes), "payload type", type(payload))
                if isinstance(header, bytes):
                    print("   header preview", header[:80])
                if isinstance(payload, (bytes, bytearray)):
                    print("   payload len", len(payload))
                else:
                    print("   payload value", payload)
            elif isinstance(part, bytes):
                print("   bytes value", part)
finally:
    try:
        imap.close()
    except Exception:
        pass
    imap.logout()
