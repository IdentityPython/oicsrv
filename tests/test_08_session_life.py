import os

import pytest
from cryptojwt.key_jar import init_key_jar
from oidcmsg.oidc import AccessTokenRequest
from oidcmsg.oidc import AuthorizationRequest
from oidcmsg.oidc import RefreshAccessTokenRequest
from oidcmsg.time_util import time_sans_frac

from oidcendpoint import user_info
from oidcendpoint.authn_event import create_authn_event
from oidcendpoint.client_authn import verify_client
from oidcendpoint.endpoint_context import EndpointContext
from oidcendpoint.id_token import IDToken
from oidcendpoint.oidc.authorization import Authorization
from oidcendpoint.oidc.provider_config import ProviderConfiguration
from oidcendpoint.oidc.registration import Registration
from oidcendpoint.oidc.session import Session
from oidcendpoint.oidc.token import Token
from oidcendpoint.session import session_key
from oidcendpoint.session import unpack_session_key
from oidcendpoint.session.grant import Grant
from oidcendpoint.session.info import ClientSessionInfo
from oidcendpoint.session.info import UserSessionInfo
from oidcendpoint.session.manager import SessionManager
from oidcendpoint.session.manager import public_id
from oidcendpoint.token import DefaultToken
from oidcendpoint.token.handler import TokenHandler
from oidcendpoint.token.handler import factory
from oidcendpoint.user_authn.authn_context import INTERNETPROTOCOLPASSWORD


class TestSession():
    @pytest.fixture(autouse=True)
    def setup_token_handler(self):
        password = "The longer the better. Is this close to enough ?"
        conf = {
            "issuer": "https://example.com/",
            "password": "mycket hemligt",
            "token_expires_in": 600,
            "grant_expires_in": 300,
            "refresh_token_expires_in": 86400,
            "verify_ssl": False,
            "keys": {"key_defs": KEYDEFS, "uri_path": "static/jwks.json"},
            "jwks_uri": "https://example.com/jwks.json",
            "token_handler_args": {
                "code": {
                    "kwargs": {
                        "lifetime": 600,
                        "password": password
                    }},
                "token": {
                    "kwargs": {
                        "lifetime": 900,
                        "password": password
                    }
                },
                "refresh": {
                    "kwargs": {
                        "lifetime": 86400,
                        "password": password
                    }
                }
            },
            "endpoint": {
                "authorization_endpoint": {
                    "path": "{}/authorization",
                    "class": Authorization,
                    "kwargs": {},
                },
                "token_endpoint": {"path": "{}/token", "class": Token, "kwargs": {}},
            },
            "template_dir": "template",
        }

        self.endpoint_context = EndpointContext(conf)
        token_handler = factory(self.endpoint_context, **conf["token_handler_args"])

        self.session_manager = SessionManager(handler=token_handler)

    def auth(self):
        # Start with an authentication request
        # The client ID appears in the request
        AUTH_REQ = AuthorizationRequest(
            client_id="client_1",
            redirect_uri="https://example.com/cb",
            scope=["openid", "mail", "address", "offline_access"],
            state="STATE",
            response_type="code",
        )

        # The authentication returns a user ID
        user_id = "diana"

        # User info is stored in the Session DB
        authn_event = create_authn_event(
            user_id,
            authn_info=INTERNETPROTOCOLPASSWORD,
            authn_time=time_sans_frac(),
        )

        user_info = UserSessionInfo(user_id=user_id)
        self.session_manager.set([user_id], user_info)

        # Now for client session information
        client_id = AUTH_REQ['client_id']
        client_info = ClientSessionInfo(client_id=client_id)
        self.session_manager.set([user_id, client_id], client_info)

        # The user consent module produces a Grant instance

        grant = Grant(scope=AUTH_REQ['scope'],
                      resources=[client_id],
                      authorization_request=AUTH_REQ,
                      authentication_event=authn_event)

        # the grant is assigned to a session (user_id, client_id)
        session_id = session_key(user_id, client_id, grant.id)
        self.session_manager.set([user_id, client_id, grant.id], grant)

        # Constructing an authorization code is now done by

        code = grant.mint_token(
            session_id=session_id,
            endpoint_context=self.endpoint_context,
            token_type='authorization_code',
            token_handler= self.session_manager.token_handler["code"],
            expires_at=time_sans_frac() + 300  # 5 minutes from now
        )

        return grant.id, code

    def test_code_flow(self):
        # code is a Token instance
        _grant_id, code = self.auth()

        # next step is access token request

        TOKEN_REQ = AccessTokenRequest(
            client_id="client_1",
            redirect_uri="https://example.com/cb",
            state="STATE",
            grant_type="authorization_code",
            client_secret="hemligt",
            code=code.value
        )

        # parse the token
        session_id = self.session_manager.token_handler.sid(TOKEN_REQ['code'])

        # Now given I have the client_id from the request and the user_id from the
        # token I can easily find the grant

        # client_info = self.session_manager.get([user_id, TOKEN_REQ['client_id']])
        tok = self.session_manager.find_token(session_id, TOKEN_REQ['code'])

        # Verify that it's of the correct type and can be used
        assert tok.type == "authorization_code"
        assert tok.is_active()

        # Mint an access token and a refresh token and mark the code as used

        assert tok.supports_minting("access_token")

        grant = self.session_manager[session_id]

        access_token = grant.mint_token(
            session_id=session_id,
            endpoint_context=self.endpoint_context,
            token_type='access_token',
            token_handler=self.session_manager.token_handler["access_token"],
            expires_at=time_sans_frac() + 900,  # 15 minutes from now
            based_on=tok  # Means the token (tok) was used to mint this token
        )

        assert tok.supports_minting("refresh_token")

        refresh_token = grant.mint_token(
            session_id=session_id,
            endpoint_context=self.endpoint_context,
            token_type='refresh_token',
            token_handler=self.session_manager.token_handler["refresh_token"],
            based_on=tok
        )

        tok.register_usage()

        assert tok.max_usage_reached() is True

        # A bit later a refresh token is used to mint a new access token

        REFRESH_TOKEN_REQ = RefreshAccessTokenRequest(
            grant_type="refresh_token",
            client_id="client_1",
            client_secret="hemligt",
            refresh_token=refresh_token.value,
            scope=["openid", "mail", "offline_access"]
        )

        reftok = self.session_manager.find_token(session_id,
                                                 REFRESH_TOKEN_REQ['refresh_token'])

        assert reftok.supports_minting("access_token")

        access_token_2 = grant.mint_token(
            session_id=session_id,
            endpoint_context=self.endpoint_context,
            token_type='access_token',
            token_handler=self.session_manager.token_handler["access_token"],
            expires_at=time_sans_frac() + 900,  # 15 minutes from now
            based_on=reftok  # Means the token (tok) was used to mint this token
        )

        assert access_token_2.is_active()


