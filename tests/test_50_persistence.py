import json
import os
import shutil

import pytest
from cryptojwt.jwt import utc_time_sans_frac
from oidcmsg.oidc import AccessTokenRequest
from oidcmsg.oidc import AuthorizationRequest

from oidcendpoint import user_info
from oidcendpoint.authn_event import create_authn_event
from oidcendpoint.authz import AuthzHandling
from oidcendpoint.endpoint_context import EndpointContext
from oidcendpoint.id_token import IDToken
from oidcendpoint.oidc import userinfo
from oidcendpoint.oidc.authorization import Authorization
from oidcendpoint.oidc.provider_config import ProviderConfiguration
from oidcendpoint.oidc.registration import Registration
from oidcendpoint.oidc.token import Token
from oidcendpoint.session import session_key
from oidcendpoint.session import unpack_session_key
from oidcendpoint.session.grant import get_usage_rules
from oidcendpoint.user_authn.authn_context import INTERNETPROTOCOLPASSWORD
from oidcendpoint.user_info import UserInfo

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

TOKEN_REQ_DICT = TOKEN_REQ.to_dict()

BASEDIR = os.path.abspath(os.path.dirname(__file__))


def full_path(local_file):
    return os.path.join(BASEDIR, local_file)


USERINFO = UserInfo(json.loads(open(full_path("users.json")).read()))

