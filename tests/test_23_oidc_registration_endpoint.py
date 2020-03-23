# -*- coding: latin-1 -*-
import json

import pytest
import responses

from oidcendpoint.endpoint_context import EndpointContext
from oidcendpoint.id_token import IDToken
from oidcendpoint.oidc.authorization import Authorization
from oidcendpoint.oidc.registration import Registration
from oidcendpoint.oidc.registration import match_sp_sep
from oidcendpoint.oidc.token import AccessToken
from oidcendpoint.oidc.userinfo import UserInfo
from oidcmsg.oidc import RegistrationRequest
from oidcmsg.oidc import RegistrationResponse

KEYDEFS = [
    {"type": "RSA", "key": "", "use": ["sig"]},
    {"type": "EC", "crv": "P-256", "use": ["sig"]},
]

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

msg = {
    "application_type": "web",
    "redirect_uris": [
        "https://client.example.org/callback",
        "https://client.example.org/callback2",
    ],
    "client_name": "My Example",
    "client_name#ja-Jpan-JP": "クライアント名",
    "subject_type": "pairwise",
    "token_endpoint_auth_method": "client_secret_basic",
    "jwks_uri": "https://client.example.org/my_public_keys.jwks",
    "userinfo_encrypted_response_alg": "RSA1_5",
    "userinfo_encrypted_response_enc": "A128CBC-HS256",
    "contacts": ["ve7jtb@example.org", "mary@example.org"],
    "request_uris": [
        "https://client.example.org/rf.txt#qpXaRLh_n93TT",
        "https://client.example.org/rf.txt",
    ],
    "post_logout_redirect_uris": [
        "https://rp.example.com/pl?foo=bar",
        "https://rp.example.com/pl",
    ],
}

CLI_REQ = RegistrationRequest(**msg)


