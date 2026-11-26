"""Offline tests: the gateway is replaced by a session that answers from a table."""

# pytest idioms: fixtures as arguments, tests reach into internals and
# import optional packages late, tables of gateway answers stay on one line.
# pylint: disable=missing-function-docstring,redefined-outer-name,protected-access
# pylint: disable=import-outside-toplevel,unused-argument,line-too-long

import json
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import ClientResponseError

from milesight_gateway_api import (
    FirmwareRelease,
    MilesightGatewayClient,
    UnexpectedResponseError,
    WebConfigError,
    WebLoginRefusedError,
    WebSessionError,
    firmware_key,
    get_latest_firmware,
    parse_firmware_page,
)
from milesight_gateway_api.firmware import FIRMWARE_PAGE_URL

from conftest import FakeResponse  # pylint: disable=import-error,wrong-import-order

GATEWAY = "http://192.0.2.10"
API = f"{GATEWAY}:8080"
CGI = ("POST", f"{GATEWAY}/cgi")
PAGE = (Path(__file__).parent / "fixtures" / "firmware_page.html").read_text("utf-8")

SYSTEM = {"model": "ND65-868M-EA", "sn": "SN0000000001", "uptime": 3600}
SUMMARY = {"status": 0, "result": [{"get": [{"type": "system", "value": SYSTEM}]}]}
NO_SESSION = {"status": -2, "result": [-32001, "Session not found"]}


def _client():
    client = MilesightGatewayClient(
        "admin", "secret", b"1111111111111111", b"2222222222222222", f"{GATEWAY}/", 8080
    )
    client.headers = {"Authorization": "Bearer a.jwt.token"}
    return client


def _web(*answers, login=None):
    """Answer /cgi like the gateway: a login sets a cookie, queries pop answers."""
    queue = list(answers)

    def answer(kwargs):
        if json.loads(kwargs["data"])["core"] == "user":
            return FakeResponse(
                login if login is not None else {"status": 0},
                cookies={"session": "abc"},
            )
        return FakeResponse(queue.pop(0) if queue else SUMMARY)

    return answer


def _cores(session):
    """Return the core of each /cgi request, in order."""
    return [json.loads(kwargs["data"])["core"] for _, _, kwargs in session.calls]


# --- web interface ---------------------------------------------------------------


async def test_system_info_is_read_behind_the_web_login(session):
    """Log in, then read the summary with the session cookie."""
    session.routes[CGI] = _web()

    assert await _client().get_system_info(session) == SYSTEM

    (_, _, login), (_, _, summary) = session.calls
    credentials = json.loads(login["data"])["values"][0]
    assert credentials["username"] == "admin"
    assert credentials["base"] == "web_login"
    # The password goes out AES-encrypted, never in clear text.
    assert credentials["password"] not in ("", "secret")
    assert summary["headers"]["Cookie"] == "session=abc"
    assert "Authorization" not in summary["headers"]


async def test_the_web_session_token_goes_out_as_bearer(session):
    """From 60.0.0.51 the login's username and td are the session, as a bearer."""
    session.routes[CGI] = _web(
        login={"status": 0, "result": [{"username": "admin", "td": "f00d"}]}
    )

    assert await _client().get_system_info(session) == SYSTEM

    summary = session.calls[1][2]
    assert summary["headers"]["Authorization"] == "Bearer login=admin;f00d"
    assert summary["headers"]["Cookie"] == "session=abc"


async def test_the_web_session_is_kept(session):
    """A second query reuses the session instead of logging in again."""
    session.routes[CGI] = _web()
    client = _client()

    await client.get_system_info(session)
    await client.get_system_info(session)

    assert _cores(session) == ["user", "yruo_status", "yruo_status"]


async def test_a_lost_web_session_is_renewed_once(session):
    """When the gateway forgets the session, one new login follows."""
    session.routes[CGI] = _web(NO_SESSION)

    await _client().get_system_info(session)

    assert _cores(session) == ["user", "yruo_status", "user", "yruo_status"]


