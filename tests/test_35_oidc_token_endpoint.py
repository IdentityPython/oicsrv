import json
import os

from cryptojwt import JWT
from cryptojwt.key_jar import build_keyjar
from oidcmsg.oidc import AccessTokenRequest
from oidcmsg.oidc import AuthorizationRequest
from oidcmsg.oidc import RefreshAccessTokenRequest
from oidcmsg.oidc import TokenErrorResponse
from oidcmsg.time_util import utc_time_sans_frac
import pytest

from oidcendpoint import JWT_BEARER
from oidcendpoint.authn_event import create_authn_event
from oidcendpoint.authz import AuthzHandling
from oidcendpoint.client_authn import verify_client
from oidcendpoint.endpoint_context import EndpointContext
from oidcendpoint.exception import UnAuthorizedClient
from oidcendpoint.oidc import userinfo
from oidcendpoint.oidc.authorization import Authorization
from oidcendpoint.oidc.provider_config import ProviderConfiguration
from oidcendpoint.oidc.registration import Registration
from oidcendpoint.oidc.token import Token
from oidcendpoint.session import session_key
from oidcendpoint.session import MintingNotAllowed
from oidcendpoint.user_authn.authn_context import INTERNETPROTOCOLPASSWORD
from oidcendpoint.user_info import UserInfo

KEYDEFS = [
    {"type": "RSA", "key": "", "use": ["sig"]},
    {"type": "EC", "crv": "P-256", "use": ["sig"]},
]

CLIENT_KEYJAR = build_keyjar(KEYDEFS)

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
    "subject_types_supported": ["public", "pairwise", "ephemeral"],
    "grant_types_supported": [
        "authorization_code",
        "implicit",
        "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "refresh_token",
    ],
}

AUTH_REQ = AuthorizationRequest(
    client_id="client_1",
    redirect_uri="https://example.com/cb",
    scope=["openid"],
    state="STATE",
    response_type="code",
)

TOKEN_REQ = AccessTokenRequest(
    client_id="client_1",
    redirect_uri="https://example.com/cb",
    state="STATE",
    grant_type="authorization_code",
    client_secret="hemligt",
)

REFRESH_TOKEN_REQ = RefreshAccessTokenRequest(
    grant_type="refresh_token", client_id="client_1", client_secret="hemligt"
)

TOKEN_REQ_DICT = TOKEN_REQ.to_dict()

BASEDIR = os.path.abspath(os.path.dirname(__file__))


def full_path(local_file):
    return os.path.join(BASEDIR, local_file)


USERINFO = UserInfo(json.loads(open(full_path("users.json")).read()))


@pytest.fixture
def conf():
    return {
        "issuer": "https://example.com/",
        "password": "mycket hemligt",
        "verify_ssl": False,
        "capabilities": CAPABILITIES,
        "keys": {"uri_path": "jwks.json", "key_defs": KEYDEFS},
        "endpoint": {
            "provider_config": {
                "path": ".well-known/openid-configuration",
                "class": ProviderConfiguration,
                "kwargs": {},
            },
            "registration": {
                "path": "registration",
                "class": Registration,
                "kwargs": {},
            },
            "authorization": {
                "path": "authorization",
                "class": Authorization,
                "kwargs": {},
            },
            "token": {
                "path": "token",
                "class": Token,
                "kwargs": {
                    "client_authn_method": [
                        "client_secret_basic",
                        "client_secret_post",
                        "client_secret_jwt",
                        "private_key_jwt",
                    ]
                },
            },
            "userinfo": {
                "path": "userinfo",
                "class": userinfo.UserInfo,
                "kwargs": {"db_file": "users.json"},
            },
        },
        "authentication": {
            "anon": {
                "acr": INTERNETPROTOCOLPASSWORD,
                "class": "oidcendpoint.user_authn.user.NoAuthn",
                "kwargs": {"user": "diana"},
            }
        },
        "userinfo": {"class": UserInfo, "kwargs": {"db": {}}},
        "client_authn": verify_client,
        "template_dir": "template",
        "authz": {
            "class": AuthzHandling,
            "kwargs": {
                "grant_config": {
                    "usage_rules": {
                        "authorization_code": {
                            "expires_in": 300,
                            'supports_minting': ["access_token", "refresh_token", "id_token"],
                            "max_usage": 1
                        },
                        "access_token": {"expires_in": 600},
                        "refresh_token": {
                            "expires_in": 86400,
                            'supports_minting': ["access_token", "refresh_token"],
                        }
                    },
                    "expires_in": 43200
                }
            }
        }
    }


