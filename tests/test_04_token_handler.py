import base64
import hashlib
import hmac
import random
import time

import pytest

from oidcendpoint.endpoint_context import EndpointContext
from oidcendpoint.jwt_token import JWTToken
from oidcendpoint.token_handler import Crypt
from oidcendpoint.token_handler import DefaultToken
from oidcendpoint.token_handler import TokenHandler
from oidcendpoint.token_handler import factory
from oidcendpoint.token_handler import is_expired


def test_is_expired():
    assert is_expired(-1) is False
    assert is_expired(1, 2)
    assert is_expired(1, 1) is False
    assert is_expired(2, 1) is False

    now = time.time()
    assert is_expired(now - 1)
    assert is_expired(now + 1) is False


def test_crypt():
    crypt = Crypt("Ditt nya bankkort")
    txt = "Arsenal's great season gifts"
    enc_text = crypt.encrypt(txt)
    dec_text = crypt.decrypt(enc_text)
    assert dec_text == txt


class TestCrypt(object):
    @pytest.fixture(autouse=True)
    def create_crypt(self):
        self.crypt = Crypt("4-amino-1H-pyrimidine-2-one")

    def test_encrypt_decrypt(self):
        ctext = self.crypt.encrypt("Cytosine")
        plain = self.crypt.decrypt(ctext)
        assert plain == "Cytosine"

        ctext = self.crypt.encrypt("cytidinetriphosp")
        plain = self.crypt.decrypt(ctext)

        assert plain == "cytidinetriphosp"

    def test_crypt_with_b64(self):
        db = {}
        msg = "secret{}{}".format(time.time(), random.random())
        csum = hmac.new(msg.encode("utf-8"), digestmod=hashlib.sha224)
        txt = csum.digest()  # 28 bytes long, 224 bits
        db[txt] = "foobar"
        txt = txt + b"aces"  # another 4 bytes

        ctext = self.crypt.encrypt(txt)
        onthewire = base64.b64encode(ctext)
        plain = self.crypt.decrypt(base64.b64decode(onthewire))
        assert plain.endswith(b"aces")
        assert db[plain[:-4]] == "foobar"


class TestDefaultToken(object):
    @pytest.fixture(autouse=True)
    def setup_token_handler(self):
        password = "The longer the better. Is this close to enough ?"
        grant_expires_in = 600
        self.th = DefaultToken(password, typ="A", lifetime=grant_expires_in)

    def test_default_token_split_token(self):
        _token = self.th("session_id")
        p = self.th.split_token(_token)
        assert p[1] == "A"
        assert p[2] == "session_id"

    def test_default_token_info(self):
        _token = self.th("another_id")
        _info = self.th.info(_token)

        assert set(_info.keys()) == {
            "_id",
            "type",
            "sid",
            "exp",
            "handler",
        }
        assert _info["handler"] == self.th

    def test_is_expired(self):
        _token = self.th("another_id")
        assert self.th.is_expired(_token) is False

        when = time.time() + 900
        assert self.th.is_expired(_token, when)


class TestTokenHandler(object):
    @pytest.fixture(autouse=True)
    def setup_token_handler(self):
        password = "The longer the better. Is this close to enough ?"
        grant_expires_in = 600
        token_expires_in = 900
        refresh_token_expires_in = 86400

        code_handler = DefaultToken(password, typ="A", lifetime=grant_expires_in)
        access_token_handler = DefaultToken(
            password, typ="T", lifetime=token_expires_in
        )
        refresh_token_handler = DefaultToken(
            password, typ="R", lifetime=refresh_token_expires_in
        )

        self.handler = TokenHandler(
            code_handler=code_handler,
            access_token_handler=access_token_handler,
            refresh_token_handler=refresh_token_handler,
        )

    def test_getitem(self):
        th = self.handler["code"]
        assert th.type == "A"
        th = self.handler["access_token"]
        assert th.type == "T"
        th = self.handler["refresh_token"]
        assert th.type == "R"

    def test_contains(self):
        assert "code" in self.handler
        assert "access_token" in self.handler
        assert "refresh_token" in self.handler

        assert "foobar" not in self.handler

    def test_info(self):
        _token = self.handler["code"]("another_id")
        _info = self.handler.info(_token)
        assert _info["type"] == "A"

    def test_sid(self):
        _token = self.handler["code"]("another_id")
        sid = self.handler.sid(_token)
        assert sid == "another_id"

    def test_type(self):
        _token = self.handler["code"]("another_id")
        assert self.handler.type(_token) == "A"

    def test_get_handler(self):
        _token = self.handler["code"]("another_id")
        th, _ = self.handler.get_handler(_token)
        assert th.type == "A"

    def test_keys(self):
        assert set(self.handler.keys()) == {"access_token", "code", "refresh_token"}


class DummyEndpointContext():
    def __init__(self):
        self.keyjar = None
        self.issuer = "issuer"
        self.cdb = {}


def test_token_handler_from_config():
    conf = {
        "token_handler_args": {
            "jwks_def": {
                "private_path": "private/token_jwks.json",
                "read_only": False,
                "key_defs": [
                    {"type": "oct", "bytes": "24", "use": ["enc"], "kid": "code"}
                ],
            },
            "code": {"lifetime": 600},
            "token": {
                "class": "oidcendpoint.jwt_token.JWTToken",
                "kwargs": {
                    "lifetime": 3600,
                    "add_claims_by_scope": True,
                    "aud": ["https://example.org/appl"],
                },
            },
            "refresh": {
                "class": "oidcendpoint.jwt_token.JWTToken",
                "kwargs": {
                    "lifetime": 3600,
                    "aud": ["https://example.org/appl"],
                }
            }
        }
    }

    token_handler = factory(DummyEndpointContext(), **conf["token_handler_args"])
    assert token_handler
    assert len(token_handler.handler) == 3
    assert set(token_handler.handler.keys()) == {"code", "access_token", "refresh_token"}
    assert isinstance(token_handler.handler["code"], DefaultToken)
    assert isinstance(token_handler.handler["access_token"], JWTToken)
    assert isinstance(token_handler.handler["refresh_token"], JWTToken)

    assert token_handler.handler["code"].lifetime == 600

    assert token_handler.handler["access_token"].alg == "ES256"
    assert token_handler.handler["access_token"].kwargs == {"add_claims_by_scope": True}
    assert token_handler.handler["access_token"].lifetime == 3600
    assert token_handler.handler["access_token"].def_aud == ["https://example.org/appl"]

    assert token_handler.handler["refresh_token"].alg == "ES256"
    assert token_handler.handler["refresh_token"].kwargs == {}
    assert token_handler.handler["refresh_token"].lifetime == 3600
    assert token_handler.handler["refresh_token"].def_aud == ["https://example.org/appl"]