async def test_a_session_that_keeps_dropping_raises(session):
    """Two lost sessions in a row stop the query."""
    session.routes[CGI] = _web(NO_SESSION, NO_SESSION)

    with pytest.raises(WebSessionError):
        await _client().get_system_info(session)


@pytest.mark.parametrize("login", [{"status": -2}, {"status": -1}, ["not", "a", "dict"]])
async def test_a_refused_web_login_is_not_retried(session, login):
    """After one refusal the client does not try again, so the account stays open."""
    session.routes[CGI] = _web(login=login)
    client = _client()

    with pytest.raises(WebLoginRefusedError):
        await client.get_system_info(session)
    with pytest.raises(WebLoginRefusedError):
        await client.get_system_info(session)

    assert client.web_login_refused
    assert _cores(session) == ["user"]


async def test_a_non_object_web_answer_raises(session):
    """The gateway always answers /cgi with an object; anything else is broken."""
    session.routes[CGI] = _web(["?"])

    with pytest.raises(UnexpectedResponseError):
        await _client().get_system_info(session)


@pytest.mark.parametrize(
    "summary",
    [
        {"status": 0, "result": []},
        {"status": 0, "result": [{"get": [{"type": "network", "value": {}}]}]},
        {"status": 0, "result": [{"get": [{"type": "system", "value": "?"}]}]},
        {"status": 0, "result": [{"get": "?"}]},
    ],
)
async def test_a_summary_without_a_system_block_is_empty(session, summary):
    """A summary in an unexpected shape yields an empty dict, not an error."""
    session.routes[CGI] = _web(summary)

    assert await _client().get_system_info(session) == {}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"model": "UG65", "rtver": "60.0.0.48-r3"}, {"model": "UG65", "rtver": "60.0.0.48-r3"}),
        (["not", "a", "dict"], {}),
    ],
)
async def test_the_login_status_needs_no_session(session, payload, expected):
    """The login check answers without a login and carries model and firmware."""
    session.routes[("POST", f"{GATEWAY}/islogin")] = FakeResponse(payload)

    assert await _client().get_login_status(session) == expected


# --- network server ----------------------------------------------------------


async def test_one_gateway_of_the_fleet_is_read_by_its_id(session):
    session.routes[("GET", f"{API}/api/gateways/24E124FFFE000001")] = FakeResponse(
        {"mac": "24E124FFFE000001", "networkIP": "192.0.2.20"}
    )

    detail = await _client().get_gateway(session, "24E124FFFE000001")

    assert detail["networkIP"] == "192.0.2.20"


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get_gateways", f"{API}/api/gateways?limit=100&offset=0&organizationID=0"),
        ("get_applications", f"{API}/api/urapplications?limit=100&offset=0&organizationID=0"),
        ("get_payload_codec_settings", f"{API}/api/payloadcodecs-setting"),
        ("get_packets", f"{API}/api/urpackets?limit=300&offset=0"),
    ],
)
async def test_status_calls_send_the_jwt(session, method, url):
    """Each status call is one GET with the token, answered with its object."""
    session.routes[("GET", url)] = FakeResponse({"totalCount": 1})

    assert await getattr(_client(), method)(session) == {"totalCount": 1}

    ((_, _, kwargs),) = session.calls
    assert kwargs["headers"] == {"Authorization": "Bearer a.jwt.token"}


@pytest.mark.parametrize(("asked", "sent"), [(5000, 1000), (0, 1)])
async def test_the_packet_window_stays_inside_the_log(session, asked, sent):
    """The log holds 1000 packets, so the window is clamped to it."""
    url = f"{API}/api/urpackets?limit={sent}&offset=0"
    session.routes[("GET", url)] = FakeResponse({"packets": []})

    assert await _client().get_packets(session, limit=asked) == {"packets": []}


