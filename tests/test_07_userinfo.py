import json
import os

import pytest
from oidcmsg.oidc import OpenIDRequest

from oidcendpoint.authn_event import create_authn_event
from oidcendpoint.endpoint_context import EndpointContext
from oidcendpoint.id_token import IDToken
from oidcendpoint.oidc import userinfo
from oidcendpoint.oidc.authorization import Authorization
from oidcendpoint.oidc.provider_config import ProviderConfiguration
from oidcendpoint.oidc.registration import Registration
from oidcendpoint.scopes import SCOPE2CLAIMS
from oidcendpoint.scopes import convert_scopes2claims
from oidcendpoint.session import session_key
from oidcendpoint.session import unpack_session_key
from oidcendpoint.session.claims import STANDARD_CLAIMS
from oidcendpoint.session.claims import ClaimsInterface
from oidcendpoint.session.grant import Grant
from oidcendpoint.user_authn.authn_context import INTERNETPROTOCOLPASSWORD
from oidcendpoint.user_info import UserInfo

CLAIMS = {
    "userinfo": {
        "given_name": {"essential": True},
        "nickname": None,
        "email": {"essential": True},
        "email_verified": {"essential": True},
        "picture": None,
        "http://example.info/claims/groups": {"value": "red"},
    },
    "id_token": {
        "auth_time": {"essential": True},
        "acr": {"values": ["urn:mace:incommon:iap:silver"]},
    },
}

CLAIMS_2 = {
    "userinfo": {
        "eduperson_scoped_affiliation": {"essential": True},
        "nickname": None,
        "email": {"essential": True},
        "email_verified": {"essential": True},
    }
}

OIDR = OpenIDRequest(
    response_type="code",
    client_id="client1",
    redirect_uri="http://example.com/authz",
    scope=["openid"],
    state="state000",
    claims=CLAIMS,
)

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

BASEDIR = os.path.abspath(os.path.dirname(__file__))


def full_path(local_file):
    return os.path.join(BASEDIR, local_file)


USERINFO_DB = json.loads(open(full_path("users.json")).read())


def test_default_scope2claims():
    assert convert_scopes2claims(["openid"], STANDARD_CLAIMS) == {"sub": None}
    assert set(convert_scopes2claims(["profile"], STANDARD_CLAIMS).keys()) == {
        "name",
        "given_name",
        "family_name",
        "middle_name",
        "nickname",
        "profile",
        "picture",
        "website",
        "gender",
        "birthdate",
        "zoneinfo",
        "locale",
        "updated_at",
        "preferred_username",
    }
    assert set(convert_scopes2claims(["email"], STANDARD_CLAIMS).keys()) == {
        "email",
        "email_verified",
    }
    assert set(convert_scopes2claims(["address"], STANDARD_CLAIMS).keys()) == {
        "address"
    }
    assert set(convert_scopes2claims(["phone"], STANDARD_CLAIMS).keys()) == {
        "phone_number",
        "phone_number_verified",
    }
    assert convert_scopes2claims(["offline_access"], STANDARD_CLAIMS) == {}

    assert convert_scopes2claims(["openid", "email", "phone"], STANDARD_CLAIMS) == {
        "sub": None,
        "email": None,
        "email_verified": None,
        "phone_number": None,
        "phone_number_verified": None,
    }


def test_custom_scopes():
    custom_scopes = {
        "research_and_scholarship": [
            "name",
            "given_name",
            "family_name",
            "email",
            "email_verified",
            "sub",
            "iss",
            "eduperson_scoped_affiliation",
        ]
    }

    _scopes = SCOPE2CLAIMS.copy()
    _scopes.update(custom_scopes)
    _available_claims = STANDARD_CLAIMS[:]
    _available_claims.append("eduperson_scoped_affiliation")

    assert set(
        convert_scopes2claims(["email"], _available_claims, map=_scopes).keys()
    ) == {"email", "email_verified", }
    assert set(
        convert_scopes2claims(["address"], _available_claims, map=_scopes).keys()
    ) == {"address"}
    assert set(
        convert_scopes2claims(["phone"], _available_claims, map=_scopes).keys()
    ) == {"phone_number", "phone_number_verified", }

    assert set(
        convert_scopes2claims(
            ["research_and_scholarship"], _available_claims, map=_scopes
        ).keys()
    ) == {
               "name",
               "given_name",
               "family_name",
               "email",
               "email_verified",
               "sub",
               "eduperson_scoped_affiliation",
           }


