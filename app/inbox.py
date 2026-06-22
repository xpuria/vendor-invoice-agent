"""Inbox sources behind one Protocol. `FolderInbox` is the demo stub (PDFs in a folder);
`GmailInbox` is a real source over IMAP. Both return the same Email objects via `.fetch()`,
so swapping is a one-line change in the runner.
"""

from __future__ import annotations

import email
import imaplib
from dataclasses import dataclass, field
from email.header import decode_header
from pathlib import Path
from typing import Protocol

from app.config import ROOT, settings


def is_pdf(path: str | Path) -> bool:
    """Stage 2 of the brief: keep only application/pdf attachments."""
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return True
    try:
        with open(p, "rb") as f:
            return f.read(5) == b"%PDF-"  # magic number, in case the extension lies
    except OSError:
        return False


def keep_pdfs(paths: list[str | Path]) -> list[Path]:
    return [Path(p) for p in paths if is_pdf(p)]


@dataclass
class Email:
    sender: str
    subject: str
    attachments: list[Path] = field(default_factory=list)


class InboxSource(Protocol):
    def fetch(self) -> list[Email]: ...


class FolderInbox:
    """Treats each PDF in a folder as an 'arrived email'. Sender is inferred from the
    filename so the demo reads naturally."""

    def __init__(self, folder: str | Path | None = None):
        self.folder = Path(folder) if folder else ROOT / "data" / "inbox"

    def fetch(self) -> list[Email]:
        pdfs = keep_pdfs(sorted(self.folder.glob("*.pdf")))
        out = []
        for p in pdfs:
            sender = p.stem.split("_", 1)[-1].replace("_", " ")
            out.append(Email(sender=f"{sender} <billing@example.com>",
                             subject=f"Invoice: {p.name}", attachments=[p]))
        return out


class GmailInbox:
    """Real inbox over IMAP (Gmail: imap.gmail.com with an app password). Fetches unread
    messages that carry PDF attachments, saves the PDFs to `data/inbox/`, and marks the
    messages read. Same `.fetch()` contract as FolderInbox.

    Needs GMAIL_USER / GMAIL_APP_PASSWORD in .env. (IMAP keeps this dependency-free; the
    Gmail API via OAuth is the alternative for finer-grained scopes.)
    """

    def __init__(self, save_dir: str | Path | None = None, mark_seen: bool = True):
        g = settings()["gmail"]
        self.user, self.password = g["user"], g["app_password"]
        if not self.user or not self.password:
            raise RuntimeError("GmailInbox needs GMAIL_USER and GMAIL_APP_PASSWORD in .env")
        self.save_dir = Path(save_dir) if save_dir else ROOT / "data" / "inbox"
        self.mark_seen = mark_seen

    @staticmethod
    def _decode(s: str | None) -> str:
        if not s:
            return ""
        return "".join(
            (part.decode(enc or "utf-8", "ignore") if isinstance(part, bytes) else part)
            for part, enc in decode_header(s)
        )

    def fetch(self) -> list[Email]:
        self.save_dir.mkdir(parents=True, exist_ok=True)
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(self.user, self.password)
        try:
            imap.select("INBOX")
            _, data = imap.search(None, "UNSEEN")
            out: list[Email] = []
            for num in data[0].split():
                _, raw = imap.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(raw[0][1])
                attachments = []
                for part in msg.walk():
                    name = part.get_filename()
                    if name and is_pdf(name):
                        dest = self.save_dir / self._decode(name)
                        dest.write_bytes(part.get_payload(decode=True))
                        attachments.append(dest)
                if attachments:
                    out.append(Email(sender=self._decode(msg.get("From")),
                                     subject=self._decode(msg.get("Subject")),
                                     attachments=attachments))
                    if self.mark_seen:
                        imap.store(num, "+FLAGS", "\\Seen")
            return out
        finally:
            imap.logout()