async def test_a_non_object_status_answer_raises(session):
    """A list where an object belongs is not passed on as data."""
    session.routes[("GET", f"{API}/api/payloadcodecs-setting")] = FakeResponse(["?"])

    with pytest.raises(UnexpectedResponseError):
        await _client().get_payload_codec_settings(session)


async def test_an_http_error_is_raised(session):
    """A 401 reaches the caller, who decides whether to log in again."""
    session.routes[("GET", f"{API}/api/payloadcodecs-setting")] = FakeResponse(status=401)

    with pytest.raises(ClientResponseError) as err:
        await _client().get_payload_codec_settings(session)
    assert err.value.status == 401


async def test_a_trailing_slash_in_the_address_is_harmless(session):
    """The address may be entered with a slash; the API port still lands right."""
    session.routes[("GET", f"{API}/api/urdevices?search=x")] = FakeResponse(
        {"deviceResult": []}
    )

    assert await _client().get_device(session, "x") == []


async def test_codecs_of_all_types_are_counted(session):
    """Without a type, the short codec list covers default and custom codecs."""
    session.routes[("GET", f"{API}/api/payloadcodecs-short")] = FakeResponse(
        {"result": [], "totalCount": 162}
    )

    assert await _client().get_payload_codecs_short(session) == ([], 162)


async def test_profiles_of_all_applications_are_listed(session):
    """Without an application, the profile query leaves the filter out."""
    url = f"{API}/api/urprofiles?organizationID=0&offset=0&limit=10"
    session.routes[("GET", url)] = FakeResponse(
        {"result": [{"profileID": "p1"}], "totalCount": 1}
    )

    assert await _client().get_profiles(session, "0") == ([{"profileID": "p1"}], 1)


# --- downlinks ----------------------------------------------------------------

QUEUE = ("POST", f"{API}/api/devices/24E1247140000001/queue")


async def test_the_downlink_body_is_flat(session):
    """The queue wants a flat body; the command travels as a JSON string."""
    session.routes[QUEUE] = FakeResponse({})

    await _client().enqueue_downlink(
        session, "24E1247140000001", {"target_temperature": 21, "temperature_tolerance": 1.0}
    )

    ((_, _, kwargs),) = session.calls
    body = kwargs["json"]
    assert {key: body[key] for key in ("devEUI", "fPort", "confirmed")} == {
        "devEUI": "24E1247140000001",
        "fPort": 85,
        "confirmed": False,
    }
    assert json.loads(body["jsonObject"]) == {
        "target_temperature": 21,
        "temperature_tolerance": 1.0,
    }
    assert kwargs["headers"] == {"Authorization": "Bearer a.jwt.token"}


async def test_a_rejected_downlink_raises(session):
    """A refused queue request reaches the caller."""
    session.routes[QUEUE] = FakeResponse(status=403)

    with pytest.raises(ClientResponseError):
        await _client().enqueue_downlink(session, "24E1247140000001", {"reboot": 1})


# --- paginated lists ----------------------------------------------------------

DEVICES = f"{API}/api/urdevices"


def _page(offset):
    return ("GET", f"{DEVICES}?offset={offset}&limit=2&applicationID=0")


async def test_devices_are_read_page_by_page(session):
    """Pages are fetched until one comes back short."""
    session.routes[_page(0)] = FakeResponse(
        {"deviceResult": [{"devEUI": "a"}, {"devEUI": "b"}], "devTotalCount": 3}
    )
    session.routes[_page(2)] = FakeResponse(
        {"deviceResult": [{"devEUI": "c"}], "devTotalCount": 3}
    )

    devices, total = await _client().get_all_devices(session, limit=2)

    assert [d["devEUI"] for d in devices] == ["a", "b", "c"]
    assert total == 3