class TestEndpoint(object):
    @pytest.fixture(autouse=True)
    def create_endpoint(self, conf):
        endpoint_context = EndpointContext(conf)
        endpoint_context.cdb["client_1"] = {
            "client_secret": "hemligt",
            "redirect_uris": [("https://example.com/cb", None)],
            "client_salt": "salted",
            "endpoint_auth_method": "client_secret_post",
            "response_types": ["code", "token", "code id_token", "id_token"],
        }
        endpoint_context.keyjar.import_jwks(CLIENT_KEYJAR.export_jwks(), "client_1")
        self.session_manager = endpoint_context.session_manager
        self.endpoint = endpoint_context.endpoint["token"]
        self.user_id = "diana"

    def test_init(self):
        assert self.endpoint

    def _create_session(self, auth_req, sub_type="public", sector_identifier=''):
        if sector_identifier:
            authz_req = auth_req.copy()
            authz_req["sector_identifier_uri"] = sector_identifier
        else:
            authz_req = auth_req
        client_id = authz_req['client_id']
        ae = create_authn_event(self.user_id)
        return self.session_manager.create_session(ae, authz_req, self.user_id,
                                                   client_id=client_id,
                                                   sub_type=sub_type)

    def _mint_code(self, grant, client_id):
        session_id = session_key(self.user_id, client_id, grant.id)
        usage_rules = grant.usage_rules.get("authorization_code", {})
        _exp_in = usage_rules.get("expires_in")

        # Constructing an authorization code is now done
        _code = grant.mint_token(
            session_id=session_id,
            endpoint_context=self.endpoint.endpoint_context,
            token_type='authorization_code',
            token_handler=self.session_manager.token_handler["code"],
            usage_rules=usage_rules
        )

        if _exp_in:
            if isinstance(_exp_in, str):
                _exp_in = int(_exp_in)
            if _exp_in:
                _code.expires_at = utc_time_sans_frac() + _exp_in
        return _code

    def _mint_access_token(self, grant, session_id, token_ref=None):
        _session_info = self.session_manager.get_session_info(session_id)
        usage_rules = grant.usage_rules.get("access_token", {})
        _exp_in = usage_rules.get("expires_in", 0)

        _token = grant.mint_token(
            _session_info,
            endpoint_context=self.endpoint.endpoint_context,
            token_type='access_token',
            token_handler=self.session_manager.token_handler["access_token"],
            based_on=token_ref,  # Means the token (tok) was used to mint this token
            usage_rules=usage_rules
        )
        if isinstance(_exp_in, str):
            _exp_in = int(_exp_in)
        if _exp_in:
            _token.expires_at = utc_time_sans_frac() + _exp_in

        return _token

    def test_parse(self):
        session_id = self._create_session(AUTH_REQ)
        grant = self.session_manager[session_id]
        code = self._mint_code(grant, AUTH_REQ['client_id'])

        _token_request = TOKEN_REQ_DICT.copy()
        _token_request["code"] = code.value
        _req = self.endpoint.parse_request(_token_request)

        assert set(_req.keys()) == set(_token_request.keys())

    def test_process_request(self):
        session_id = self._create_session(AUTH_REQ)
        grant = self.session_manager[session_id]
        code = self._mint_code(grant, AUTH_REQ['client_id'])

        _token_request = TOKEN_REQ_DICT.copy()
        _context = self.endpoint.endpoint_context
        _token_request["code"] = code.value
        _req = self.endpoint.parse_request(_token_request)
        _resp = self.endpoint.process_request(request=_req)

        assert _resp
        assert set(_resp.keys()) == {"http_headers", "response_args"}

    def test_process_request_using_code_twice(self):
        session_id = self._create_session(AUTH_REQ)
        grant = self.session_manager[session_id]
        code = self._mint_code(grant, AUTH_REQ['client_id'])

        _token_request = TOKEN_REQ_DICT.copy()
        _context = self.endpoint.endpoint_context
        _token_request["code"] = code.value

        _req = self.endpoint.parse_request(_token_request)
        _resp = self.endpoint.process_request(request=_req)

        # 2nd time used
        _2nd_response = self.endpoint.parse_request(_token_request)
        assert "error" in _2nd_response

    def test_do_response(self):
        session_id = self._create_session(AUTH_REQ)
        grant = self.session_manager[session_id]
        code = self._mint_code(grant, AUTH_REQ['client_id'])

        _token_request = TOKEN_REQ_DICT.copy()
        _token_request["code"] = code.value
        _req = self.endpoint.parse_request(_token_request)

        _resp = self.endpoint.process_request(request=_req)
        msg = self.endpoint.do_response(request=_req, **_resp)
        assert isinstance(msg, dict)

    def test_process_request_using_private_key_jwt(self):
        session_id = self._create_session(AUTH_REQ)
        grant = self.session_manager[session_id]
        code = self._mint_code(grant, AUTH_REQ['client_id'])

        _token_request = TOKEN_REQ_DICT.copy()
        del _token_request["client_id"]
        del _token_request["client_secret"]
        _context = self.endpoint.endpoint_context

        _jwt = JWT(CLIENT_KEYJAR, iss=AUTH_REQ["client_id"], sign_alg="RS256")
        _jwt.with_jti = True
        _assertion = _jwt.pack({"aud": [_context.endpoint["token"].full_path]})
        _token_request.update(
            {"client_assertion": _assertion, "client_assertion_type": JWT_BEARER}
        )
        _token_request["code"] = code.value

        _req = self.endpoint.parse_request(_token_request)
        _resp = self.endpoint.process_request(request=_req)

        # 2nd time used
        with pytest.raises(UnAuthorizedClient):
            self.endpoint.parse_request(_token_request)

    def test_do_refresh_access_token(self):
        areq = AUTH_REQ.copy()
        areq["scope"] = ["openid", "offline_access"]

        session_id = self._create_session(areq)
        grant = self.endpoint.endpoint_context.authz(session_id, areq)
        code = self._mint_code(grant, areq['client_id'])

        _cntx = self.endpoint.endpoint_context

        _token_request = TOKEN_REQ_DICT.copy()
        _token_request["code"] = code.value
        _req = self.endpoint.parse_request(_token_request)
        _resp = self.endpoint.process_request(request=_req)

        _request = REFRESH_TOKEN_REQ.copy()
        _request["refresh_token"] = _resp["response_args"]["refresh_token"]

        _token_value = _resp["response_args"]["refresh_token"]
        _session_info = self.session_manager.get_session_info_by_token(_token_value)
        _token = self.session_manager.find_token(_session_info["session_id"], _token_value)
        _token.usage_rules["supports_minting"] = ["access_token", "refresh_token", "id_token"]

        _req = self.endpoint.parse_request(_request.to_json())
        _resp = self.endpoint.process_request(request=_req)
        assert set(_resp.keys()) == {"response_args", "http_headers"}
        assert set(_resp["response_args"].keys()) == {
            "access_token",
            "token_type",
            "expires_in",
            "refresh_token",
            "id_token",
            "scope"
        }
        msg = self.endpoint.do_response(request=_req, **_resp)
        assert isinstance(msg, dict)

    def test_do_2nd_refresh_access_token(self):
        areq = AUTH_REQ.copy()
        areq["scope"] = ["openid", "offline_access"]

        session_id = self._create_session(areq)
        grant = self.endpoint.endpoint_context.authz(session_id, areq)
        code = self._mint_code(grant, areq['client_id'])

        _cntx = self.endpoint.endpoint_context

        _token_request = TOKEN_REQ_DICT.copy()
        _token_request["code"] = code.value
        _req = self.endpoint.parse_request(_token_request)
        _resp = self.endpoint.process_request(request=_req)

        _request = REFRESH_TOKEN_REQ.copy()
        _request["refresh_token"] = _resp["response_args"]["refresh_token"]

        # Make sure ID Tokens can also be used by this refesh token
        _token_value = _resp["response_args"]["refresh_token"]
        _session_info = self.session_manager.get_session_info_by_token(_token_value)
        _token = self.session_manager.find_token(_session_info["session_id"], _token_value)
        _token.usage_rules["supports_minting"] = ["access_token", "refresh_token", "id_token"]

        _req = self.endpoint.parse_request(_request.to_json())
        _resp = self.endpoint.process_request(request=_req)

        _2nd_request = REFRESH_TOKEN_REQ.copy()
        _2nd_request["refresh_token"] = _resp["response_args"]["refresh_token"]
        _2nd_req = self.endpoint.parse_request(_request.to_json())
        _2nd_resp = self.endpoint.process_request(request=_req)

        assert set(_2nd_resp.keys()) == {"response_args", "http_headers"}
        assert set(_2nd_resp["response_args"].keys()) == {
            "access_token",
            "token_type",
            "expires_in",
            "refresh_token",
            "id_token",
            "scope"
        }
        msg = self.endpoint.do_response(request=_req, **_resp)
        assert isinstance(msg, dict)

    def test_new_refresh_token(self, conf):
        self.endpoint.endpoint_context.cdb["client_1"] = {
            "client_secret": "hemligt",
            "redirect_uris": [("https://example.com/cb", None)],
            "client_salt": "salted",
            "endpoint_auth_method": "client_secret_post",
            "response_types": ["code", "token", "code id_token", "id_token"],
        }

        areq = AUTH_REQ.copy()
        areq["scope"] = ["openid", "offline_access"]

        session_id = self._create_session(areq)
        grant = self.endpoint.endpoint_context.authz(session_id, areq)
        code = self._mint_code(grant, areq['client_id'])

        _token_request = TOKEN_REQ_DICT.copy()
        _token_request["code"] = code.value
        _req = self.endpoint.parse_request(_token_request)
        _resp = self.endpoint.process_request(request=_req)
        assert "refresh_token" in _resp["response_args"]
        first_refresh_token = _resp["response_args"]["refresh_token"]

        _refresh_request = REFRESH_TOKEN_REQ.copy()
        _refresh_request["refresh_token"] = first_refresh_token
        _2nd_req = self.endpoint.parse_request(_refresh_request.to_json())
        _2nd_resp = self.endpoint.process_request(request=_2nd_req)
        assert "refresh_token" in _2nd_resp["response_args"]
        second_refresh_token = _2nd_resp["response_args"]["refresh_token"]

        _2d_refresh_request = REFRESH_TOKEN_REQ.copy()
        _2d_refresh_request["refresh_token"] = second_refresh_token
        _3rd_req = self.endpoint.parse_request(_2d_refresh_request.to_json())
        _3rd_resp = self.endpoint.process_request(request=_3rd_req)
        assert "access_token" in _3rd_resp["response_args"]
        assert "refresh_token" in _3rd_resp["response_args"]

        assert first_refresh_token != second_refresh_token

    def test_do_refresh_access_token_not_allowed(self):
        areq = AUTH_REQ.copy()
        areq["scope"] = ["openid", "offline_access"]

        session_id = self._create_session(areq)
        grant = self.endpoint.endpoint_context.authz(session_id, areq)
        code = self._mint_code(grant, areq['client_id'])

        _cntx = self.endpoint.endpoint_context

        _token_request = TOKEN_REQ_DICT.copy()
        _token_request["code"] = code.value
        # This is weird, issuing a refresh token that can't be used to mint anything
        # but it's testing so anything goes.
        grant.usage_rules["refresh_token"] = {"supports_minting": []}
        _req = self.endpoint.parse_request(_token_request)
        _resp = self.endpoint.process_request(request=_req)

        _request = REFRESH_TOKEN_REQ.copy()
        _request["refresh_token"] = _resp["response_args"]["refresh_token"]
        _req = self.endpoint.parse_request(_request.to_json())
        with pytest.raises(MintingNotAllowed):
            self.endpoint.process_request(_req)

    def test_do_refresh_access_token_revoked(self):
        areq = AUTH_REQ.copy()
        areq["scope"] = ["openid", "offline_access"]

        session_id = self._create_session(areq)
        grant = self.endpoint.endpoint_context.authz(session_id, areq)
        code = self._mint_code(grant, areq['client_id'])

        _cntx = self.endpoint.endpoint_context

        _token_request = TOKEN_REQ_DICT.copy()
        _token_request["code"] = code.value
        _req = self.endpoint.parse_request(_token_request)
        _resp = self.endpoint.process_request(request=_req)

        _refresh_token = _resp["response_args"]["refresh_token"]
        self.endpoint.endpoint_context.session_manager.revoke_token(session_id, _refresh_token)

        _request = REFRESH_TOKEN_REQ.copy()
        _request["refresh_token"] = _refresh_token
        _req = self.endpoint.parse_request(_request.to_json())
        # A revoked token is caught already when parsing the query.
        assert isinstance(_req, TokenErrorResponse)