PROVIDER_INFO = {
    "claims_supported": [
        "auth_time",
        "acr",
        "given_name",
        "nickname",
        "email",
        "email_verified",
        "picture",
        "http://example.info/claims/groups",
    ]
}

KEYDEFS = [
    {"type": "RSA", "key": "", "use": ["sig"]},
    {"type": "EC", "crv": "P-256", "use": ["sig"]},
]


class TestCollectUserInfo:
    @pytest.fixture(autouse=True)
    def create_endpoint_context(self):
        self.endpoint_context = EndpointContext(
            {
                "userinfo": {"class": UserInfo, "kwargs": {"db": USERINFO_DB}},
                "password": "we didn't start the fire",
                "issuer": "https://example.com/op",
                "token_expires_in": 900,
                "grant_expires_in": 600,
                "refresh_token_expires_in": 86400,
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
                        "kwargs": {
                            "response_types_supported": [
                                " ".join(x) for x in RESPONSE_TYPES_SUPPORTED
                            ],
                            "response_modes_supported": [
                                "query",
                                "fragment",
                                "form_post",
                            ],
                            "claims_parameter_supported": True,
                            "request_parameter_supported": True,
                            "request_uri_parameter_supported": True,
                        },
                    },
                    "userinfo": {
                        "path": "userinfo",
                        "class": userinfo.UserInfo,
                        "kwargs": {
                            "claim_types_supported": [
                                "normal",
                                "aggregated",
                                "distributed",
                            ],
                            "client_authn_method": ["bearer_header"],
                            "base_claims": {
                                "eduperson_scoped_affiliation": None,
                                "email": None,
                            },
                            "add_claims_by_scope": True,
                            "enable_claims_per_client": True
                        },
                    },
                },
                "keys": {
                    "public_path": "jwks.json",
                    "key_defs": KEYDEFS,
                    "uri_path": "static/jwks.json",
                },
                "authentication": {
                    "anon": {
                        "acr": INTERNETPROTOCOLPASSWORD,
                        "class": "oidcendpoint.user_authn.user.NoAuthn",
                        "kwargs": {"user": "diana"},
                    }
                },
                "template_dir": "template",
                "id_token": {
                    "class": IDToken,
                    "kwargs": {
                        "base_claims": {
                            "email": None,
                            "email_verified": None,
                        },
                        "enable_claims_per_client": True
                    },
                },
            }
        )
        # Just has to be there
        self.endpoint_context.cdb["client1"] = {}
        self.session_manager = self.endpoint_context.session_manager
        self.claims_interface = ClaimsInterface(self.endpoint_context)
        self.user_id = "diana"

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

    def test_collect_user_info(self):
        _req = OIDR.copy()
        _req["claims"] = CLAIMS_2

        session_id = self._create_session(_req)

        _userinfo_restriction = self.claims_interface.get_claims(session_id=session_id,
                                                                 scopes=OIDR["scope"],
                                                                 usage="userinfo")

        res = self.claims_interface.get_user_claims("diana", _userinfo_restriction)

        assert res == {
            'eduperson_scoped_affiliation': ['staff@example.org'],
            "email": "diana@example.org",
            "nickname": "Dina",
            "email_verified": False
        }

        _id_token_restriction = self.claims_interface.get_claims(session_id=session_id,
                                                                 scopes=OIDR["scope"],
                                                                 usage="id_token")

        res = self.claims_interface.get_user_claims("diana", _id_token_restriction)

        assert res == {
            "email": "diana@example.org",
            "email_verified": False,
        }

        _introspection_restriction = self.claims_interface.get_claims(session_id=session_id,
                                                                      scopes=OIDR["scope"],
                                                                      usage="introspection")

        res = self.claims_interface.get_user_claims("diana", _introspection_restriction)

        assert res == {}

    def test_collect_user_info_2(self):
        _req = OIDR.copy()
        _req["scope"] = "openid email address"
        del _req["claims"]

        session_id = self._create_session(_req)
        _uid, _cid, _gid = unpack_session_key(session_id)

        _userinfo_restriction = self.claims_interface.get_claims(session_id=session_id,
                                                                 scopes=_req["scope"],
                                                                 usage="userinfo")

        res = self.claims_interface.get_user_claims("diana", _userinfo_restriction)

        assert res == {
            'address': {
                'country': 'Sweden',
                'locality': 'Umeå',
                'postal_code': 'SE-90187',
                'street_address': 'Umeå Universitet'
            },
            'eduperson_scoped_affiliation': ['staff@example.org'],
            'email': 'diana@example.org',
            'email_verified': False
        }

    def test_collect_user_info_scope_not_supported_no_base_claims(self):
        _req = OIDR.copy()
        _req["scope"] = "openid email address"
        del _req["claims"]

        session_id = self._create_session(_req)
        _uid, _cid, _gid = unpack_session_key(session_id)

        self.endpoint_context.endpoint["userinfo"].kwargs["add_claims_by_scope"] = False
        self.endpoint_context.endpoint["userinfo"].kwargs["enable_claims_per_client"] = False
        del self.endpoint_context.endpoint["userinfo"].kwargs["base_claims"]

        _userinfo_restriction = self.claims_interface.get_claims(session_id=session_id,
                                                                 scopes=_req["scope"],
                                                                 usage="userinfo")

        res = self.claims_interface.get_user_claims("diana", _userinfo_restriction)

        assert res == {}

    def test_collect_user_info_enable_claims_per_client(self):
        _req = OIDR.copy()
        _req["scope"] = "openid email address"
        del _req["claims"]

        session_id = self._create_session(_req)
        _uid, _cid, _gid = unpack_session_key(session_id)

        self.endpoint_context.endpoint["userinfo"].kwargs["add_claims_by_scope"] = False
        self.endpoint_context.endpoint["userinfo"].kwargs["enable_claims_per_client"] = True
        del self.endpoint_context.endpoint["userinfo"].kwargs["base_claims"]

        self.endpoint_context.cdb[_req["client_id"]]["userinfo_claims"] = {"phone_number": None}

        _userinfo_restriction = self.claims_interface.get_claims(session_id=session_id,
                                                                 scopes=_req["scope"],
                                                                 usage="userinfo")

        res = self.claims_interface.get_user_claims("diana", _userinfo_restriction)

        assert res == {'phone_number': '+46907865000'}


