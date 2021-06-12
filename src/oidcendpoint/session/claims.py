import logging
from typing import Optional
from typing import Union

from oidcmsg.oidc import OpenIDSchema

from oidcendpoint.scopes import convert_scopes2claims
from oidcendpoint.session import unpack_session_key

logger = logging.getLogger(__name__)

# USAGE = Literal["userinfo", "id_token", "introspection"]

IGNORE = ["error", "error_description", "error_uri", "_claim_names", "_claim_sources"]
STANDARD_CLAIMS = [c for c in OpenIDSchema.c_param.keys() if c not in IGNORE]


def available_claims(endpoint_context):
    _supported = endpoint_context.provider_info.get("claims_supported")
    if _supported:
        return _supported
    else:
        return STANDARD_CLAIMS


class ClaimsInterface:
    init_args = {
        "add_claims_by_scope": False,
        "enable_claims_per_client": False
    }

    def __init__(self, endpoint_context):
        self.endpoint_context = endpoint_context

    def authorization_request_claims(self, session_id: str, usage: Optional[str] = "") -> dict:
        if usage in ["id_token", "userinfo"]:
            _grant = self.endpoint_context.session_manager.get_grant(session_id)
            if "claims" in _grant.authorization_request:
                return _grant.authorization_request["claims"].get(usage, {})

        return {}

    def _get_client_claims(self, client_id, usage):
        client_info = self.endpoint_context.cdb.get(client_id, {})
        client_claims = client_info.get("{}_claims".format(usage), {})
        if isinstance(client_claims, list):
            client_claims = {k: None for k in client_claims}
        return client_claims

    def get_claims(self, session_id: str, scopes: str, usage: str) -> dict:
        """

        :param session_id: Session identifier
        :param scopes: Scopes
        :param usage: Where to use the claims. One of "userinfo"/"id_token"/"introspection"
        :return: Claims specification as a dictionary.
        """

        # which endpoint module configuration to get the base claims from
        module = None
        if usage == "userinfo":
            if "userinfo" in self.endpoint_context.endpoint:
                module = self.endpoint_context.endpoint["userinfo"]
        elif usage == "id_token":
            if self.endpoint_context.idtoken:
                module = self.endpoint_context.idtoken
        elif usage == "introspection":
            if "introspection" in self.endpoint_context.endpoint:
                module = self.endpoint_context.endpoint["introspection"]
        elif usage == "access_token":
            try:
                module = self.endpoint_context.session_manager.token_handler["access_token"]
            except KeyError:
                pass

        if module:
            base_claims = module.kwargs.get("base_claims", {})
        else:
            base_claims = {}

        user_id, client_id, grant_id = unpack_session_key(session_id)

        # Can there be per client specification of which claims to use.
        if module and module.kwargs.get("enable_claims_per_client"):
            claims = self._get_client_claims(client_id, usage)
        else:
            claims = {}

        claims.update(base_claims)

        # Scopes can in some cases equate to set of claims, is that used here ?
        if module and module.kwargs.get("add_claims_by_scope"):
            if scopes:
                _scopes = self.endpoint_context.scopes_handler.filter_scopes(
                    client_id, self.endpoint_context, scopes
                )

                _claims = convert_scopes2claims(
                    _scopes, map=self.endpoint_context.scope2claims
                )
                claims.update(_claims)

        # Bring in claims specification from the authorization request
        request_claims = self.authorization_request_claims(session_id=session_id,
                                                           usage=usage)

        # This will add claims that has not be added before and
        # set filters on those claims that also appears in one of the sources above
        if request_claims:
            claims.update(request_claims)

        return claims

    def get_claims_all_usage(self, session_id: str, scopes: str) -> dict:
        _claims = {}
        for usage in ["userinfo", "introspection", "id_token", "token"]:
            _claims.update(self.get_claims(session_id, scopes, usage))
        return _claims

    def get_user_claims(self, user_id: str, claims_restriction: dict) -> dict:
        """

        :param user_id: User identifier
        :param claims_restriction: Specifies the upper limit of which claims can be returned
        :return:
        """
        if claims_restriction:
            # Get all possible claims
            user_info = self.endpoint_context.userinfo(user_id, client_id=None)
            # Filter out the once that can be returned
            return {k: user_info.get(k) for k, v in claims_restriction.items() if
                    claims_match(user_info.get(k), v)}
        else:
            return {}


def claims_match(value: Union[str, int], claimspec: Optional[dict]) -> bool:
    """
    Implements matching according to section 5.5.1 of
    http://openid.net/specs/openid-connect-core-1_0.html
    The lack of value is not checked here.
    Also the text doesn't prohibit having both 'value' and 'values'.

    :param value: single value
    :param claimspec: None or dictionary with 'essential', 'value' or 'values'
        as key
    :return: Boolean
    """
    if value is None:
        return False

    if claimspec is None:  # match anything
        return True

    matched = False
    for key, val in claimspec.items():
        if key == "value":
            if value == val:
                matched = True
        elif key == "values":
            if value in val:
                matched = True
        elif key == "essential":
            # Whether it's essential or not doesn't change anything here
            continue

        if matched:
            break

    if matched is False:
        if list(claimspec.keys()) == ["essential"]:
            return True

    return matched


def by_schema(cls, **kwa):
    """
    Will return only those claims that are listed in the Class definition.

    :param cls: A subclass of :py:class:´oidcmsg.message.Message`
    :param kwa: Keyword arguments
    :return: A dictionary with claims (keys) that meets the filter criteria
    """
    return dict([(key, val) for key, val in kwa.items() if key in cls.c_param])