async def test_a_gateway_that_caps_the_page_is_read_to_the_end(session):
    """Pages shorter than asked for are not the end while the total is open."""
    capped = ("GET", f"{DEVICES}?offset={{}}&limit=100&applicationID=0")
    session.routes[(capped[0], capped[1].format(0))] = FakeResponse(
        {"deviceResult": [{"devEUI": "a"}, {"devEUI": "b"}], "devTotalCount": "3"}
    )
    session.routes[(capped[0], capped[1].format(2))] = FakeResponse(
        {"deviceResult": [{"devEUI": "c"}], "devTotalCount": "3"}
    )

    devices, _ = await _client().get_all_devices(session, limit=100)

    assert [d["devEUI"] for d in devices] == ["a", "b", "c"]


async def test_without_a_total_a_short_page_ends_the_list(session):
    """A gateway that sends no total still ends on a short page."""
    session.routes[_page(0)] = FakeResponse({"deviceResult": [{"devEUI": "a"}]})

    devices, _ = await _client().get_all_devices(session, limit=2)

    assert [d["devEUI"] for d in devices] == ["a"]


async def test_a_device_on_two_pages_is_kept_once(session):
    """A reordered list repeats a device on the next page; it counts once."""
    session.routes[_page(0)] = FakeResponse(
        {"deviceResult": [{"devEUI": "a"}, {"devEUI": "b"}], "devTotalCount": 3}
    )
    session.routes[_page(2)] = FakeResponse(
        {"deviceResult": [{"devEUI": "B"}], "devTotalCount": 3}
    )
    session.routes[_page(3)] = FakeResponse({"deviceResult": [], "devTotalCount": 3})

    devices, _ = await _client().get_all_devices(session, limit=2)

    assert [d["devEUI"] for d in devices] == ["a", "b"]


async def test_a_failing_later_page_is_not_a_short_list(session):
    """An error on page two is raised, not returned as a complete page one."""
    session.routes[_page(0)] = FakeResponse(
        {"deviceResult": [{"devEUI": "a"}, {"devEUI": "b"}], "devTotalCount": 3}
    )
    session.routes[_page(2)] = FakeResponse(status=500)

    with pytest.raises(ClientResponseError):
        await _client().get_all_devices(session, limit=2)


def test_importing_the_library_leaves_logging_alone():
    """A library must not configure the logging of the program that imports it."""
    code = (
        "import logging, milesight_gateway_api;"
        "print(len(logging.getLogger().handlers))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "0"


# --- firmware -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("60.0.0.48", "60.0.0.48-r3"),
        ("60.0.0.48-r3", "60.0.0.50"),
        ("60.0.0.9", "60.0.0.10"),
        ("60.0.0.48-r2", "60.0.0.48-r3"),
    ],
)
def test_firmware_versions_sort_with_revisions(older, newer):
    """A revision is newer than its base and older than the next version."""
    assert firmware_key(older) < firmware_key(newer)


def test_the_newest_release_of_the_family_is_picked():
    """The family decides, so an SDK under the same title is not a firmware."""
    release = parse_firmware_page(PAGE, "60")

    assert release == FirmwareRelease(
        version="60.0.0.50",
        title="UG65&UG67 LoRaWAN® Gateway",
        release_url="https://www.milesight.com/iot/resources/download/release-note/?type=ug6x",
        download_url="https://resource.milesight.com/milesight/iot/firmware/60.0.0.50.bin",
        notes=(
            "1. Do not upgrade this firmware to Helium versions.\n"
            "2. Update to 60.0.0.45 or later first."
        ),
    )


def test_an_entry_without_notes_or_links_still_counts():
    """Missing release notes, download link or warnings are not an error."""
    release = parse_firmware_page(PAGE, "63")

    assert release is not None
    assert release.version == "63.0.0.2-r2"
    assert release.notes is None


def test_cameras_and_sensors_of_the_same_family_are_skipped():
    """A camera and a sensor also use 63, but only the UG63 is a gateway."""
    release = parse_firmware_page(PAGE, "63")

    assert release is not None
    assert release.title.startswith("UG63")