class TestCollectUserInfoCustomScopes:
    @pytest.fixture(autouse=True)
    def create_endpoint_context(self):
        self.endpoint_context = EndpointContext(
            {
                "userinfo": {"class": UserInfo, "kwargs": {"db": USERINFO_DB}},
                "password": "we didn't start the fire",
                "issuer": "https://example.com/op",
                "token_expires_in": 900,
                "grant_expires_in": 600,
                "refresh_token_expires_in": 86400,
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
                        "kwargs": {
                            "response_types_supported": [
                                " ".join(x) for x in RESPONSE_TYPES_SUPPORTED
                            ],
                            "response_modes_supported": [
                                "query",
                                "fragment",
                                "form_post",
                            ],
                            "claims_parameter_supported": True,
                            "request_parameter_supported": True,
                            "request_uri_parameter_supported": True,
                        },
                    },
                    "userinfo": {
                        "path": "userinfo",
                        "class": userinfo.UserInfo,
                        "kwargs": {
                            "claim_types_supported": [
                                "normal",
                                "aggregated",
                                "distributed",
                            ],
                            "client_authn_method": ["bearer_header"],
                            "base_claims": {
                                "eduperson_scoped_affiliation": None,
                                "email": None,
                            },
                            "add_claims_by_scope": True,
                            "enable_claims_per_client": True
                        },
                    },
                },
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
                                "iss",
                                "eduperson_scoped_affiliation",
                            ]
                        },
                    }
                },
                "keys": {
                    "public_path": "jwks.json",
                    "key_defs": KEYDEFS,
                    "uri_path": "static/jwks.json",
                },
                "authentication": {
                    "anon": {
                        "acr": INTERNETPROTOCOLPASSWORD,
                        "class": "oidcendpoint.user_authn.user.NoAuthn",
                        "kwargs": {"user": "diana"},
                    }
                },
                "template_dir": "template",
                "id_token": {
                    "class": IDToken,
                    "kwargs": {
                        "base_claims": {
                            "email": None,
                            "email_verified": None,
                        },
                        "enable_claims_per_client": True
                    },
                },
            }
        )
        self.endpoint_context.cdb["client1"] = {}
        self.session_manager = self.endpoint_context.session_manager
        self.claims_interface = ClaimsInterface(self.endpoint_context)
        self.user_id = "diana"

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

    # def _do_grant(self, auth_req):
    #     client_id = auth_req['client_id']
    #     # The user consent module produces a Grant instance
    #     grant = Grant(scope=auth_req['scope'], resources=[client_id])
    #
    #     # the grant is assigned to a session (user_id, client_id)
    #     self.session_manager.set([self.user_id, client_id, grant.id], grant)
    #     return session_key(self.user_id, client_id, grant.id)

    def test_collect_user_info_custom_scope(self):
        _req = OIDR.copy()
        _req["scope"] = "openid research_and_scholarship"
        del _req["claims"]

        session_id = self._create_session(_req)

        _restriction = self.claims_interface.get_claims(session_id=session_id,
                                                        scopes=_req["scope"],
                                                        usage="userinfo")

        res = self.claims_interface.get_user_claims("diana", _restriction)

        assert res == {
            'eduperson_scoped_affiliation': ['staff@example.org'],
            'email': 'diana@example.org',
            'email_verified': False,
            'family_name': 'Krall',
            'given_name': 'Diana',
            'name': 'Diana Krall'
        }


