import json
import logging
from typing import Union
from urllib.parse import unquote
from urllib.parse import urlencode
from urllib.parse import urlparse

from cryptojwt import BadSyntax
from cryptojwt import as_unicode
from cryptojwt import b64d
from cryptojwt.jwe.exception import JWEException
from cryptojwt.jws.exception import NoSuitableSigningKeys
from cryptojwt.utils import as_bytes
from cryptojwt.utils import b64e
from oidcmsg import oauth2
from oidcmsg.exception import ParameterError
from oidcmsg.exception import URIError
from oidcmsg.message import Message
from oidcmsg.oidc import verified_claim_name
from oidcmsg.time_util import utc_time_sans_frac

from oidcendpoint import rndstr
from oidcendpoint.authn_event import create_authn_event
from oidcendpoint.cookie import append_cookie
from oidcendpoint.cookie import compute_session_state
from oidcendpoint.cookie import new_cookie
from oidcendpoint.endpoint import Endpoint
from oidcendpoint.exception import InvalidRequest
from oidcendpoint.exception import NoSuchAuthentication
from oidcendpoint.exception import RedirectURIError
from oidcendpoint.exception import ServiceError
from oidcendpoint.exception import TamperAllert
from oidcendpoint.exception import ToOld
from oidcendpoint.exception import UnAuthorizedClientScope
from oidcendpoint.exception import UnknownClient
from oidcendpoint.session import Revoked
from oidcendpoint.session import unpack_session_key
from oidcendpoint.token.exception import UnknownToken
from oidcendpoint.user_authn.authn_context import pick_auth
from oidcendpoint.util import split_uri

logger = logging.getLogger(__name__)

# For the time being. This is JAR specific and should probably be configurable.
ALG_PARAMS = {
    "sign": [
        "request_object_signing_alg",
        "request_object_signing_alg_values_supported",
    ],
    "enc_alg": [
        "request_object_encryption_alg",
        "request_object_encryption_alg_values_supported",
    ],
    "enc_enc": [
        "request_object_encryption_enc",
        "request_object_encryption_enc_values_supported",
    ],
}

FORM_POST = """<html>
  <head>
    <title>Submit This Form</title>
  </head>
  <body onload="javascript:document.forms[0].submit()">
    <form method="post" action="{action}">
        {inputs}
    </form>
  </body>
</html>"""


def inputs(form_args):
    """
    Creates list of input elements
    """
    element = []
    html_field = '<input type="hidden" name="{}" value="{}"/>'
    for name, value in form_args.items():
        element.append(html_field.format(name, value))
    return "\n".join(element)


def max_age(request):
    verified_request = verified_claim_name("request")
    return request.get(verified_request, {}).get("max_age") or request.get("max_age", 0)


def verify_uri(endpoint_context, request, uri_type, client_id=None):
    """
    A redirect URI
    MUST NOT contain a fragment
    MAY contain query component

    :param endpoint_context: An EndpointContext instance
    :param request: The authorization request
    :param uri_type: redirect_uri or post_logout_redirect_uri
    :return: An error response if the redirect URI is faulty otherwise
        None
    """
    _cid = request.get("client_id", client_id)

    if not _cid:
        logger.error("No client id found")
        raise UnknownClient("No client_id provided")
    else:
        logger.debug("Client ID: {}".format(_cid))

    _redirect_uri = unquote(request[uri_type])

    part = urlparse(_redirect_uri)
    if part.fragment:
        raise URIError("Contains fragment")

    (_base, _query) = split_uri(_redirect_uri)
    # if _query:
    #     _query = parse_qs(_query)

    match = False
    # Get the clients registered redirect uris
    client_info = endpoint_context.cdb.get(_cid, {})
    if not client_info:
        raise KeyError("No such client")
    logger.debug("Client info: {}".format(client_info))
    redirect_uris = client_info.get("{}s".format(uri_type))
    if not redirect_uris:
        if _cid not in endpoint_context.cdb:
            logger.debug("CIDs: {}".format(list(endpoint_context.cdb.keys())))
            raise KeyError("No such client")
        raise ValueError("No registered {}".format(uri_type))
    else:
        for regbase, rquery in redirect_uris:
            # The URI MUST exactly match one of the Redirection URI
            if _base == regbase:
                # every registered query component must exist in the uri
                if rquery:
                    if not _query:
                        raise ValueError("Missing query part")

                    for key, vals in rquery.items():
                        if key not in _query:
                            raise ValueError('"{}" not in query part'.format(key))

                        for val in vals:
                            if val not in _query[key]:
                                raise ValueError(
                                    "{}={} value not in query part".format(key, val)
                                )

                # and vice versa, every query component in the uri
                # must be registered
                if _query:
                    if not rquery:
                        raise ValueError("No registered query part")

                    for key, vals in _query.items():
                        if key not in rquery:
                            raise ValueError('"{}" extra in query part'.format(key))
                        for val in vals:
                            if val not in rquery[key]:
                                raise ValueError(
                                    "Extra {}={} value in query part".format(key, val)
                                )
                match = True
                break
        if not match:
            raise RedirectURIError("Doesn't match any registered uris")