@pytest.mark.parametrize("page", [PAGE, "", "<html>redesigned</html>"])
def test_an_unknown_family_has_no_release(page):
    """A family the page does not list, or a page without entries, gives None."""
    assert parse_firmware_page(page, "99") is None


async def test_latest_firmware_is_read_from_the_download_page(session):
    """The page is fetched and parsed for the family."""
    session.routes[("GET", FIRMWARE_PAGE_URL)] = FakeResponse(body=PAGE)

    release = await get_latest_firmware(session, "60")

    assert release is not None
    assert release.version == "60.0.0.50"


async def test_an_unreachable_download_page_raises(session):
    """A server error on the page reaches the caller."""
    session.routes[("GET", FIRMWARE_PAGE_URL)] = FakeResponse(status=503)

    with pytest.raises(ClientResponseError):
        await get_latest_firmware(session, "60")


# --- administration through the web interface --------------------------------------

WIFI = {"enabled": 0, "mode": 0, "ssid": "Gateway_000001", "encryption": 4, "key": "old-key-1"}
WIFI_ANSWER = {"status": 0, "result": [{"get": [{"type": "yruo_wifi", "index": 0, "value": WIFI}]}]}
PEER = {"id": "abc123", "name": "server", "public_key": "PUB=", "allowed_ips": ["10.0.0.0/24"]}
WIREGUARD_ANSWER = {
    "status": 0,
    "result": [
        {
            "get": [
                {"type": "yruo_wireguard", "index": 1, "value": {"name": "wg0", "enable": 1, "peers": [PEER]}},
                {"type": "yruo_wireguard", "index": 2, "value": {"name": "wg1", "enable": 0, "peers": []}},
            ]
        }
    ],
}
OK = {"status": 0, "result": [{}]}


def _requests(session):
    """Return each /cgi request after the login as (core, function, values)."""
    bodies = [json.loads(kwargs["data"]) for _, _, kwargs in session.calls]
    return [(b["core"], b["function"], b["values"]) for b in bodies if b["core"] != "user"]


async def test_wifi_settings_are_read(session):
    session.routes[CGI] = _web(WIFI_ANSWER)

    assert await _client().get_wifi(session) == WIFI


async def test_an_access_point_is_set_and_applied(session):
    """SSID and key are stored, then applied like the web page's two buttons."""
    session.routes[CGI] = _web(OK, OK)

    await _client().set_wifi_access_point(session, "Workshop", "a-long-key")

    stored, applied = _requests(session)
    assert stored == (
        "yruo_wifi",
        "set",
        [
            {
                "base": "yruo_wifi",
                "index": 0,
                "value": {"enabled": 1, "mode": 0, "ssid": "Workshop", "encryption": 4, "key": "a-long-key"},
            }
        ],
    )
    assert applied == ("yruo_apply", "apply", [])


async def test_wifi_is_switched_off_without_applying(session):
    session.routes[CGI] = _web(OK)

    await _client().set_wifi_enabled(session, False, apply=False)

    assert _requests(session) == [
        ("yruo_wifi", "set", [{"base": "yruo_wifi", "index": 0, "value": {"enabled": 0}}])
    ]


@pytest.mark.parametrize(
    ("ssid", "key", "encryption"),
    [("", "a-long-key", "wpa2_psk"), ("x" * 33, "a-long-key", "wpa2_psk"), ("Workshop", "short", "wpa2_psk"), ("Workshop", "", "wep")],
)
async def test_an_unusable_access_point_is_refused_before_it_goes_out(session, ssid, key, encryption):
    with pytest.raises(WebConfigError):
        await _client().set_wifi_access_point(session, ssid, key, encryption=encryption)
    assert session.calls == []


async def test_a_refused_setting_raises(session):
    session.routes[CGI] = _web({"status": -1, "result": [-1, "invalid"]})

    with pytest.raises(WebConfigError, match="refused"):
        await _client().set_wifi_enabled(session, True)
    # Nothing is applied after a refusal.
    assert [core for core, _, _ in _requests(session)] == ["yruo_wifi"]