class TestCollectUserInfoAllowedScopes:
    @pytest.fixture(autouse=True)
    def create_endpoint_context(self):
        self.endpoint_context = EndpointContext(
            {
                "userinfo": {"class": UserInfo, "kwargs": {"db": USERINFO_DB}},
                "password": "we didn't start the fire",
                "issuer": "https://example.com/op",
                "token_expires_in": 900,
                "grant_expires_in": 600,
                "refresh_token_expires_in": 86400,
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
                        "kwargs": {
                            "response_types_supported": [
                                " ".join(x) for x in RESPONSE_TYPES_SUPPORTED
                            ],
                            "response_modes_supported": [
                                "query",
                                "fragment",
                                "form_post",
                            ],
                            "claims_parameter_supported": True,
                            "request_parameter_supported": True,
                            "request_uri_parameter_supported": True,
                        },
                    },
                },
                "keys": {
                    "public_path": "jwks.json",
                    "key_defs": KEYDEFS,
                    "uri_path": "static/jwks.json",
                },
                "authentication": {
                    "anon": {
                        "acr": INTERNETPROTOCOLPASSWORD,
                        "class": "oidcendpoint.user_authn.user.NoAuthn",
                        "kwargs": {"user": "diana"},
                    }
                },
                "template_dir": "template",
            }
        )
        # Just has to be there
        self.endpoint_context.cdb["client1"] = {
            "allowed_scopes": ['openid', 'email', 'ciao']
        }

    def test_collect_user_info(self):
        _req = OIDR.copy()
        _req["claims"] = CLAIMS_2

        _session_info = {"authn_req": _req}
        session = _session_info.copy()
        session["sub"] = "doe"
        session["uid"] = "diana"
        session["authn_event"] = create_authn_event("diana", "salt")

        res = collect_user_info(self.endpoint_context, session)

        assert res == {
            "nickname": "Dina",
            "sub": "doe",
            "email": "diana@example.org",
            "email_verified": False,
        }

    def test_collect_user_info_2(self):
        _req = OIDR.copy()
        _req["scope"] = "openid email"
        del _req["claims"]

        _session_info = {"authn_req": _req}
        session = _session_info.copy()
        session["sub"] = "doe"
        session["uid"] = "diana"
        session["authn_event"] = create_authn_event("diana", "salt")

        self.endpoint_context.provider_info["scopes_supported"] = [
            "openid",
            "email",
            "offline_access",
        ]
        res = collect_user_info(self.endpoint_context, session)

        assert res == {
            "sub": "doe",
            "email": "diana@example.org",
            "email_verified": False,
        }

    def test_collect_user_info_scope_not_supported(self):
        _req = OIDR.copy()
        _req["scope"] = "openid email address"
        del _req["claims"]

        _session_info = {"authn_req": _req}
        session = _session_info.copy()
        session["sub"] = "doe"
        session["uid"] = "diana"
        session["authn_event"] = create_authn_event("diana", "salt")

        # Scope address generally supported
        self.endpoint_context.provider_info["scopes_supported"] = [
            "openid",
            "email",
            "address"
            "offline_access",
        ]

        # Scope address not supported for the specific client
        res = collect_user_info(self.endpoint_context, session)

        assert res == {
            "sub": "doe",
            "email": "diana@example.org",
            "email_verified": False,
        }