def join_query(base, query):
    """

    :param base: URL base
    :param query: query part as a dictionary
    :return:
    """
    if query:
        return "{}?{}".format(base, urlencode(query, doseq=True))
    else:
        return base


def get_uri(endpoint_context, request, uri_type):
    """ verify that the redirect URI is reasonable.

    :param endpoint_context: An EndpointContext instance
    :param request: The Authorization request
    :param uri_type: 'redirect_uri' or 'post_logout_redirect_uri'
    :return: redirect_uri
    """
    uri = ""

    if uri_type in request:
        verify_uri(endpoint_context, request, uri_type)
        uri = request[uri_type]
    else:
        uris = "{}s".format(uri_type)
        client_id = str(request["client_id"])
        if client_id in endpoint_context.cdb:
            _specs = endpoint_context.cdb[client_id].get(uris)
            if not _specs:
                raise ParameterError("Missing {} and none registered".format(uri_type))

            if len(_specs) > 1:
                raise ParameterError(
                    "Missing {} and more than one registered".format(uri_type)
                )

            uri = join_query(*_specs[0])

    return uri


def authn_args_gather(request, authn_class_ref, cinfo, **kwargs):
    """
    Gather information to be used by the authentication method

    :param request: The request either as a dictionary or as a Message instance
    :param authn_class_ref: Authentication class reference
    :param cinfo: Client information
    :param kwargs: Extra keyword arguments
    :return: Authentication arguments
    """
    authn_args = {
        "authn_class_ref": authn_class_ref,
        "return_uri": request["redirect_uri"],
    }

    if isinstance(request, Message):
        authn_args["query"] = request.to_urlencoded()
    elif isinstance(request, dict):
        authn_args["query"] = urlencode(request)
    else:
        ValueError("Wrong request format")

    if "req_user" in kwargs:
        authn_args["as_user"] = (kwargs["req_user"],)

    # Below are OIDC specific. Just ignore if OAuth2
    if cinfo:
        for attr in ["policy_uri", "logo_uri", "tos_uri"]:
            if cinfo.get(attr):
                authn_args[attr] = cinfo[attr]

    for attr in ["ui_locales", "acr_values", "login_hint"]:
        if request.get(attr):
            authn_args[attr] = request[attr]

    return authn_args


def check_unknown_scopes_policy(request_info, cinfo, endpoint_context):
    op_capabilities = endpoint_context.conf['capabilities']
    client_allowed_scopes = cinfo.get('allowed_scopes') or \
                            op_capabilities['scopes_supported']

    # this prevents that authz would be released for unavailable scopes
    for scope in request_info['scope']:
        if op_capabilities.get('deny_unknown_scopes') and \
                scope not in client_allowed_scopes:
            _msg = '{} requested an unauthorized scope ({})'
            logger.warning(_msg.format(cinfo['client_id'],
                                       scope))
            raise UnAuthorizedClientScope()


