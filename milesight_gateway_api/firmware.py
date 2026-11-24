"""Read the newest gateway firmware from Milesight's download page."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Optional

from aiohttp import ClientSession, ClientTimeout

# Milesight publishes gateway firmware only on this page, as plain HTML.
FIRMWARE_PAGE_URL = "https://www.milesight.com/support/resources/firmware"
_REQUEST_TIMEOUT = ClientTimeout(total=30)


@dataclass(frozen=True)
class FirmwareRelease:
    """A gateway firmware Milesight offers for download."""

    version: str
    title: str
    release_url: Optional[str]
    download_url: Optional[str]
    notes: Optional[str] = None


def firmware_key(version: str) -> tuple:
    """
    Return a sort key for a Milesight firmware version.

    Revisions sort after their base, so 60.0.0.48 < 60.0.0.48-r3 < 60.0.0.50.
    """
    return tuple(int(part) for part in re.findall(r"\d+", version))


def _plain_text(markup: str) -> str:
    """Turn an attribute holding escaped HTML into plain text."""
    text = re.sub(r"<br\s*/?>", "\n", html.unescape(markup))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def parse_firmware_page(page: str, family: str) -> Optional[FirmwareRelease]:
    """
    Return the newest release of a firmware family from the download page.

    The family is the first part of the version (60 for UG65 and UG67), which
    is more stable than the product titles the page lists it under.
    """
    releases = []
    for block in page.split("<li ")[1:]:
        title = re.search(r'class="list__title"[^>]*title="([^"]*)"', block)
        version = re.search(
            r'class="version">\s*<p>\s*v?(\d+(?:\.\d+)+(?:-r\d+)?)\s*</p>', block
        )
        if not title or not version or version[1].split(".")[0] != family:
            continue
        release = re.search(r'class="release-note">\s*<a href="([^"]+)"', block)
        download = re.search(r'href="([^"]+\.bin)"', block)
        # The download link carries upgrade warnings, such as a minimum version.
        notes = re.search(r'\.bin"[^>]*data-info="([^"]*)"', block)
        releases.append(
            FirmwareRelease(
                version=version[1],
                title=html.unescape(title[1]),
                release_url=html.unescape(release[1]) if release else None,
                download_url=html.unescape(download[1]) if download else None,
                notes=_plain_text(notes[1]) if notes else None,
            )
        )
    return max(releases, key=lambda r: firmware_key(r.version), default=None)


async def get_latest_firmware(
    session: ClientSession, family: str
) -> Optional[FirmwareRelease]:
    """Fetch the download page and return the newest release of ``family``."""
    async with session.get(FIRMWARE_PAGE_URL, timeout=_REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        page = await response.text()
    return parse_firmware_page(page, family)
