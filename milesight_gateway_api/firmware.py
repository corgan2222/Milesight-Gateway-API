"""Read gateway firmware from Milesight's download page: releases, steps, images."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Optional

from aiohttp import ClientSession, ClientTimeout

# Milesight publishes gateway firmware only on this page, as plain HTML.
FIRMWARE_PAGE_URL = "https://www.milesight.com/support/resources/firmware"
_REQUEST_TIMEOUT = ClientTimeout(total=30)
# An image has more than a hundred megabytes.
_IMAGE_TIMEOUT = ClientTimeout(total=1800)
_SMALLEST_IMAGE = 1_000_000
# "ensure that your gateway is updated to v60.0.0.45 or later"
_REQUIRES = re.compile(
    r"updated to (?:version )?v?(\d+(?:\.\d+)+(?:-r\d+)?) or later", re.IGNORECASE
)


@dataclass(frozen=True)
class FirmwareRelease:
    """A gateway firmware Milesight offers for download."""

    version: str
    title: str
    release_url: Optional[str]
    download_url: Optional[str]
    notes: Optional[str] = None
    # The oldest firmware this one installs on, where the notes name one.
    requires: Optional[str] = None


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


def parse_firmware_releases(page: str, family: str) -> list[FirmwareRelease]:
    """
    Return the releases of a firmware family on the download page, oldest first.

    The family is the first part of the version (60 for UG65 and UG67), which
    is more stable than the product titles the page lists it under. Only
    gateway entries count: cameras and sensors share family numbers, such as
    63 for the UG63, a camera line and the VS121. Besides the newest release
    the page keeps the one an older gateway has to install first.
    """
    releases = []
    for block in page.split("<li ")[1:]:
        title = re.search(r'class="list__title"[^>]*title="([^"]*)"', block)
        version = re.search(
            r'class="version">\s*<p>\s*v?(\d+(?:\.\d+)+(?:-r\d+)?)\s*</p>', block
        )
        if not title or not version or version[1].split(".")[0] != family:
            continue
        if "Gateway" not in title[1]:
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
                requires=_requires(_plain_text(notes[1])) if notes else None,
            )
        )
    return sorted(releases, key=lambda r: firmware_key(r.version))


def _requires(notes: str) -> Optional[str]:
    """Return the minimum installed version the notes name, if any."""
    found = _REQUIRES.search(notes)
    return found[1] if found else None


def parse_firmware_page(page: str, family: str) -> Optional[FirmwareRelease]:
    """Return the newest release of a firmware family from the download page."""
    releases = parse_firmware_releases(page, family)
    return releases[-1] if releases else None


def next_firmware(
    installed: str, releases: list[FirmwareRelease]
) -> Optional[FirmwareRelease]:
    """
    Return the release to install next on the way to the newest one, or None.

    Usually that is the newest. Where it only installs on a minimum version
    the gateway has not reached, it is the release that gets it there: on
    60.0.0.44 the way to 60.0.0.51 leads over 60.0.0.45. None when nothing is
    newer, or when the page offers no release for a step that is needed.
    """
    have = firmware_key(installed)
    newer = [r for r in releases if firmware_key(r.version) > have]
    if not newer:
        return None
    target = newer[-1]
    while target.requires and have < firmware_key(target.requires):
        steps = [
            r
            for r in newer
            if firmware_key(target.requires) <= firmware_key(r.version) < firmware_key(target.version)
        ]
        if not steps:
            return None
        target = steps[0]
    return target


async def get_firmware_releases(session: ClientSession, family: str) -> list[FirmwareRelease]:
    """Fetch the download page and return the releases of ``family``, oldest first."""
    async with session.get(FIRMWARE_PAGE_URL, timeout=_REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        page = await response.text()
    return parse_firmware_releases(page, family)


async def download_firmware(session: ClientSession, release: FirmwareRelease) -> bytes:
    """
    Download a release's image from Milesight and return its bytes.

    Raises ValueError when the release has no download, or when what comes
    back is no image: shorter than the server announced, or far too small.
    """
    if not release.download_url:
        raise ValueError(f"Firmware {release.version} has no download")
    async with session.get(release.download_url, timeout=_IMAGE_TIMEOUT) as response:
        response.raise_for_status()
        image = await response.read()
        announced = response.headers.get("Content-Length")
    if len(image) < _SMALLEST_IMAGE or (announced and int(announced) != len(image)):
        raise ValueError(f"The download of firmware {release.version} is incomplete")
    return image


async def get_latest_firmware(
    session: ClientSession, family: str
) -> Optional[FirmwareRelease]:
    """Fetch the download page and return the newest release of ``family``."""
    async with session.get(FIRMWARE_PAGE_URL, timeout=_REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        page = await response.text()
    return parse_firmware_page(page, family)