class Authorization(Endpoint):
    request_cls = oauth2.AuthorizationRequest
    response_cls = oauth2.AuthorizationResponse
    error_cls = oauth2.AuthorizationErrorResponse
    request_format = "urlencoded"
    response_format = "urlencoded"
    response_placement = "url"
    endpoint_name = "authorization_endpoint"
    name = "authorization"
    default_capabilities = {
        "claims_parameter_supported": True,
        "request_parameter_supported": True,
        "request_uri_parameter_supported": True,
        "response_types_supported": ["code", "token", "code token"],
        "response_modes_supported": ["query", "fragment", "form_post"],
        "request_object_signing_alg_values_supported": None,
        "request_object_encryption_alg_values_supported": None,
        "request_object_encryption_enc_values_supported": None,
        "grant_types_supported": ["authorization_code", "implicit"],
        "scopes_supported": [],
    }

    def __init__(self, endpoint_context, **kwargs):
        Endpoint.__init__(self, endpoint_context, **kwargs)
        self.post_parse_request.append(self._do_request_uri)
        self.post_parse_request.append(self._post_parse_request)
        self.allowed_request_algorithms = AllowedAlgorithms(ALG_PARAMS)

    def filter_request(self, endpoint_context, req):
        return req

    def extra_response_args(self, aresp):
        return aresp

    def verify_response_type(self, request, cinfo):
        # Checking response types
        _registered = [set(rt.split(" ")) for rt in cinfo.get("response_types", [])]
        if not _registered:
            # If no response_type is registered by the client then we'll
            # use code.
            _registered = [{"code"}]

        # Is the asked for response_type among those that are permitted
        return set(request["response_type"]) in _registered

    def mint_token(self, token_type, grant, session_id, based_on=None):
        _mngr = self.endpoint_context.session_manager
        usage_rules = grant.usage_rules.get(token_type, {})

        token = grant.mint_token(
            session_id=session_id,
            endpoint_context=self.endpoint_context,
            token_type=token_type,
            token_handler=_mngr.token_handler["access_token"],
            based_on=based_on,
            usage_rules=usage_rules
        )

        _exp_in = usage_rules.get("expires_in")
        if isinstance(_exp_in, str):
            _exp_in = int(_exp_in)
        if _exp_in:
            token.expires_at = utc_time_sans_frac() + _exp_in

        self.endpoint_context.session_manager.set(unpack_session_key(session_id),
                                                  grant)

        return token

    def _do_request_uri(self, request, client_id, endpoint_context, **kwargs):
        _request_uri = request.get("request_uri")
        if _request_uri:
            # Do I do pushed authorization requests ?
            if "pushed_authorization" in endpoint_context.endpoint:
                # Is it a UUID urn
                if _request_uri.startswith("urn:uuid:"):
                    _req = endpoint_context.par_db.get(_request_uri)
                    if _req:
                        del endpoint_context.par_db[_request_uri]  # One time usage
                        return _req
                    else:
                        raise ValueError("Got a request_uri I can not resolve")

            # Do I support request_uri ?
            _supported = endpoint_context.provider_info.get(
                "request_uri_parameter_supported", True
            )
            _registered = endpoint_context.cdb[client_id].get("request_uris")
            # Not registered should be handled else where
            if _registered:
                # Before matching remove a possible fragment
                _p = _request_uri.split("#")
                # ignore registered fragments for now.
                if _p[0] not in [l[0] for l in _registered]:
                    raise ValueError("A request_uri outside the registered")

            # Fetch the request
            _resp = endpoint_context.httpc.get(
                _request_uri, **endpoint_context.httpc_params
            )
            if _resp.status_code == 200:
                args = {"keyjar": endpoint_context.keyjar, "issuer": client_id}
                _ver_request = self.request_cls().from_jwt(_resp.text, **args)
                self.allowed_request_algorithms(
                    client_id,
                    endpoint_context,
                    _ver_request.jws_header.get("alg", "RS256"),
                    "sign",
                )
                if _ver_request.jwe_header is not None:
                    self.allowed_request_algorithms(
                        client_id,
                        endpoint_context,
                        _ver_request.jws_header.get("alg"),
                        "enc_alg",
                    )
                    self.allowed_request_algorithms(
                        client_id,
                        endpoint_context,
                        _ver_request.jws_header.get("enc"),
                        "enc_enc",
                    )
                # The protected info overwrites the non-protected
                for k, v in _ver_request.items():
                    request[k] = v

                request[verified_claim_name("request")] = _ver_request
            else:
                raise ServiceError("Got a %s response", _resp.status)

        return request

    def _post_parse_request(self, request, client_id, endpoint_context, **kwargs):
        """
        Verify the authorization request.

        :param endpoint_context:
        :param request:
        :param client_id:
        :param kwargs:
        :return:
        """
        if not request:
            logger.debug("No AuthzRequest")
            return self.error_cls(
                error="invalid_request", error_description="Can not parse AuthzRequest"
            )

        request = self.filter_request(endpoint_context, request)

        _cinfo = endpoint_context.cdb.get(client_id)
        if not _cinfo:
            logger.error(
                "Client ID ({}) not in client database".format(request["client_id"])
            )
            return self.error_cls(
                error="unauthorized_client", error_description="unknown client"
            )

        # Is the asked for response_type among those that are permitted
        if not self.verify_response_type(request, _cinfo):
            return self.error_cls(
                error="invalid_request",
                error_description="Trying to use unregistered response_type",
            )

        # Get a verified redirect URI
        try:
            redirect_uri = get_uri(endpoint_context, request, "redirect_uri")
        except (RedirectURIError, ParameterError, UnknownClient) as err:
            return self.error_cls(
                error="invalid_request",
                error_description="{}:{}".format(err.__class__.__name__, err),
            )
        else:
            request["redirect_uri"] = redirect_uri

        return request

    def pick_authn_method(self, request, redirect_uri, acr=None, **kwargs):
        auth_id = kwargs.get("auth_method_id")
        if auth_id:
            return self.endpoint_context.authn_broker[auth_id]

        if acr:
            res = self.endpoint_context.authn_broker.pick(acr)
        else:
            res = pick_auth(self.endpoint_context, request)

        if res:
            return res
        else:
            return {
                "error": "access_denied",
                "error_description": "ACR I do not support",
                "return_uri": redirect_uri,
                "return_type": request["response_type"],
            }

    def setup_auth(self, request, redirect_uri, cinfo, cookie, acr=None, **kwargs):
        """

        :param request: The authorization/authentication request
        :param redirect_uri:
        :param cinfo: client info
        :param cookie:
        :param acr: Default ACR, if nothing else is specified
        :param kwargs:
        :return:
        """

        res = self.pick_authn_method(request, redirect_uri, acr, **kwargs)

        authn = res["method"]
        authn_class_ref = res["acr"]

        try:
            _auth_info = kwargs.get("authn", "")
            if "upm_answer" in request and request["upm_answer"] == "true":
                _max_age = 0
            else:
                _max_age = max_age(request)

            identity, _ts = authn.authenticated_as(
                cookie, authorization=_auth_info, max_age=_max_age
            )
        except (NoSuchAuthentication, TamperAllert):
            identity = None
            _ts = 0
        except ToOld:
            logger.info("Too old authentication")
            identity = None
            _ts = 0
        except UnknownToken:
            logger.info("Unknown Token")
            identity = None
            _ts = 0
        else:
            if identity:
                try:  # If identity['uid'] is in fact a base64 encoded JSON string
                    _id = b64d(as_bytes(identity["uid"]))
                except BadSyntax:
                    pass
                else:
                    identity = json.loads(as_unicode(_id))

                    try:
                        _csi = self.endpoint_context.session_manager[identity.get("sid")]
                    except Revoked:
                        identity = None
                    else:
                        if _csi.is_active() is False:
                            identity = None

        authn_args = authn_args_gather(request, authn_class_ref, cinfo, **kwargs)
        _mngr = self.endpoint_context.session_manager
        _session_id = ""

        # To authenticate or Not
        if identity is None:  # No!
            logger.info("No active authentication")
            logger.debug(
                "Known clients: {}".format(list(self.endpoint_context.cdb.keys()))
            )

            if "prompt" in request and "none" in request["prompt"]:
                # Need to authenticate but not allowed
                return {
                    "error": "login_required",
                    "return_uri": redirect_uri,
                    "return_type": request["response_type"],
                }
            else:
                return {"function": authn, "args": authn_args}
        else:
            logger.info("Active authentication")
            if re_authenticate(request, authn):
                # demand re-authentication
                return {"function": authn, "args": authn_args}
            else:
                # I get back a dictionary
                user = identity["uid"]
                if "req_user" in kwargs:
                    if user != kwargs["req_user"]:
                        logger.debug("Wanted to be someone else!")
                        if "prompt" in request and "none" in request["prompt"]:
                            # Need to authenticate but not allowed
                            return {
                                "error": "login_required",
                                "return_uri": redirect_uri,
                            }
                        else:
                            return {"function": authn, "args": authn_args}

                if "sid" in identity:
                    _session_id = identity["sid"]

                    # make sure the client is the same
                    _uid, _cid, _gid = unpack_session_key(_session_id)
                    if request["client_id"] != _cid:
                        return {"function": authn, "args": authn_args}

                    grant = _mngr[_session_id]
                    if grant.is_active() is False:
                        return {"function": authn, "args": authn_args}
                    elif request != grant.authorization_request:
                        authn_event = _mngr.get_authentication_event(session_id=_session_id)
                        if authn_event.is_valid() is False:  # if not valid, do new login
                            return {"function": authn, "args": authn_args}

                        # create new grant
                        _session_id = _mngr.create_grant(authn_event=authn_event,
                                                         auth_req=request,
                                                         user_id=user,
                                                         client_id=request["client_id"])

        if _session_id:
            authn_event = _mngr.get_authentication_event(session_id=_session_id)
            if authn_event.is_valid() is False:  # if not valid, do new login
                return {"function": authn, "args": authn_args}
        else:
            authn_event = create_authn_event(
                identity["uid"],
                authn_info=authn_class_ref,
                time_stamp=_ts,
            )
            _exp_in = authn.kwargs.get("expires_in")
            if _exp_in and "valid_until" in authn_event:
                authn_event["valid_until"] = utc_time_sans_frac() + _exp_in

            _token_usage_rules = self.endpoint_context.authz.usage_rules(
                request["client_id"])
            _session_id = _mngr.create_session(authn_event=authn_event, auth_req=request,
                                               user_id=user, client_id=request["client_id"],
                                               token_usage_rules=_token_usage_rules)

        return {"session_id": _session_id, "identity": identity, "user": user}

    def aresp_check(self, aresp, request):
        return ""

    def response_mode(self, request, **kwargs):
        resp_mode = request["response_mode"]
        if resp_mode == "form_post":
            msg = FORM_POST.format(
                inputs=inputs(kwargs["response_args"].to_dict()),
                action=kwargs["return_uri"],
            )
            kwargs.update(
                {
                    "response_msg": msg,
                    "content_type": "text/html",
                    "response_placement": "body",
                }
            )
        elif resp_mode == "fragment":
            if "fragment_enc" in kwargs:
                if not kwargs["fragment_enc"]:
                    # Can't be done
                    raise InvalidRequest("wrong response_mode")
            else:
                kwargs["fragment_enc"] = True
        elif resp_mode == "query":
            if "fragment_enc" in kwargs:
                if kwargs["fragment_enc"]:
                    # Can't be done
                    raise InvalidRequest("wrong response_mode")
        else:
            raise InvalidRequest("Unknown response_mode")
        return kwargs

    def error_response(self, response_info, error, error_description):
        resp = self.error_cls(
            error=error, error_description=str(error_description)
        )
        response_info["response_args"] = resp
        return response_info

    def create_authn_response(self, request: Union[dict, Message], sid: str) -> dict:
        """

        :param request:
        :param sid:
        :return:
        """
        # create the response
        aresp = self.response_cls()
        if request.get("state"):
            aresp["state"] = request["state"]


        if "response_type" in request and request["response_type"] == ["none"]:
            fragment_enc = False
        else:
            _context = self.endpoint_context
            _mngr = self.endpoint_context.session_manager

            _sinfo = _mngr.get_session_info(sid, grant=True)


            if request.get("scope"):
                aresp["scope"] = request["scope"]

            rtype = set(request["response_type"][:])
            handled_response_type = []

            fragment_enc = True
            if len(rtype) == 1 and "code" in rtype:
                fragment_enc = False

            grant = _sinfo["grant"]

            if "code" in request["response_type"]:
                _code = self.mint_token(
                    token_type='authorization_code',
                    grant=grant,
                    session_id= _sinfo["session_id"])
                aresp["code"] = _code.value
                handled_response_type.append("code")
            else:
                _code = None

            if "token" in rtype:
                if _code:
                    based_on = _code
                else:
                    based_on = None

                _access_token = self.mint_token(token_type="access_token",
                                                grant=grant,
                                                session_id=_sinfo["session_id"],
                                                based_on=based_on)
                aresp['access_token'] = _access_token.value
                aresp['token_type'] = "Bearer"
                if _access_token.expires_at:
                    aresp["expires_in"] = _access_token.expires_at - utc_time_sans_frac()
                handled_response_type.append("token")
            else:
                _access_token = None

            if "id_token" in request["response_type"]:
                kwargs = {}
                if {"code", "id_token", "token"}.issubset(rtype):
                    kwargs = {"code": _code.value, "access_token": _access_token.value}
                elif {"code", "id_token"}.issubset(rtype):
                    kwargs = {"code": _code.value}
                elif {"id_token", "token"}.issubset(rtype):
                    kwargs = {"access_token": _access_token.value}

                try:
                    id_token = _context.idtoken.make(sid, **kwargs)
                except (JWEException, NoSuitableSigningKeys) as err:
                    logger.warning(str(err))
                    resp = self.error_cls(
                        error="invalid_request",
                        error_description="Could not sign/encrypt id_token",
                    )
                    return {"response_args": resp, "fragment_enc": fragment_enc}

                aresp["id_token"] = id_token
                _mngr.update([_sinfo["user_id"], _sinfo["client_id"]],
                             {"id_token": id_token})
                handled_response_type.append("id_token")

            not_handled = rtype.difference(handled_response_type)
            if not_handled:
                resp = self.error_cls(
                    error="invalid_request", error_description="unsupported_response_type"
                )
                return {"response_args": resp, "fragment_enc": fragment_enc}

        aresp = self.extra_response_args(aresp)

        return {"response_args": aresp, "fragment_enc": fragment_enc}

    def post_authentication(self, request: Union[dict, Message],
                            session_id: str, **kwargs) -> dict:
        """
        Things that are done after a successful authentication.

        :param request: The authorization request
        :param session_id: Session identifier
        :param kwargs:
        :return: A dictionary with 'response_args'
        """

        response_info = {}
        _mngr = self.endpoint_context.session_manager

        # Do the authorization
        try:
            grant = self.endpoint_context.authz(session_id, request=request)
        except ToOld as err:
            return self.error_response(
                response_info,
                "access_denied",
                "Authentication to old {}".format(err.args),
            )
        except Exception as err:
            return self.error_response(
                response_info, "access_denied", "{}".format(err.args)
            )
        else:
            user_id, client_id, grant_id = unpack_session_key(session_id)
            try:
                _mngr.set([user_id, client_id, grant_id], grant)
            except Exception as err:
                return self.error_response(
                    response_info, "server_error", "{}".format(err.args)
                )

        logger.debug("response type: %s" % request["response_type"])

        response_info = self.create_authn_response(request, session_id)
        response_info["session_id"] = session_id

        logger.debug("Known clients: {}".format(list(self.endpoint_context.cdb.keys())))

        try:
            redirect_uri = get_uri(self.endpoint_context, request, "redirect_uri")
        except (RedirectURIError, ParameterError) as err:
            return self.error_response(
                response_info, "invalid_request", "{}".format(err.args)
            )
        else:
            response_info["return_uri"] = redirect_uri

        # Must not use HTTP unless implicit grant type and native application
        # info = self.aresp_check(response_info['response_args'], request)
        # if isinstance(info, ResponseMessage):
        #     return info

        _cookie = new_cookie(
            self.endpoint_context,
            sid=session_id,
            state=request["state"],
            cookie_name=self.endpoint_context.cookie_name["session"],
        )

        # Now about the response_mode. Should not be set if it's obvious
        # from the response_type. Knows about 'query', 'fragment' and
        # 'form_post'.

        if "response_mode" in request:
            try:
                response_info = self.response_mode(request, **response_info)
            except InvalidRequest as err:
                return self.error_response(
                    response_info, "invalid_request", "{}".format(err.args)
                )

        response_info["cookie"] = [_cookie]

        return response_info

    # def setup_client_session(self, user_id: str, request: dict) -> str:
    #     _mngr = self.endpoint_context.session_manager
    #     client_id = request['client_id']
    #
    #     client_info = ClientSessionInfo(
    #         authorization_request=request,
    #         sub=_mngr.sub_func['public'](user_id, salt=_mngr.salt)
    #     )
    #
    #     _mngr.set([user_id, client_id], client_info)
    #     return session_key(user_id, client_id)

    def authz_part2(self, request, session_id, **kwargs):
        """
        After the authentication this is where you should end up

        :param user:
        :param request: The Authorization Request
        :param session_id: Session identifier
        :param kwargs: possible other parameters
        :return: A redirect to the redirect_uri of the client
        """

        try:
            resp_info = self.post_authentication(request, session_id, **kwargs)
        except Exception as err:
            return self.error_response({}, "server_error", err)

        if "check_session_iframe" in self.endpoint_context.provider_info:
            ec = self.endpoint_context
            salt = rndstr()
            try:
                authn_event = ec.session_manager.get_authentication_event(session_id)
            except KeyError:
                return self.error_response({}, "server_error", "No such session")
            else:
                if authn_event.is_valid() is False:
                    return self.error_response({}, "server_error", "Authentication has timed out")

            _state = b64e(
                as_bytes(json.dumps({"authn_time": authn_event["authn_time"]}))
            )

            opbs_value = ''
            if hasattr(ec.cookie_dealer, 'create_cookie'):
                session_cookie = ec.cookie_dealer.create_cookie(
                    as_unicode(_state),
                    typ="session",
                    cookie_name=ec.cookie_name["session_management"],
                    same_site="None",
                    http_only=False,
                )

                opbs = session_cookie[ec.cookie_name["session_management"]]
                opbs_value = opbs.value
            else:
                session_cookie = None
                logger.debug(
                    "Failed to set Cookie, that's not configured in main configuration.")

            logger.debug(
                "compute_session_state: client_id=%s, origin=%s, opbs=%s, salt=%s",
                request["client_id"],
                resp_info["return_uri"],
                opbs_value,
                salt,
            )

            _session_state = compute_session_state(
                opbs_value, salt, request["client_id"], resp_info["return_uri"]
            )

            if opbs_value and session_cookie:
                if "cookie" in resp_info:
                    if isinstance(resp_info["cookie"], list):
                        resp_info["cookie"].append(session_cookie)
                    else:
                        append_cookie(resp_info["cookie"], session_cookie)
                else:
                    resp_info["cookie"] = session_cookie

            resp_info["response_args"]["session_state"] = _session_state

        # Mix-Up mitigation
        resp_info["response_args"]["iss"] = self.endpoint_context.issuer
        resp_info["response_args"]["client_id"] = request["client_id"]

        return resp_info

    def do_request_user(self, request_info, **kwargs):
        return kwargs

    def process_request(self, request: Union[Message, dict], **kwargs):
        """ The AuthorizationRequest endpoint

        :param request: The authorization request as a Message instance
        :return: dictionary
        """

        if isinstance(request, self.error_cls):
            return request

        _cid = request["client_id"]
        cinfo = self.endpoint_context.cdb[_cid]
        logger.debug("client {}: {}".format(_cid, cinfo))

        # this apply the default optionally deny_unknown_scopes policy
        if cinfo:
            check_unknown_scopes_policy(request, cinfo, self.endpoint_context)

        cookie = kwargs.get("cookie", "")
        if cookie:
            del kwargs["cookie"]

        kwargs = self.do_request_user(request_info=request, **kwargs)

        info = self.setup_auth(
            request, request["redirect_uri"], cinfo, cookie, **kwargs
        )

        if "error" in info:
            return info

        _function = info.get("function")
        if not _function:
            logger.debug("- authenticated -")
            logger.debug("AREQ keys: %s" % request.keys())
            return self.authz_part2(request=request, cookie=cookie, **info)

        try:
            # Run the authentication function
            return {
                "http_response": _function(**info["args"]),
                "return_uri": request["redirect_uri"],
            }
        except Exception as err:
            logger.exception(err)
            return {"http_response": "Internal error: {}".format(err)}