KEYDEFS = [
    {"type": "RSA", "key": "", "use": ["sig"]},
    {"type": "EC", "crv": "P-256", "use": ["sig"]},
]

ISSUER = "https://example.com/"

KEYJAR = init_key_jar(key_defs=KEYDEFS, issuer_id=ISSUER)
KEYJAR.import_jwks(KEYJAR.export_jwks(True, ISSUER), "")
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
    "subject_types_supported": ["public", "pairwise", "ephemeral"],
    "grant_types_supported": [
        "authorization_code",
        "implicit",
        "urn:ietf:params:oauth:grant-type:jwt-bearer",
    ],
    "claim_types_supported": ["normal", "aggregated", "distributed"],
    "claims_parameter_supported": True,
    "request_parameter_supported": True,
    "request_uri_parameter_supported": True,
}
BASEDIR = os.path.abspath(os.path.dirname(__file__))


def full_path(local_file):
    return os.path.join(BASEDIR, local_file)


class TestSessionJWTToken():
    @pytest.fixture(autouse=True)
    def setup_session_manager(self):
        conf = {
            "issuer": ISSUER,
            "password": "mycket hemligt",
            "token_expires_in": 600,
            "grant_expires_in": 300,
            "refresh_token_expires_in": 86400,
            "verify_ssl": False,
            "capabilities": CAPABILITIES,
            "keys": {"uri_path": "jwks.json", "key_defs": KEYDEFS},
            "token_handler_args": {
                "jwks_def": {
                    "private_path": "private/token_jwks.json",
                    "read_only": False,
                    "key_defs": [
                        {"type": "oct", "bytes": "24", "use": ["enc"], "kid": "code"},
                        {"type": "oct", "bytes": "24", "use": ["enc"], "kid": "refresh"}
                    ],
                },
                "code": {"lifetime": 600},
                "token": {
                    "class": "oidcendpoint.token.jwt_token.JWTToken",
                    "kwargs": {
                        "lifetime": 3600,
                        "add_claims": [
                            "email",
                            "email_verified",
                            "phone_number",
                            "phone_number_verified",
                        ],
                        "add_claim_by_scope": True,
                        "aud": ["https://example.org/appl"],
                    },
                },
                "refresh": {},
            },
            "endpoint": {
                "provider_config": {
                    "path": "{}/.well-known/openid-configuration",
                    "class": ProviderConfiguration,
                    "kwargs": {},
                },
                "registration": {
                    "path": "{}/registration",
                    "class": Registration,
                    "kwargs": {},
                },
                "authorization": {
                    "path": "{}/authorization",
                    "class": Authorization,
                    "kwargs": {},
                },
                "token": {"path": "{}/token", "class": Token, "kwargs": {}},
                "session": {"path": "{}/end_session", "class": Session},
            },
            "client_authn": verify_client,
            "authentication": {
                "anon": {
                    "acr": INTERNETPROTOCOLPASSWORD,
                    "class": "oidcendpoint.user_authn.user.NoAuthn",
                    "kwargs": {"user": "diana"},
                }
            },
            "template_dir": "template",
            "userinfo": {
                "class": user_info.UserInfo,
                "kwargs": {"db_file": full_path("users.json")},
            },
            "id_token": {"class": IDToken},
        }

        self.endpoint_context = EndpointContext(conf, keyjar=KEYJAR)
        self.session_manager = self.endpoint_context.session_manager
        # self.session_manager = SessionManager(handler=self.endpoint_context.sdb.handler)
        # self.endpoint_context.session_manager = self.session_manager

    def auth(self):
        # Start with an authentication request
        # The client ID appears in the request
        AUTH_REQ = AuthorizationRequest(
            client_id="client_1",
            redirect_uri="https://example.com/cb",
            scope=["openid", "mail", "address", "offline_access"],
            state="STATE",
            response_type="code",
        )

        # The authentication returns a user ID
        user_id = "diana"

        # User info is stored in the Session DB

        user_info = UserSessionInfo(user_id=user_id)
        self.session_manager.set([user_id], user_info)

        # Now for client session information
        authn_event = create_authn_event(
            user_id,
            authn_info=INTERNETPROTOCOLPASSWORD,
            authn_time=time_sans_frac(),
        )

        client_id = AUTH_REQ["client_id"]
        client_info = ClientSessionInfo(client_id=client_id)
        self.session_manager.set([user_id, client_id], client_info)

        # The user consent module produces a Grant instance

        grant = Grant(
            scope=AUTH_REQ['scope'],
            resources=[client_id],
            authentication_event=authn_event,
            authorization_request=AUTH_REQ
        )

        # the grant is assigned to a session (user_id, client_id)
        session_id = session_key(user_id, client_id, grant.id)
        self.session_manager.set([user_id, client_id, grant.id], grant)

        # Constructing an authorization code is now done by

        code = grant.mint_token(
            session_id=session_id,
            endpoint_context=self.endpoint_context,
            token_type='authorization_code',
            token_handler=self.session_manager.token_handler["code"],
            expires_at=time_sans_frac() + 300  # 5 minutes from now
        )
        return code

    def test_code_flow(self):
        # code is a Token instance
        code = self.auth()

        # next step is access token request

        TOKEN_REQ = AccessTokenRequest(
            client_id="client_1",
            redirect_uri="https://example.com/cb",
            state="STATE",
            grant_type="authorization_code",
            client_secret="hemligt",
            code=code.value
        )

        # parse the token
        session_id = self.session_manager.token_handler.sid(TOKEN_REQ['code'])
        user_id, client_id, grant_id = unpack_session_key(session_id)

        # Now given I have the client_id from the request and the user_id from the
        # token I can easily find the grant

        # client_info = self.session_manager.get([user_id, TOKEN_REQ['client_id']])
        tok = self.session_manager.find_token(session_id, TOKEN_REQ['code'])

        # Verify that it's of the correct type and can be used
        assert tok.type == "authorization_code"
        assert tok.is_active()

        # Mint an access token and a refresh token and mark the code as used

        assert tok.supports_minting("access_token")

        client_info = self.session_manager.get([user_id, TOKEN_REQ["client_id"]])

        assert tok.supports_minting("access_token")

        grant = self.session_manager[session_id]

        access_token = grant.mint_token(
            session_id=session_id,
            endpoint_context=self.endpoint_context,
            token_type='access_token',
            token_handler=self.session_manager.token_handler["access_token"],
            expires_at=time_sans_frac() + 900,  # 15 minutes from now
            based_on=tok  # Means the token (tok) was used to mint this token
        )

        # this test is include in the mint_token methods
        # assert tok.supports_minting("refresh_token")

        refresh_token = grant.mint_token(
            session_id=session_id,
            endpoint_context=self.endpoint_context,
            token_type='refresh_token',
            token_handler=self.session_manager.token_handler["refresh_token"],
            based_on=tok
        )

        tok.register_usage()

        assert tok.max_usage_reached() is True

        # A bit later a refresh token is used to mint a new access token

        REFRESH_TOKEN_REQ = RefreshAccessTokenRequest(
            grant_type="refresh_token",
            client_id="client_1",
            client_secret="hemligt",
            refresh_token=refresh_token.value,
            scope=["openid", "mail", "offline_access"]
        )

        session_id = session_key(user_id, REFRESH_TOKEN_REQ['client_id'], grant_id)
        reftok = self.session_manager.find_token(session_id,
                                                 REFRESH_TOKEN_REQ['refresh_token'])

        # Can I use this token to mint another token ?
        assert grant.is_active()

        user_claims = self.endpoint_context.userinfo(user_id, client_id=TOKEN_REQ["client_id"],
                                                     user_info_claims=grant.claims)

        access_token_2 = grant.mint_token(
            session_id=session_id,
            endpoint_context=self.endpoint_context,
            token_type='access_token',
            token_handler=self.session_manager.token_handler["access_token"],
            expires_at=time_sans_frac() + 900,  # 15 minutes from now
            based_on=reftok  # Means the refresh token (reftok) was used to mint this token
        )

        assert access_token_2.is_active()

        token_info = self.session_manager.token_handler.info(access_token_2.value)
        assert token_info
