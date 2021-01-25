# -*- coding: utf-8 -*-
import time
from datetime import datetime
from functools import partial
from os import environ

import plexapi
import pytest
import requests
from plexapi.client import PlexClient
from plexapi.exceptions import NotFound
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from plexapi.utils import createMyPlexDevice

from .payloads import ACCOUNT_XML

try:
    from unittest.mock import patch, MagicMock, mock_open
except ImportError:
    from mock import patch, MagicMock, mock_open


SERVER_BASEURL = plexapi.CONFIG.get("auth.server_baseurl")
MYPLEX_USERNAME = plexapi.CONFIG.get("auth.myplex_username")
MYPLEX_PASSWORD = plexapi.CONFIG.get("auth.myplex_password")
SERVER_TOKEN = plexapi.CONFIG.get("auth.server_token")
CLIENT_BASEURL = plexapi.CONFIG.get("auth.client_baseurl")
CLIENT_TOKEN = plexapi.CONFIG.get("auth.client_token")

MIN_DATETIME = datetime(1999, 1, 1)
REGEX_EMAIL = r"(^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$)"
REGEX_IPADDR = r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$"

AUDIOCHANNELS = {2, 6}
AUDIOLAYOUTS = {"5.1", "5.1(side)", "stereo"}
CODECS = {"aac", "ac3", "dca", "h264", "mp3", "mpeg4"}
CONTAINERS = {"avi", "mp4", "mkv"}
CONTENTRATINGS = {"TV-14", "TV-MA", "G", "NR", "Not Rated"}
FRAMERATES = {"24p", "PAL", "NTSC"}
PROFILES = {"advanced simple", "main", "constrained baseline"}
RESOLUTIONS = {"sd", "480", "576", "720", "1080"}
HW_DECODERS = {'dxva2', 'videotoolbox', 'mediacodecndk', 'vaapi', 'nvdec'}
HW_ENCODERS = {'qsv', 'mf', 'videotoolbox', 'mediacodecndk', 'vaapi', 'nvenc', 'x264'}
ENTITLEMENTS = {
    "ios",
    "roku",
    "android",
    "xbox_one",
    "xbox_360",
    "windows",
    "windows_phone",
}
SYNC_DEVICE_IDENTIFIER = "test-sync-client-%s" % plexapi.X_PLEX_IDENTIFIER
SYNC_DEVICE_HEADERS = {
    "X-Plex-Provides": "sync-target",
    "X-Plex-Platform": "iOS",
    "X-Plex-Platform-Version": "11.4.1",
    "X-Plex-Device": "iPhone",
    "X-Plex-Device-Name": "Test Sync Device",
    "X-Plex-Client-Identifier": SYNC_DEVICE_IDENTIFIER
}

TEST_AUTHENTICATED = "authenticated"
TEST_ANONYMOUSLY = "anonymously"
ANON_PARAM = pytest.param(TEST_ANONYMOUSLY, marks=pytest.mark.anonymous)
AUTH_PARAM = pytest.param(TEST_AUTHENTICATED, marks=pytest.mark.authenticated)


def pytest_addoption(parser):
    parser.addoption(
        "--client", action="store_true", default=False, help="Run client tests."
    )


def pytest_generate_tests(metafunc):
    if "plex" in metafunc.fixturenames:
        if (
            "account" in metafunc.fixturenames
            or TEST_AUTHENTICATED in metafunc.definition.keywords
        ):
            metafunc.parametrize("plex", [AUTH_PARAM], indirect=True)
        else:
            metafunc.parametrize("plex", [ANON_PARAM, AUTH_PARAM], indirect=True)
    elif "account" in metafunc.fixturenames:
        metafunc.parametrize("account", [AUTH_PARAM], indirect=True)


def pytest_runtest_setup(item):
    if "client" in item.keywords and not item.config.getvalue("client"):
        return pytest.skip("Need --client option to run.")
    if TEST_AUTHENTICATED in item.keywords and not (
        MYPLEX_USERNAME and MYPLEX_PASSWORD or SERVER_TOKEN
    ):
        return pytest.skip(
            "You have to specify MYPLEX_USERNAME and MYPLEX_PASSWORD or SERVER_TOKEN to run authenticated tests"
        )
    if TEST_ANONYMOUSLY in item.keywords and (MYPLEX_USERNAME and MYPLEX_PASSWORD or SERVER_TOKEN):
        return pytest.skip(
            "Anonymous tests should be ran on unclaimed server, without providing MYPLEX_USERNAME and "
            "MYPLEX_PASSWORD or SERVER_TOKEN"
        )


# ---------------------------------
#  Fixtures
# ---------------------------------


def get_account():
    return MyPlexAccount()


@pytest.fixture(scope="session")
def account():
    if SERVER_TOKEN:
        return get_account()
    assert MYPLEX_USERNAME, "Required MYPLEX_USERNAME not specified."
    assert MYPLEX_PASSWORD, "Required MYPLEX_PASSWORD not specified."
    return get_account()


@pytest.fixture(scope="session")
def account_once(account):
    if environ.get("TEST_ACCOUNT_ONCE") not in ("1", "true") and environ.get("CI") == "true":
        pytest.skip("Do not forget to test this by providing TEST_ACCOUNT_ONCE=1")
    return account


@pytest.fixture(scope="session")
def account_plexpass(account):
    if not account.subscriptionActive:
        pytest.skip(
            "PlexPass subscription is not active, unable to test sync-stuff, be careful!"
        )
    return account


