import copy
import json
import os
from urllib.parse import parse_qs
from urllib.parse import urlparse

from cryptojwt import as_unicode
from cryptojwt import b64d
from cryptojwt.key_jar import build_keyjar
from cryptojwt.utils import as_bytes
from oidcmsg.exception import InvalidRequest
from oidcmsg.message import Message
from oidcmsg.oidc import AuthorizationRequest
from oidcmsg.oidc import verified_claim_name
from oidcmsg.oidc import verify_id_token
from oidcmsg.time_util import time_sans_frac
import pytest
import responses

from oidcendpoint.authn_event import create_authn_event
from oidcendpoint.common.authorization import join_query
from oidcendpoint.cookie import CookieDealer
from oidcendpoint.cookie import new_cookie
from oidcendpoint.endpoint_context import EndpointContext
from oidcendpoint.exception import RedirectURIError
from oidcendpoint.grant import Grant
from oidcendpoint.oidc import userinfo
from oidcendpoint.oidc.authorization import Authorization
from oidcendpoint.oidc.provider_config import ProviderConfiguration
from oidcendpoint.oidc.registration import Registration
from oidcendpoint.oidc.session import Session
from oidcendpoint.oidc.session import do_front_channel_logout_iframe
from oidcendpoint.oidc.token import Token
from oidcendpoint.session_management import db_key
from oidcendpoint.session_management import unpack_db_key
from oidcendpoint.user_authn.authn_context import INTERNETPROTOCOLPASSWORD
from oidcendpoint.user_info import UserInfo

ISS = "https://example.com/"

CLI1 = "https://client1.example.com/"
CLI2 = "https://client2.example.com/"

KEYDEFS = [
    {"type": "RSA", "use": ["sig"]},
    {"type": "EC", "crv": "P-256", "use": ["sig"]},
]

KEYJAR = build_keyjar(KEYDEFS)
KEYJAR.import_jwks(KEYJAR.export_jwks(private=True), ISS)

RESPONSE_TYPES_SUPPORTED = [
    ["code"],
    ["token"],
    ["id_token"],
    ["code", "token"],
    ["code", "id_token"],
    ["id_token", "token"],
    ["code", "token", "id_token"],
    ["none"],
]

CAPABILITIES = {
    "response_types_supported": [" ".join(x) for x in RESPONSE_TYPES_SUPPORTED],
    "token_endpoint_auth_methods_supported": [
        "client_secret_post",
        "client_secret_basic",
        "client_secret_jwt",
        "private_key_jwt",
    ],
    "response_modes_supported": ["query", "fragment", "form_post"],
    "subject_types_supported": ["public", "pairwise"],
    "grant_types_supported": [
        "authorization_code",
        "implicit",
        "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "refresh_token",
    ],
    "claim_types_supported": ["normal", "aggregated", "distributed"],
    "claims_parameter_supported": True,
    "request_parameter_supported": True,
    "request_uri_parameter_supported": True,
}

AUTH_REQ = AuthorizationRequest(
    client_id="client_1",
    redirect_uri="{}cb".format(ISS),
    scope=["openid"],
    state="STATE",
    response_type="code",
    client_secret="hemligt",
)

AUTH_REQ_DICT = AUTH_REQ.to_dict()

BASEDIR = os.path.abspath(os.path.dirname(__file__))


def full_path(local_file):
    return os.path.join(BASEDIR, local_file)


USERINFO_db = json.loads(open(full_path("users.json")).read())