async def test_wireguard_interfaces_are_read_with_their_index(session):
    session.routes[CGI] = _web(WIREGUARD_ANSWER)

    interfaces = await _client().get_wireguard(session)

    assert [(i["index"], i["name"]) for i in interfaces] == [(1, "wg0"), (2, "wg1")]
    assert interfaces[0]["peers"] == [PEER]


async def test_a_wireguard_interface_gets_only_the_given_fields(session):
    session.routes[CGI] = _web(OK, OK)

    await _client().set_wireguard_interface(session, 1, enable=True, address="10.0.0.2/24", private_key="PRIV=")

    (_, _, values), _apply = _requests(session)
    assert values == [
        {
            "base": "yruo_wireguard",
            "index": 1,
            "value": {"enable": 1, "custom_private_key": 1, "private_key": "PRIV=", "address": "10.0.0.2/24"},
        }
    ]


async def test_a_new_peer_is_added_and_a_known_one_replaced(session):
    """The gateway tells them apart by an action; a known peer keeps its ID."""
    session.routes[CGI] = _web(WIREGUARD_ANSWER, OK, WIREGUARD_ANSWER, OK)
    client = _client()

    await client.set_wireguard_peer(session, 1, "office", "KEY=", ["10.1.0.0/24"], endpoint="vpn.example.com", port=51820, apply=False)
    await client.set_wireguard_peer(session, 1, "server", "NEW=", ["10.0.0.0/24"], apply=False)

    _get, (_, _, added), _get2, (_, _, edited) = _requests(session)
    new = added[0]["value"]["peers"][0]
    assert (new["action"], new["name"], new["endpoint"], new["port"]) == (1, "office", "vpn.example.com", 51820)
    assert new["id"]
    old = edited[0]["value"]["peers"][0]
    assert (old["action"], old["id"], old["public_key"]) == (2, "abc123", "NEW=")


async def test_a_peer_is_removed_by_name(session):
    session.routes[CGI] = _web(OK)

    await _client().remove_wireguard_peer(session, 2, "server", apply=False)

    assert _requests(session) == [
        ("yruo_wireguard", "set", [{"base": "yruo_wireguard", "index": 2, "value": {"peers": [{"name": "server", "action": 3}]}}])
    ]


async def test_the_gateway_has_three_wireguard_interfaces(session):
    with pytest.raises(WebConfigError):
        await _client().set_wireguard_interface(session, 4, enable=True)


# --- firmware upgrade ------------------------------------------------------------

IMPORT = ("POST", f"{GATEWAY}/cgi-bin/file-import")


def _fields(form):
    """Return a FormData's fields as name -> value."""
    return {options["name"]: value for options, _headers, value in form._fields}  # pylint: disable=protected-access


async def test_a_firmware_image_is_uploaded_like_the_upgrade_button(session):
    """Behind the web login, as the form the page posts: size, kind and the file."""
    session.routes[CGI] = _web(login={"status": 0, "result": [{"username": "admin", "td": "tok"}]})
    session.routes[IMPORT] = FakeResponse({"size": 4, "checksum": "abcd", "filename": "firmware.bin"})

    result = await _client().upgrade_firmware(session, b"\x7fELF")

    assert result["checksum"] == "abcd"
    _login, (_, _, upload) = session.calls
    assert upload["headers"]["Cookie"] == "session=abc"
    fields = _fields(upload["data"])
    assert fields["size"] == "4"
    assert fields["filename"] == "type=upgrade&file=firmware.bin"
    # Older firmware reads the session from the form, not from the cookie.
    assert fields["sessionid"] == "tok"
    assert fields["file"] == b"\x7fELF"