class AllowedAlgorithms:
    def __init__(self, algorithm_parameters):
        self.algorithm_parameters = algorithm_parameters

    def __call__(self, client_id, endpoint_context, alg, alg_type):
        _cinfo = endpoint_context.cdb[client_id]
        _pinfo = endpoint_context.provider_info

        _reg, _sup = self.algorithm_parameters[alg_type]
        _allowed = _cinfo.get(_reg)
        if _allowed is None:
            _allowed = _pinfo.get(_sup)

        if alg not in _allowed:
            logger.error(
                "Signing alg user: {} not among allowed: {}".format(alg, _allowed)
            )
            raise ValueError("Not allowed '%s' algorithm used", alg)


def re_authenticate(request, authn):
    return False

# class Authorization(authorization.Authorization):
#     request_cls = oauth2.AuthorizationRequest
#     response_cls = oauth2.AuthorizationResponse
#     error_cls = oauth2.AuthorizationErrorResponse
#     request_format = "urlencoded"
#     response_format = "urlencoded"
#     response_placement = "url"
#     endpoint_name = "authorization_endpoint"
#     name = "authorization"
#     default_capabilities = {
#         "claims_parameter_supported": True,
#         "request_parameter_supported": True,
#         "request_uri_parameter_supported": True,
#         "response_types_supported": ["code", "token", "code token"],
#         "response_modes_supported": ["query", "fragment", "form_post"],
#         "request_object_signing_alg_values_supported": None,
#         "request_object_encryption_alg_values_supported": None,
#         "request_object_encryption_enc_values_supported": None,
#         "grant_types_supported": ["authorization_code", "implicit"],
#         "scopes_supported": [],
#     }
#
#     def __init__(self, endpoint_context, **kwargs):
#         authorization.Authorization.__init__(self, endpoint_context, **kwargs)
#         # self.pre_construct.append(self._pre_construct)
#         self.post_parse_request.append(self._do_request_uri)
#         self.post_parse_request.append(self._post_parse_request)
#         # Has to be done elsewhere. To make sure things happen in order.
#         # self.scopes_supported = available_scopes(endpoint_context)
#
#     def setup_client_session(self, user_id: str, request: dict) -> str:
#         _mngr = self.endpoint_context.session_manager
#         client_id = request['client_id']
#
#         client_info = ClientSessionInfo(
#             authorization_request=request,
#             sub=_mngr.sub_func['public'](user_id, salt=_mngr.salt)
#         )
#
#         _mngr.set([user_id, client_id], client_info)
#         return session_key(user_id, client_id)