class TestEndpoint(object):
    @pytest.fixture(autouse=True)
    def create_endpoint(self):
        conf = {
            "issuer": ISS,
            "password": "mycket hemlig zebra",
            "token_expires_in": 600,
            "grant_expires_in": 300,
            "refresh_token_expires_in": 86400,
            "verify_ssl": False,
            "capabilities": CAPABILITIES,
            "keys": {"uri_path": "jwks.json", "key_defs": KEYDEFS},
            "endpoint": {
                "provider_config": {
                    "path": "{}/.well-known/openid-configuration",
                    "class": ProviderConfiguration,
                    "kwargs": {"client_authn_method": None},
                },
                "registration": {
                    "path": "{}/registration",
                    "class": Registration,
                    "kwargs": {"client_authn_method": None},
                },
                "authorization": {
                    "path": "{}/authorization",
                    "class": Authorization,
                    "kwargs": {"client_authn_method": None},
                },
                "token": {"path": "{}/token", "class": Token, "kwargs": {}},
                "userinfo": {
                    "path": "{}/userinfo",
                    "class": userinfo.UserInfo,
                    "kwargs": {"db_file": "users.json"},
                },
                "session": {
                    "path": "{}/end_session",
                    "class": Session,
                    "kwargs": {
                        "post_logout_uri_path": "post_logout",
                        "signing_alg": "ES256",
                        "logout_verify_url": "{}/verify_logout".format(ISS),
                        "client_authn_method": None,
                    },
                },
            },
            "authentication": {
                "anon": {
                    "acr": INTERNETPROTOCOLPASSWORD,
                    "class": "oidcendpoint.user_authn.user.NoAuthn",
                    "kwargs": {"user": "diana"},
                }
            },
            "userinfo": {"class": UserInfo, "kwargs": {"db": USERINFO_db}},
            "template_dir": "template",
            # 'cookie_name':{
            #     'session': 'oidcop',
            #     'register': 'oidcreg'
            # }
        }
        cookie_conf = {
            "sign_key": "ghsNKDDLshZTPn974nOsIGhedULrsqnsGoBFBLwUKuJhE2ch",
            "default_values": {
                "name": "oidcop",
                "domain": "127.0.0.1",
                "path": "/",
                "max_age": 3600,
            },
        }

        self.cd = CookieDealer(**cookie_conf)
        endpoint_context = EndpointContext(conf, cookie_dealer=self.cd, keyjar=KEYJAR)
        endpoint_context.cdb = {
            "client_1": {
                "client_secret": "hemligt",
                "redirect_uris": [("{}cb".format(CLI1), None)],
                "client_salt": "salted",
                "token_endpoint_auth_method": "client_secret_post",
                "response_types": ["code", "token", "code id_token", "id_token"],
                "post_logout_redirect_uris": [("{}logout_cb".format(CLI1), "")],
            },
            "client_2": {
                "client_secret": "hemligare",
                "redirect_uris": [("{}cb".format(CLI2), None)],
                "client_salt": "saltare",
                "token_endpoint_auth_method": "client_secret_post",
                "response_types": ["code", "token", "code id_token", "id_token"],
                "post_logout_redirect_uris": [("{}logout_cb".format(CLI2), "")],
            },
        }
        self.session_manager = endpoint_context.session_manager
        self.authn_endpoint = endpoint_context.endpoint["authorization"]
        self.session_endpoint = endpoint_context.endpoint["session"]
        self.token_endpoint = endpoint_context.endpoint["token"]
        self.user_id = "diana"

    def _create_session(self, auth_req, user_id="", sub_type="public", sector_identifier=''):
        if not user_id:
            user_id = self.user_id
        client_id = auth_req['client_id']
        ae = create_authn_event(self.user_id, self.session_manager.salt)
        self.session_manager.create_session(ae, auth_req, user_id, client_id=client_id,
                                            sub_type=sub_type,
                                            sector_identifier=sector_identifier)
        return db_key(self.user_id, client_id)

    def _do_grant(self, auth_req, user_id=''):
        if not user_id:
            user_id = self.user_id
        client_id = auth_req['client_id']
        # The user consent module produces a Grant instance
        grant = Grant(scope=auth_req['scope'], resources=[client_id])

        # the grant is assigned to a session (user_id, client_id)
        self.session_manager.set([user_id, client_id, grant.id], grant)
        return db_key(user_id, client_id, grant.id)

    def _mint_code(self, grant, session_id):
        # Constructing an authorization code is now done
        return grant.mint_token(
            'authorization_code',
            value=self.session_manager.token_handler["code"](session_id),
            expires_at=time_sans_frac() + 300  # 5 minutes from now
        )

    def _mint_access_token(self, grant, session_id, token_ref=None):
        _session_info = self.session_manager.get_session_info(session_id)
        return grant.mint_token(
            'access_token',
            value=self.session_manager.token_handler["access_token"](
                session_id,
                client_id=_session_info["client_id"],
                aud=grant.resources,
                user_claims=None,
                scope=grant.scope,
                sub=_session_info["client_session_info"]['sub']
            ),
            expires_at=time_sans_frac() + 900,  # 15 minutes from now
            based_on=token_ref  # Means the token (tok) was used to mint this token
        )

    def test_end_session_endpoint(self):
        # End session not allowed if no cookie and no id_token_hint is sent
        # (can't determine session)
        with pytest.raises(ValueError):
            _ = self.session_endpoint.process_request("", cookie="FAIL")

    def _create_cookie(self, session_id):
        ec = self.session_endpoint.endpoint_context
        return new_cookie(
            ec,
            sid=session_id,
            cookie_name=ec.cookie_name["session"],
        )

    def _code_auth(self, state):
        req = AuthorizationRequest(
            state=state,
            response_type="code",
            redirect_uri="{}cb".format(CLI1),
            scope=["openid"],
            client_id="client_1",
        )
        _pr_resp = self.authn_endpoint.parse_request(req.to_dict())
        return self.authn_endpoint.process_request(_pr_resp)

    def _code_auth2(self, state):
        req = AuthorizationRequest(
            state=state,
            response_type="code",
            redirect_uri="{}cb".format(CLI2),
            scope=["openid"],
            client_id="client_2",
        )
        _pr_resp = self.authn_endpoint.parse_request(req.to_dict())
        return self.authn_endpoint.process_request(_pr_resp)

    def _auth_with_id_token(self, state):
        req = AuthorizationRequest(
            state=state,
            response_type="id_token",
            redirect_uri="{}cb".format(CLI1),
            scope=["openid"],
            client_id="client_1",
            nonce="_nonce_",
        )
        _pr_resp = self.authn_endpoint.parse_request(req.to_dict())
        _resp = self.authn_endpoint.process_request(_pr_resp)

        part = self.session_endpoint.endpoint_context.cookie_dealer.get_cookie_value(
            _resp["cookie"][0], cookie_name="oidcop"
        )
        # value is a base64 encoded JSON document
        _cookie_info = json.loads(as_unicode(b64d(as_bytes(part[0]))))

        return _resp["response_args"], _cookie_info["sid"]

    def test_end_session_endpoint_with_cookie(self):
        _resp = self._code_auth("1234567")
        _code = _resp["response_args"]["code"]
        _session_info = self.session_manager.get_session_info_by_token(_code)
        cookie = self._create_cookie(_session_info["session_id"])

        _req_args = self.session_endpoint.parse_request({"state": "1234567"})
        resp = self.session_endpoint.process_request(_req_args, cookie=cookie)

        # returns a signed JWT to be put in a verification web page shown to
        # the user

        p = urlparse(resp["redirect_location"])
        qs = parse_qs(p.query)
        jwt_info = self.session_endpoint.unpack_signed_jwt(qs["sjwt"][0])

        assert jwt_info["sid"] == _session_info["session_id"]
        assert jwt_info["redirect_uri"] == "https://example.com/post_logout"

    def test_end_session_endpoint_with_cookie_and_unknown_sid(self):
        # Need cookie and ID Token to figure this out
        resp_args, _session_id = self._auth_with_id_token("1234567")
        id_token = resp_args["id_token"]

        _uid, _cid, _gid = unpack_db_key(_session_id)
        cookie = self._create_cookie(db_key(_uid, "client_66", _gid))

        with pytest.raises(ValueError):
            self.session_endpoint.process_request({"state": "foo"}, cookie=cookie)

    def test_end_session_endpoint_with_cookie_id_token_and_unknown_sid(self):
        # Need cookie and ID Token to figure this out
        resp_args, _session_id = self._auth_with_id_token("1234567")
        id_token = resp_args["id_token"]

        _uid, _cid, _gid = unpack_db_key(_session_id)
        cookie = self._create_cookie(db_key(_uid, "client_66", _gid))

        msg = Message(id_token=id_token)
        verify_id_token(msg, keyjar=self.session_endpoint.endpoint_context.keyjar)

        msg2 = Message(id_token_hint=id_token)
        msg2[verified_claim_name("id_token_hint")] = msg[
            verified_claim_name("id_token")
        ]
        with pytest.raises(ValueError):
            self.session_endpoint.process_request(msg2, cookie=cookie)

    def test_end_session_endpoint_with_cookie_dual_login(self):
        _resp = self._code_auth("1234567")
        self._code_auth2("abcdefg")
        _code = _resp["response_args"]["code"]
        _session_info = self.session_manager.get_session_info_by_token(_code)
        cookie = self._create_cookie(_session_info["session_id"])

        resp = self.session_endpoint.process_request({"state": "abcde"}, cookie=cookie)

        # returns a signed JWT to be put in a verification web page shown to
        # the user

        p = urlparse(resp["redirect_location"])
        qs = parse_qs(p.query)
        jwt_info = self.session_endpoint.unpack_signed_jwt(qs["sjwt"][0])

        assert jwt_info["sid"] == _session_info["session_id"]
        assert jwt_info["redirect_uri"] == "https://example.com/post_logout"

    def test_end_session_endpoint_with_post_logout_redirect_uri(self):
        _resp = self._code_auth("1234567")
        self._code_auth2("abcdefg")
        _code = _resp["response_args"]["code"]
        _session_info = self.session_manager.get_session_info_by_token(_code)
        cookie = self._create_cookie(_session_info["session_id"])

        post_logout_redirect_uri = join_query(
            *self.session_endpoint.endpoint_context.cdb["client_1"][
                "post_logout_redirect_uris"
            ][0]
        )

        with pytest.raises(InvalidRequest):
            self.session_endpoint.process_request(
                {
                    "post_logout_redirect_uri": post_logout_redirect_uri,
                    "state": "abcde",
                },
                cookie=cookie,
            )

    def test_end_session_endpoint_with_wrong_post_logout_redirect_uri(self):
        _resp = self._code_auth("1234567")
        self._code_auth2("abcdefg")

        resp_args, _session_id = self._auth_with_id_token("1234567")
        id_token = resp_args["id_token"]

        cookie = self._create_cookie(_session_id)

        post_logout_redirect_uri = "https://demo.example.com/log_out"

        msg = Message(id_token=id_token)
        verify_id_token(msg, keyjar=self.session_endpoint.endpoint_context.keyjar)

        with pytest.raises(RedirectURIError):
            self.session_endpoint.process_request(
                {
                    "post_logout_redirect_uri": post_logout_redirect_uri,
                    "state": "abcde",
                    "id_token_hint": id_token,
                    verified_claim_name("id_token_hint"): msg[
                        verified_claim_name("id_token")
                    ],
                },
                cookie=cookie,
            )

    def test_back_channel_logout_no_uri(self):
        self._code_auth("1234567")

        res = self.session_endpoint.do_back_channel_logout(
            self.session_endpoint.endpoint_context.cdb["client_1"], "username", 0
        )
        assert res is None

    def test_back_channel_logout(self):
        self._code_auth("1234567")

        _cdb = copy.copy(self.session_endpoint.endpoint_context.cdb["client_1"])
        _cdb["backchannel_logout_uri"] = "https://example.com/bc_logout"
        _cdb["client_id"] = "client_1"
        res = self.session_endpoint.do_back_channel_logout(_cdb, "username", "_sid_")
        assert isinstance(res, tuple)
        assert res[0] == "https://example.com/bc_logout"
        _jwt = self.session_endpoint.unpack_signed_jwt(res[1], "RS256")
        assert _jwt
        assert _jwt["iss"] == ISS
        assert _jwt["aud"] == ["client_1"]
        assert _jwt["sub"] == "username"
        assert "sid" in _jwt

    def test_front_channel_logout(self):
        self._code_auth("1234567")

        _cdb = copy.copy(self.session_endpoint.endpoint_context.cdb["client_1"])
        _cdb["frontchannel_logout_uri"] = "https://example.com/fc_logout"
        _cdb["client_id"] = "client_1"
        res = do_front_channel_logout_iframe(_cdb, ISS, "_sid_")
        assert res == '<iframe src="https://example.com/fc_logout">'

    def test_front_channel_logout_session_required(self):
        self._code_auth("1234567")

        _cdb = copy.copy(self.session_endpoint.endpoint_context.cdb["client_1"])
        _cdb["frontchannel_logout_uri"] = "https://example.com/fc_logout"
        _cdb["frontchannel_logout_session_required"] = True
        _cdb["client_id"] = "client_1"
        res = do_front_channel_logout_iframe(_cdb, ISS, "_sid_")
        test_res = (
            '<iframe src="https://example.com/fc_logout?',
            "iss=https%3A%2F%2Fexample.com%2F",
            "sid=_sid_",
        )
        for i in test_res:
            assert i in res

    def test_front_channel_logout_with_query(self):
        self._code_auth("1234567")

        _cdb = copy.copy(self.session_endpoint.endpoint_context.cdb["client_1"])
        _cdb["frontchannel_logout_uri"] = "https://example.com/fc_logout?entity_id=foo"
        _cdb["frontchannel_logout_session_required"] = True
        _cdb["client_id"] = "client_1"
        res = do_front_channel_logout_iframe(_cdb, ISS, "_sid_")
        test_res = (
            "<iframe",
            'src="https://example.com/fc_logout?',
            "entity_id=foo",
            "iss=https%3A%2F%2Fexample.com%2F",
            "sid=_sid_",
        )
        for i in test_res:
            assert i in res

    def test_logout_from_client_bc(self):
        _resp = self._code_auth("1234567")
        _code = _resp["response_args"]["code"]
        _session_info = self.session_manager.get_session_info_by_token(_code)

        self.session_endpoint.endpoint_context.cdb["client_1"][
            "backchannel_logout_uri"
        ] = "https://example.com/bc_logout"
        self.session_endpoint.endpoint_context.cdb["client_1"]["client_id"] = "client_1"

        res = self.session_endpoint.logout_from_client(_session_info["session_id"], "client_1")
        assert set(res.keys()) == {"blu"}
        assert set(res["blu"].keys()) == {"client_1"}
        _spec = res["blu"]["client_1"]
        assert _spec[0] == "https://example.com/bc_logout"
        _jwt = self.session_endpoint.unpack_signed_jwt(_spec[1], "RS256")
        assert _jwt
        assert _jwt["iss"] == ISS
        assert _jwt["aud"] == ["client_1"]
        assert "sid" in _jwt  # This session ID is not the same as the session_id mentioned above

        _sid = self.session_endpoint._decrypt_sid(_jwt["sid"])
        assert _sid == _session_info["session_id"]
        assert _session_info["client_session_info"].is_revoked()

    def test_logout_from_client_fc(self):
        _resp = self._code_auth("1234567")
        _code = _resp["response_args"]["code"]
        _session_info = self.session_manager.get_session_info_by_token(_code)

        # del self.session_endpoint.endpoint_context.cdb['client_1']['backchannel_logout_uri']
        self.session_endpoint.endpoint_context.cdb["client_1"][
            "frontchannel_logout_uri"
        ] = "https://example.com/fc_logout"
        self.session_endpoint.endpoint_context.cdb["client_1"]["client_id"] = "client_1"

        res = self.session_endpoint.logout_from_client(_session_info["session_id"], "client_1")
        assert set(res.keys()) == {"flu"}
        assert set(res["flu"].keys()) == {"client_1"}
        _spec = res["flu"]["client_1"]
        assert _spec == '<iframe src="https://example.com/fc_logout">'

        assert _session_info["client_session_info"].is_revoked()

    def test_logout_from_client(self):
        _resp = self._code_auth("1234567")
        _code = _resp["response_args"]["code"]
        _session_info = self.session_manager.get_session_info_by_token(_code)
        self._code_auth2("abcdefg")

        # client0
        self.session_endpoint.endpoint_context.cdb["client_1"][
            "backchannel_logout_uri"
        ] = "https://example.com/bc_logout"
        self.session_endpoint.endpoint_context.cdb["client_1"]["client_id"] = "client_1"
        self.session_endpoint.endpoint_context.cdb["client_2"][
            "frontchannel_logout_uri"
        ] = "https://example.com/fc_logout"
        self.session_endpoint.endpoint_context.cdb["client_2"]["client_id"] = "client_2"

        res = self.session_endpoint.logout_all_clients(_session_info["session_id"], "client_1")

        assert res
        assert set(res.keys()) == {"blu", "flu"}
        assert set(res["flu"].keys()) == {"client_2"}
        _spec = res["flu"]["client_2"]
        assert _spec == '<iframe src="https://example.com/fc_logout">'
        assert set(res["blu"].keys()) == {"client_1"}
        _spec = res["blu"]["client_1"]
        assert _spec[0] == "https://example.com/bc_logout"
        _jwt = self.session_endpoint.unpack_signed_jwt(_spec[1], "RS256")
        assert _jwt
        assert _jwt["iss"] == ISS
        assert _jwt["aud"] == ["client_1"]

        # both should be revoked
        assert _session_info["client_session_info"].is_revoked()
        _cinfo = self.session_manager[db_key(self.user_id, "client_2")]
        assert _cinfo.is_revoked()

    def test_do_verified_logout(self):
        with responses.RequestsMock() as rsps:
            rsps.add("POST", "https://example.com/bc_logout", body="OK", status=200)

            _resp = self._code_auth("1234567")
            _code = _resp["response_args"]["code"]
            _session_info = self.session_manager.get_session_info_by_token(_code)
            _cdb = self.session_endpoint.endpoint_context.cdb
            _cdb["client_1"]["backchannel_logout_uri"] = "https://example.com/bc_logout"
            _cdb["client_1"]["client_id"] = "client_1"

            res = self.session_endpoint.do_verified_logout(_session_info["session_id"], "client_1")
            assert res == []

    def test_logout_from_client_unknow_sid(self):
        _resp = self._code_auth("1234567")
        _code = _resp["response_args"]["code"]
        _session_info = self.session_manager.get_session_info_by_token(_code)
        self._code_auth2("abcdefg")

        _uid, _cid, _gid = unpack_db_key(_session_info["session_id"])
        _sid = db_key('babs', _cid, _gid)
        with pytest.raises(KeyError):
            res = self.session_endpoint.logout_all_clients(_sid, "client_1")

    def test_logout_from_client_no_session(self):
        _resp = self._code_auth("1234567")
        _code = _resp["response_args"]["code"]
        _session_info = self.session_manager.get_session_info_by_token(_code)
        self._code_auth2("abcdefg")

        # client0
        self.session_endpoint.endpoint_context.cdb["client_1"][
            "backchannel_logout_uri"
        ] = "https://example.com/bc_logout"
        self.session_endpoint.endpoint_context.cdb["client_1"]["client_id"] = "client_1"
        self.session_endpoint.endpoint_context.cdb["client_2"][
            "frontchannel_logout_uri"
        ] = "https://example.com/fc_logout"
        self.session_endpoint.endpoint_context.cdb["client_2"]["client_id"] = "client_2"

        _uid, _cid, _gid = unpack_db_key(_session_info["session_id"])
        self.session_endpoint.endpoint_context.session_manager.delete([_uid, _cid])

        with pytest.raises(ValueError):
            self.session_endpoint.logout_all_clients(_session_info["session_id"], "client_1")
