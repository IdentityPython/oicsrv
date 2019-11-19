# -*- coding: latin-1 -*-
import json

import pytest
from oidcmsg.oidc import RegistrationRequest

from oidcendpoint.client_authn import BearerHeader
from oidcendpoint.endpoint_context import EndpointContext
from oidcendpoint.oidc.authorization import Authorization
from oidcendpoint.oidc.read_registration import RegistrationRead
from oidcendpoint.oidc.registration import Registration
from oidcendpoint.oidc.token import AccessToken

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
            "capabilities": CAPABILITIES,
            "jwks": {"key_defs": KEYDEFS, "uri_path": "static/jwks.json"},
            "endpoint": {
                "registration": {
                    "path": "registration",
                    "class": Registration,
                    "kwargs": {"client_auth_method": None},
                },
                "registration_api": {
                    "path": "registration_api",
                    "class": RegistrationRead,
                    "kwargs": {"client_auth_method": {"bearer_header": BearerHeader}},
                },
                "authorization": {
                    "path": "authorization",
                    "class": Authorization,
                    "kwargs": {},
                },
                "token": {"path": "token", "class": AccessToken, "kwargs": {}},
            },
            "template_dir": "template",
        }
        endpoint_context = EndpointContext(conf)
        self.registration_endpoint = endpoint_context.endpoint["registration"]
        self.registration_api_endpoint = endpoint_context.endpoint["registration_read"]

    def test_do_response(self):
        _req = self.registration_endpoint.parse_request(CLI_REQ.to_json())
        _resp = self.registration_endpoint.process_request(request=_req)
        msg = self.registration_endpoint.do_response(**_resp)
        assert isinstance(msg, dict)
        _msg = json.loads(msg["response"])
        assert _msg

        _api_req = self.registration_api_endpoint.parse_request(
            "client_id={}".format(_resp["response_args"]["client_id"]),
            auth="Bearer {}".format(
                _resp["response_args"]["registration_access_token"]
            ),
        )
        assert set(_api_req.keys()) == {"client_id"}

        _info = self.registration_api_endpoint.process_request(request=_api_req)
        assert set(_info.keys()) == {"response_args"}
        assert _info["response_args"] == _resp["response_args"]

        _endp_response = self.registration_api_endpoint.do_response(_info)
        assert set(_endp_response.keys()) == {"response", "http_headers"}
        assert ("Content-type", "application/json") in _endp_response["http_headers"]