class TestEndpoint(object):
    @pytest.fixture(autouse=True)
    def create_endpoint(self):
        conf = {
            "issuer": "https://example.com/",
            "password": "mycket hemligt",
            "token_expires_in": 600,
            "grant_expires_in": 300,
            "refresh_token_expires_in": 86400,
            "verify_ssl": False,
            "capabilities": {
                "subject_types_supported": ["public", "pairwise"],
                "grant_types_supported": [
                    "authorization_code",
                    "implicit",
                    "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "refresh_token",
                ],
            },
            "jwks": {"key_defs": KEYDEFS, "uri_path": "static/jwks.json"},
            "id_token": {"class": IDToken},
            "endpoint": {
                "registration": {
                    "path": "registration",
                    "class": Registration,
                    "kwargs": {"client_auth_method": None},
                },
                "authorization": {
                    "path": "authorization",
                    "class": Authorization,
                    "kwargs": {
                        "response_types_supported": [
                            " ".join(x) for x in RESPONSE_TYPES_SUPPORTED
                        ],
                        "response_modes_supported": ["query", "fragment", "form_post"],
                        "claim_types_supported": [
                            "normal",
                            "aggregated",
                            "distributed",
                        ],
                        "claims_parameter_supported": True,
                        "request_parameter_supported": True,
                        "request_uri_parameter_supported": True,
                    },
                },
                "token": {
                    "path": "token",
                    "class": AccessToken,
                    "kwargs": {
                        "client_authn_method": [
                            "client_secret_post",
                            "client_secret_basic",
                            "client_secret_jwt",
                            "private_key_jwt",
                        ]
                    },
                },
                "userinfo": {"path": "userinfo", "class": UserInfo, "kwargs": {}},
            },
            "template_dir": "template",
        }
        endpoint_context = EndpointContext(conf)
        self.endpoint = endpoint_context.endpoint["registration"]

    def test_parse(self):
        _req = self.endpoint.parse_request(CLI_REQ.to_json())

        assert isinstance(_req, RegistrationRequest)
        assert set(_req.keys()) == set(CLI_REQ.keys())

    def test_process_request(self):
        _req = self.endpoint.parse_request(CLI_REQ.to_json())
        _resp = self.endpoint.process_request(request=_req)
        _reg_resp = _resp["response_args"]
        assert isinstance(_reg_resp, RegistrationResponse)
        assert "client_id" in _reg_resp and "client_secret" in _reg_resp

    def test_do_response(self):
        _req = self.endpoint.parse_request(CLI_REQ.to_json())
        _resp = self.endpoint.process_request(request=_req)
        msg = self.endpoint.do_response(**_resp)
        assert isinstance(msg, dict)
        _msg = json.loads(msg["response"])
        assert _msg

    def test_register_unsupported_str(self):
        _msg = msg.copy()
        _msg["id_token_signed_response_alg"] = "XYZ256"
        _req = self.endpoint.parse_request(RegistrationRequest(**_msg).to_json())
        _resp = self.endpoint.process_request(request=_req)
        assert _resp["error"] == "invalid_request"

    def test_register_unsupported_set(self):
        _msg = msg.copy()
        _msg["grant_types"] = ["authorization_code", "external"]
        _req = self.endpoint.parse_request(RegistrationRequest(**_msg).to_json())
        _resp = self.endpoint.process_request(request=_req)
        assert _resp["error"] == "invalid_request"

    def test_register_post_logout_redirect_uri_with_fragment(self):
        _msg = msg.copy()
        _msg["post_logout_redirect_uris"] = ["https://rp.example.com/pl#fragment"]
        _req = self.endpoint.parse_request(RegistrationRequest(**_msg).to_json())
        _resp = self.endpoint.process_request(request=_req)
        assert _resp["error"] == "invalid_configuration_parameter"

    def test_register_redirect_uri_with_fragment(self):
        _msg = msg.copy()
        _msg["post_logout_redirect_uris"] = ["https://rp.example.com/cb#fragment"]
        _req = self.endpoint.parse_request(RegistrationRequest(**_msg).to_json())
        _resp = self.endpoint.process_request(request=_req)
        assert _resp["error"] == "invalid_configuration_parameter"

    def test_register_sector_identifier_uri(self):
        _msg = msg.copy()
        _msg["sector_identifier_uri"] = "https://rp.example.com/si#fragment"
        _req = self.endpoint.parse_request(RegistrationRequest(**_msg).to_json())
        _resp = self.endpoint.process_request(request=_req)
        assert _resp["error"] == "invalid_configuration_parameter"

    def test_register_alg_keys(self):
        _msg = msg.copy()
        _msg["id_token_signed_response_alg"] = "RS256"
        _msg["userinfo_signed_response_alg"] = "ES256"
        _req = self.endpoint.parse_request(RegistrationRequest(**_msg).to_json())
        _resp = self.endpoint.process_request(request=_req)
        assert "response_args" in _resp

    def test_register_custom_redirect_uri_web(self):
        _msg = msg.copy()
        _msg["redirect_uris"] = ["custom://cb.example.com"]
        _req = self.endpoint.parse_request(RegistrationRequest(**_msg).to_json())
        _resp = self.endpoint.process_request(request=_req)
        assert "error" in _resp

    def test_register_custom_redirect_uri_native(self):
        _msg = msg.copy()
        _msg["redirect_uris"] = ["custom://cb.example.com"]
        _msg["application_type"] = "native"
        _req = self.endpoint.parse_request(RegistrationRequest(**_msg).to_json())
        _resp = self.endpoint.process_request(request=_req)
        assert "response_args" in _resp

    def test_sector_uri_missing_redirect_uri(self):
        _url = "https://github.com/sector"

        _msg = msg.copy()
        _msg["redirect_uris"] = ["custom://cb.example.com"]
        _msg["application_type"] = "native"
        _msg["sector_identifier_uri"] = _url

        with responses.RequestsMock() as rsps:
            rsps.add("GET", _url,
                     body=json.dumps(["https://example.com", "https://example.org"]),
                     adding_headers={"Content-Type": "application/json"}, status=200)

        _req = self.endpoint.parse_request(RegistrationRequest(**_msg).to_json())
        _resp = self.endpoint.process_request(request=_req)
        assert "error" in _resp

    def test_incorrect_request(self):
        _msg = msg.copy()
        _msg["default_max_age"] = "five"
        with pytest.raises(ValueError):
            self.endpoint.parse_request(RegistrationRequest(**_msg).to_json())

    def test_no_client_expiration_time(self):
        self.endpoint.kwargs["client_secret_expires"] = False
        _req = self.endpoint.parse_request(CLI_REQ.to_json())
        _resp = self.endpoint.process_request(request=_req)
        assert _resp


def test_match_sp_sep():
    assert match_sp_sep("foo bar", "bar foo")
    assert match_sp_sep(["foo", "bar"], "bar foo")
    assert match_sp_sep("foo bar", ["bar", "foo"])
    assert match_sp_sep(["foo", "bar"], ["bar", "foo"])

    assert match_sp_sep("foo bar exp", "bar foo") is False
    assert match_sp_sep(["foo", "bar", "exp"], "bar foo") is False
    assert match_sp_sep("foo bar exp", ["bar", "foo"]) is False
    assert match_sp_sep(["foo", "bar", "exp"], ["bar", "foo"]) is False