async def test_a_reset_with_the_upgrade_has_to_be_asked_for(session):
    session.routes[CGI] = _web()
    session.routes[IMPORT] = FakeResponse({"size": 1, "checksum": "x"})

    await _client().upgrade_firmware(session, b"x", reset=True)

    assert _fields(session.calls[-1][2]["data"])["filename"] == "type=upgrade_with_reset&file=firmware.bin"


async def test_a_refused_firmware_image_raises(session):
    session.routes[CGI] = _web()
    session.routes[IMPORT] = FakeResponse({"message": 2})

    with pytest.raises(WebConfigError, match="refused the firmware"):
        await _client().upgrade_firmware(session, b"not firmware")


async def test_an_empty_firmware_image_never_goes_out(session):
    with pytest.raises(WebConfigError):
        await _client().upgrade_firmware(session, b"")
    assert session.calls == []


# --- backup ----------------------------------------------------------------------

EXPORT = ("POST", f"{GATEWAY}/cgi-bin/file-export")
ARCHIVE = bytes([0x1F, 0x8B]) + bytes(2000)


async def test_the_full_backup_is_downloaded_like_the_button_saves_it(session):
    """The form the page posts: the session token, and which file."""
    session.routes[CGI] = _web(login={"status": 0, "result": [{"username": "admin", "td": "tok"}]})
    session.routes[EXPORT] = FakeResponse(body=ARCHIVE)

    assert await _client().download_backup(session) == ARCHIVE

    _login, (_, _, export) = session.calls
    assert export["data"] == {"sessionid": "tok", "type": "backup", "file": "cfgbackup"}
    assert export["headers"]["Authorization"] == "Bearer login=admin;tok"


@pytest.mark.parametrize("body", [b"", b'{"status": -2}', b"<html>login</html>" + bytes(2000)])
async def test_an_answer_that_is_no_backup_raises(session, body):
    session.routes[CGI] = _web()
    session.routes[EXPORT] = FakeResponse(body=body)

    with pytest.raises(WebConfigError, match="no backup"):
        await _client().download_backup(session)


# --- firmware steps --------------------------------------------------------------


def _release(version, requires=None):
    return FirmwareRelease(version, "UG65", None, f"https://example.com/{version}.bin", requires=requires)


def test_the_page_lists_the_release_an_older_gateway_installs_first():
    """Milesight keeps 60.0.0.45 next to the newest for exactly that."""
    from milesight_gateway_api import parse_firmware_releases

    versions = [r.version for r in parse_firmware_releases(PAGE, "60")]

    assert versions == sorted(versions, key=firmware_key)
    assert "60.0.0.45" in versions
    assert len(versions) > 1


@pytest.mark.parametrize(
    ("installed", "expected"),
    [
        ("60.0.0.44", "60.0.0.45"),  # the newest needs 60.0.0.45 first
        ("60.0.0.45", "60.0.0.51"),
        ("60.0.0.48-r3", "60.0.0.51"),
        ("60.0.0.51", None),
    ],
)
def test_the_next_step_respects_the_minimum_version(installed, expected):
    from milesight_gateway_api import next_firmware

    releases = [_release("60.0.0.45"), _release("60.0.0.51", requires="60.0.0.45")]

    step = next_firmware(installed, releases)

    assert (step.version if step else None) == expected


def test_a_needed_step_the_page_does_not_offer_blocks_the_upgrade():
    from milesight_gateway_api import next_firmware

    assert next_firmware("60.0.0.44", [_release("60.0.0.51", requires="60.0.0.45")]) is None


def test_the_minimum_version_is_read_from_the_notes():
    from milesight_gateway_api.firmware import _requires

    notes = "2. Before upgrading to this version, ensure that your gateway is updated to v60.0.0.45 or later; otherwise it fails."
    assert _requires(notes) == "60.0.0.45"
    assert _requires("ensure that your gateway is updated to version 56.0.0.5 or later") == "56.0.0.5"
    assert _requires("Please reset the gateway if you would like to downgrade.") is None