ENDPOINT_CONTEXT_CONFIG = {
    "issuer": "https://example.com/",
    "password": "mycket hemligt",
    "verify_ssl": False,
    "capabilities": CAPABILITIES,
    "keys": {"uri_path": "jwks.json", "key_defs": KEYDEFS},
    "id_token": {"class": IDToken, "kwargs": {}},
    "endpoint": {
        "provider_config": {
            "path": ".well-known/openid-configuration",
            "class": ProviderConfiguration,
            "kwargs": {},
        },
        "registration": {"path": "registration", "class": Registration, "kwargs": {}, },
        "authorization": {
            "path": "authorization",
            "class": Authorization,
            "kwargs": {},
        },
        "token": {
            "path": "token",
            "class": Token,
            "kwargs": {
                "client_authn_methods": [
                    "client_secret_post",
                    "client_secret_basic",
                    "client_secret_jwt",
                    "private_key_jwt",
                ]
            },
        },
        "userinfo": {
            "path": "userinfo",
            "class": userinfo.UserInfo,
            "kwargs": {
                "claim_types_supported": ["normal", "aggregated", "distributed", ],
                "client_authn_method": ["bearer_header"],
                "add_claims_by_scope": True
            },
        },
    },
    "userinfo": {
        "class": user_info.UserInfo,
        "kwargs": {"db_file": full_path("users.json")},
    },
    # "client_authn": verify_client,
    "authentication": {
        "anon": {
            "acr": INTERNETPROTOCOLPASSWORD,
            "class": "oidcendpoint.user_authn.user.NoAuthn",
            "kwargs": {"user": "diana"},
        }
    },
    "template_dir": "template",
    "add_on": {
        "custom_scopes": {
            "function": "oidcendpoint.oidc.add_on.custom_scopes.add_custom_scopes",
            "kwargs": {
                "research_and_scholarship": [
                    "name",
                    "given_name",
                    "family_name",
                    "email",
                    "email_verified",
                    "sub",
                    "eduperson_scoped_affiliation",
                ]
            },
        }
    },
    "db_conf": {
        "keyjar": {
            "handler": "oidcmsg.storage.abfile.LabeledAbstractFileSystem",
            "fdir": "db/keyjar",
            "key_conv": "oidcmsg.storage.converter.QPKey",
            "value_conv": "cryptojwt.serialize.item.KeyIssuer",
            "label": "keyjar",
        },
        "default": {
            "handler": "oidcmsg.storage.abfile.AbstractFileSystem",
            "fdir": "db",
            "key_conv": "oidcmsg.storage.converter.QPKey",
            "value_conv": "oidcmsg.storage.converter.JSON",
        },
        "session": {
            "handler": "oidcmsg.storage.abfile.AbstractFileSystem",
            "fdir": "db/session",
            "key_conv": "oidcmsg.storage.converter.QPKey",
            "value_conv": "oidcendpoint.session.storage.JSON",
        },
        "client": {
            "handler": "oidcmsg.storage.abfile.AbstractFileSystem",
            "fdir": "db/client",
            "key_conv": "oidcmsg.storage.converter.QPKey",
            "value_conv": "oidcmsg.storage.converter.JSON",
        },
        "registration_access_token": {
            "handler": "oidcmsg.storage.abfile.AbstractFileSystem",
            "fdir": "db/rat",
            "key_conv": "oidcmsg.storage.converter.QPKey",
            "value_conv": "oidcmsg.storage.converter.JSON",
        },
        "jti": {
            "handler": "oidcmsg.storage.abfile.AbstractFileSystem",
            "fdir": "db/jti",
            "key_conv": "oidcmsg.storage.converter.QPKey",
            "value_conv": "oidcmsg.storage.converter.JSON",
        },
    },
    "authz": {
        "class": AuthzHandling,
        "kwargs": {
            "grant_config": {
                "usage_rules": {
                    "authorization_code": {
                        'supports_minting': ["access_token", "refresh_token", "id_token"],
                        "max_usage": 1
                    },
                    "access_token": {},
                    "refresh_token": {
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
    def create_endpoint(self):
        try:
            shutil.rmtree("db")
        except FileNotFoundError:
            pass

        endpoint_context1 = EndpointContext(ENDPOINT_CONTEXT_CONFIG)
        endpoint_context1.cdb["client_1"] = {
            "client_secret": "hemligt",
            "redirect_uris": [("https://example.com/cb", None)],
            "client_salt": "salted",
            "token_endpoint_auth_method": "client_secret_post",
            "response_types": ["code", "token", "code id_token", "id_token"],
        }
        self.endpoint = {1: endpoint_context1.endpoint["userinfo"]}
        endpoint_context2 = EndpointContext(ENDPOINT_CONTEXT_CONFIG)
        self.endpoint[2] = endpoint_context2.endpoint["userinfo"]
        self.session_manager = {
            1: endpoint_context1.session_manager,
            2: endpoint_context2.session_manager
        }
        self.user_id = "diana"

    def _create_session(self, auth_req, sub_type="public", sector_identifier='', index=1):
        if sector_identifier:
            authz_req = auth_req.copy()
            authz_req["sector_identifier_uri"] = sector_identifier
        else:
            authz_req = auth_req
        client_id = authz_req['client_id']
        ae = create_authn_event(self.user_id)
        return self.session_manager[index].create_session(ae, authz_req, self.user_id,
                                                          client_id=client_id,
                                                          sub_type=sub_type)

    def _mint_code(self, grant, session_id, index=1):
        # Constructing an authorization code is now done
        _code = grant.mint_token(
            session_id,
            endpoint_context=self.endpoint[index].endpoint_context,
            token_type='authorization_code',
            token_handler=self.session_manager[index].token_handler["code"]
        )

        # if _exp_in:
        #     if isinstance(_exp_in, str):
        #         _exp_in = int(_exp_in)
        #     if _exp_in:
        #         _code.expires_at = utc_time_sans_frac() + _exp_in

        self.session_manager[index].set(unpack_session_key(session_id), grant)

        return _code

    def _mint_access_token(self, grant, session_id, token_ref=None, index=1):
        _session_info = self.session_manager[index].get_session_info(
            session_id, client_session_info=True)

        _token = grant.mint_token(
            session_id=session_id,
            endpoint_context=self.endpoint[index].endpoint_context,
            token_type='access_token',
            token_handler=self.session_manager[index].token_handler["access_token"],
            based_on=token_ref  # Means the token (tok) was used to mint this token
        )

        self.session_manager[index].set([self.user_id, _session_info["client_id"], grant.id],
                                        grant)

        return _token

    def test_init(self):
        assert self.endpoint[1]
        assert set(
            self.endpoint[1].endpoint_context.provider_info["claims_supported"]
        ) == {
                   "address",
                   "birthdate",
                   "email",
                   "email_verified",
                   "eduperson_scoped_affiliation",
                   "family_name",
                   "gender",
                   "given_name",
                   "locale",
                   "middle_name",
                   "name",
                   "nickname",
                   "phone_number",
                   "phone_number_verified",
                   "picture",
                   "preferred_username",
                   "profile",
                   "sub",
                   "updated_at",
                   "website",
                   "zoneinfo",
               }
        assert set(
            self.endpoint[1].endpoint_context.provider_info["claims_supported"]
        ) == set(self.endpoint[2].endpoint_context.provider_info["claims_supported"])

    def test_parse(self):
        session_id = self._create_session(AUTH_REQ, index=1)
        grant = self.endpoint[1].endpoint_context.authz(session_id, AUTH_REQ)
        # grant, session_id = self._do_grant(AUTH_REQ, index=1)
        code = self._mint_code(grant, session_id, index=1)
        access_token = self._mint_access_token(grant, session_id, code, 1)

        # switch to another endpoint context instance
        _req = self.endpoint[2].parse_request(
            {}, auth="Bearer {}".format(access_token.value)
        )

        assert set(_req.keys()) == {"client_id", "access_token"}

    def test_process_request(self):
        session_id = self._create_session(AUTH_REQ, index=1)
        grant = self.endpoint[1].endpoint_context.authz(session_id, AUTH_REQ)
        code = self._mint_code(grant, session_id, index=1)
        access_token = self._mint_access_token(grant, session_id, code, 1)

        _req = self.endpoint[2].parse_request(
            {}, auth="Bearer {}".format(access_token.value)
        )
        args = self.endpoint[2].process_request(_req)
        assert args

    def test_process_request_not_allowed(self):
        session_id = self._create_session(AUTH_REQ, index=2)
        grant = self.endpoint[2].endpoint_context.authz(session_id, AUTH_REQ)
        code = self._mint_code(grant, session_id, index=2)
        access_token = self._mint_access_token(grant, session_id, code, 2)

        access_token.expires_at = utc_time_sans_frac() - 60
        self.session_manager[2].set([self.user_id, AUTH_REQ["client_id"], grant.id], grant)

        _req = self.endpoint[1].parse_request({}, auth="Bearer {}".format(access_token.value))

        args = self.endpoint[1].process_request(_req)
        assert set(args.keys()) == {"error", "error_description"}
        assert args["error"] == "invalid_token"

    # Don't test for offline_access right now. Should be expressed in supports_minting
    # def test_process_request_offline_access(self):
    #     auth_req = AUTH_REQ.copy()
    #     auth_req["scope"] = ["openid", "offline_access"]
    #     self._create_session(auth_req, index=2)
    #     grant, session_id = self._do_grant(auth_req, index=2)
    #     code = self._mint_code(grant, auth_req["client_id"], index=2)
    #     access_token = self._mint_access_token(grant, session_id, code, 2)
    #
    #     _req = self.endpoint[1].parse_request(
    #         {}, auth="Bearer {}".format(access_token.value)
    #     )
    #     args = self.endpoint[1].process_request(_req)
    #     assert set(args["response_args"].keys()) == {"sub"}

    def test_do_response(self):
        session_id = self._create_session(AUTH_REQ, index=2)
        grant = self.endpoint[2].endpoint_context.authz(session_id, AUTH_REQ)
        code = self._mint_code(grant, session_id, index=2)
        access_token = self._mint_access_token(grant, session_id, code, 2)

        _req = self.endpoint[1].parse_request(
            {}, auth="Bearer {}".format(access_token.value)
        )
        args = self.endpoint[1].process_request(_req)
        assert args
        res = self.endpoint[2].do_response(request=_req, **args)
        assert res

    def test_do_signed_response(self):
        self.endpoint[2].endpoint_context.cdb["client_1"][
            "userinfo_signed_response_alg"
        ] = "ES256"

        session_id = self._create_session(AUTH_REQ, index=2)
        grant = self.endpoint[2].endpoint_context.authz(session_id, AUTH_REQ)
        code = self._mint_code(grant, session_id, index=2)
        access_token = self._mint_access_token(grant, session_id, code, 2)

        _req = self.endpoint[1].parse_request(
            {}, auth="Bearer {}".format(access_token.value)
        )
        args = self.endpoint[1].process_request(_req)
        assert args
        res = self.endpoint[1].do_response(request=_req, **args)
        assert res

    def test_custom_scope(self):
        _auth_req = AUTH_REQ.copy()
        _auth_req["scope"] = ["openid", "research_and_scholarship"]

        session_id = self._create_session(AUTH_REQ, index=2)
        grant = self.endpoint[2].endpoint_context.authz(session_id, AUTH_REQ)

        grant.claims = {
            "userinfo": self.endpoint[1].endpoint_context.claims_interface.get_claims(
                session_id, scopes=_auth_req["scope"], usage="userinfo")
        }
        self.session_manager[2].set(unpack_session_key(session_id), grant)

        code = self._mint_code(grant, session_id, index=2)
        access_token = self._mint_access_token(grant, session_id, code, 2)

        _req = self.endpoint[1].parse_request(
            {}, auth="Bearer {}".format(access_token.value)
        )
        args = self.endpoint[1].process_request(_req)
        assert set(args["response_args"].keys()) == {
            "sub",
            "name",
            "given_name",
            "family_name",
            "email",
            "email_verified",
            "eduperson_scoped_affiliation",
        }