@pytest.fixture(scope="session")
def account_synctarget(account_plexpass):
    assert "sync-target" in SYNC_DEVICE_HEADERS["X-Plex-Provides"]
    assert "iOS" == SYNC_DEVICE_HEADERS["X-Plex-Platform"]
    assert "11.4.1" == SYNC_DEVICE_HEADERS["X-Plex-Platform-Version"]
    assert "iPhone" == SYNC_DEVICE_HEADERS["X-Plex-Device"]
    return account_plexpass


@pytest.fixture()
def mocked_account(requests_mock):
    requests_mock.get("https://plex.tv/users/account", text=ACCOUNT_XML)
    return MyPlexAccount(token="faketoken")


@pytest.fixture(scope="session")
def plex(request):
    assert SERVER_BASEURL, "Required SERVER_BASEURL not specified."
    session = requests.Session()
    if request.param == TEST_AUTHENTICATED:
        token = get_account().authenticationToken
    else:
        token = None
    return PlexServer(SERVER_BASEURL, token, session=session)


@pytest.fixture(scope="session")
def sync_device(account_synctarget):
    try:
        device = account_synctarget.device(clientId=SYNC_DEVICE_IDENTIFIER)
    except NotFound:
        device = createMyPlexDevice(SYNC_DEVICE_HEADERS, account_synctarget)
    
    assert device
    assert "sync-target" in device.provides
    return device


@pytest.fixture()
def clear_sync_device(sync_device, plex):
    sync_items = sync_device.syncItems()
    for item in sync_items.items:
        item.delete()
    plex.refreshSync()
    return sync_device


@pytest.fixture
def fresh_plex():
    return PlexServer


@pytest.fixture()
def plex2(plex):
    return plex()


@pytest.fixture()
def client(request, plex):
    return PlexClient(plex, baseurl=CLIENT_BASEURL, token=CLIENT_TOKEN)


@pytest.fixture()
def tvshows(plex):
    return plex.library.section("TV Shows")


@pytest.fixture()
def movies(plex):
    return plex.library.section("Movies")


@pytest.fixture()
def music(plex):
    return plex.library.section("Music")


@pytest.fixture()
def photos(plex):
    return plex.library.section("Photos")


@pytest.fixture()
def movie(movies):
    return movies.get("Elephants Dream")


@pytest.fixture()
def collection(plex):
    try:
        return plex.library.section("Movies").collections()[0]
    except IndexError:
        movie = plex.library.section("Movies").get("Elephants Dream")
        movie.addCollection(["marvel"])

        n = plex.library.section("Movies").reload()
        return n.collections()[0]


@pytest.fixture()
def artist(music):
    return music.get("Broke For Free")


@pytest.fixture()
def album(artist):
    return artist.album("Layers")


@pytest.fixture()
def track(album):
    return album.track("As Colourful as Ever")


@pytest.fixture()
def show(tvshows):
    return tvshows.get("Game of Thrones")


@pytest.fixture()
def episode(show):
    return show.get("Winter Is Coming")


@pytest.fixture()
def photoalbum(photos):
    try:
        return photos.get("Cats")
    except Exception:
        return photos.get("photo_album1")


@pytest.fixture()
def subtitle():
    mopen = mock_open()
    with patch("__main__.open", mopen):
        with open("subtitle.srt", "w") as handler:
            handler.write("test")
        return handler


@pytest.fixture()
def shared_username(account):
    username = environ.get("SHARED_USERNAME", "PKKid")
    for user in account.users():
        if user.title.lower() == username.lower():
            return username
        elif (
            user.username
            and user.email
            and user.id
            and username.lower()
            in (user.username.lower(), user.email.lower(), str(user.id))
        ):
            return username
    pytest.skip("Shared user %s wasn`t found in your MyPlex account" % username)


@pytest.fixture()
def monkeydownload(request, monkeypatch):
    monkeypatch.setattr(
        "plexapi.utils.download", partial(plexapi.utils.download, mocked=True)
    )
    yield
    monkeypatch.undo()


def callable_http_patch():
    """This intented to stop some http requests inside some tests."""
    return patch(
        "plexapi.server.requests.sessions.Session.send",
        return_value=MagicMock(status_code=200, text="<xml><child></child></xml>"),
    )


@pytest.fixture()
def empty_response(mocker):
    response = mocker.MagicMock(status_code=200, text="<xml><child></child></xml>")
    return response


@pytest.fixture()
def patched_http_call(mocker):
    """This will stop any http calls inside any test."""
    return mocker.patch(
        "plexapi.server.requests.sessions.Session.send",
        return_value=MagicMock(status_code=200, text="<xml><child></child></xml>"),
    )


# ---------------------------------
#  Utility Functions
# ---------------------------------
def is_datetime(value):
    if value is None:
        return True
    return value > MIN_DATETIME


def is_int(value, gte=1):
    return int(value) >= gte


def is_float(value, gte=1.0):
    return float(value) >= gte


def is_bool(value):
    return value is True or value is False


def is_metadata(key, prefix="/library/metadata/", contains="", suffix=""):
    try:
        assert key.startswith(prefix)
        assert contains in key
        assert key.endswith(suffix)
        return True
    except AssertionError:
        return False


def is_part(key):
    return is_metadata(key, prefix="/library/parts/")


def is_section(key):
    return is_metadata(key, prefix="/library/sections/")


def is_string(value, gte=1):
    return isinstance(value, str) and len(value) >= gte


def is_thumb(key):
    return is_metadata(key, contains="/thumb/")


def wait_until(condition_function, delay=0.25, timeout=1, *args, **kwargs):
    start = time.time()
    ready = condition_function(*args, **kwargs)
    retries = 1
    while not ready and time.time() - start < timeout:
        retries += 1
        time.sleep(delay)
        ready = condition_function(*args, **kwargs)

    assert ready, "Wait timeout after %d retries, %.2f seconds" % (
        retries,
        time.time() - start,
    )

    return ready
