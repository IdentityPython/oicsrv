import json
import logging
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlparse

from cryptojwt import as_unicode
from cryptojwt import b64d
from cryptojwt.jwe.aes import AES_GCMEncrypter
from cryptojwt.jwe.utils import split_ctx_and_tag
from cryptojwt.jws.exception import JWSException
from cryptojwt.jws.jws import factory
from cryptojwt.jws.utils import alg2keytype
from cryptojwt.jwt import JWT
from cryptojwt.utils import as_bytes
from cryptojwt.utils import b64e
from oidcmsg.exception import InvalidRequest
from oidcmsg.message import Message
from oidcmsg.oauth2 import ResponseMessage
from oidcmsg.oidc import verified_claim_name
from oidcmsg.oidc.session import BACK_CHANNEL_LOGOUT_EVENT
from oidcmsg.oidc.session import EndSessionRequest

from oidcendpoint import rndstr
from oidcendpoint.client_authn import UnknownOrNoAuthnMethod
from oidcendpoint.cookie import append_cookie
from oidcendpoint.endpoint import Endpoint
from oidcendpoint.endpoint_context import add_path
from oidcendpoint.oauth2.authorization import verify_uri
from oidcendpoint.session import session_key

logger = logging.getLogger(__name__)


def do_front_channel_logout_iframe(cinfo, iss, sid):
    """

    :param cinfo: Client info
    :param iss: Issuer ID
    :param sid: Session ID
    :return: IFrame
    """
    try:
        frontchannel_logout_uri = cinfo["frontchannel_logout_uri"]
    except KeyError:
        return None

    try:
        flsr = cinfo["frontchannel_logout_session_required"]
    except KeyError:
        flsr = False

    if flsr:
        _query = {"iss": iss, "sid": sid}
        if "?" in frontchannel_logout_uri:
            p = urlparse(frontchannel_logout_uri)
            _args = parse_qs(p.query)
            _args.update(_query)
            _query = _args
            _np = p._replace(query="")
            frontchannel_logout_uri = _np.geturl()

        _iframe = '<iframe src="{}?{}">'.format(
            frontchannel_logout_uri, urlencode(_query, doseq=True)
        )
    else:
        _iframe = '<iframe src="{}">'.format(frontchannel_logout_uri)

    return _iframe


class Session(Endpoint):
    request_cls = EndSessionRequest
    response_cls = Message
    request_format = "urlencoded"
    response_format = "urlencoded"
    response_placement = "url"
    endpoint_name = "end_session_endpoint"
    name = "session"
    default_capabilities = {
        "frontchannel_logout_supported": True,
        "frontchannel_logout_session_supported": True,
        "backchannel_logout_supported": True,
        "backchannel_logout_session_supported": True,
        "check_session_iframe": None,
    }

    def __init__(self, endpoint_context, **kwargs):
        _csi = kwargs.get("check_session_iframe")
        if _csi and not _csi.startswith("http"):
            kwargs["check_session_iframe"] = add_path(endpoint_context.issuer, _csi)
        Endpoint.__init__(self, endpoint_context, **kwargs)
        self.iv = as_bytes(rndstr(24))

    def _encrypt_sid(self, sid):
        encrypter = AES_GCMEncrypter(key=as_bytes(self.endpoint_context.symkey))
        enc_msg = encrypter.encrypt(as_bytes(sid), iv=self.iv)
        return as_unicode(b64e(enc_msg))

    def _decrypt_sid(self, enc_msg):
        _msg = b64d(as_bytes(enc_msg))
        encrypter = AES_GCMEncrypter(key=as_bytes(self.endpoint_context.symkey))
        ctx, tag = split_ctx_and_tag(_msg)
        return as_unicode(encrypter.decrypt(as_bytes(ctx), iv=self.iv, tag=as_bytes(tag)))

    def do_back_channel_logout(self, cinfo, sub, sid):
        """

        :param cinfo: Client information
        :param sub: Subject identifier
        :param sid: The session ID
        :return: Tuple with logout URI and signed logout token
        """

        _cntx = self.endpoint_context

        try:
            back_channel_logout_uri = cinfo["backchannel_logout_uri"]
        except KeyError:
            return None

        # Create the logout token
        # always include sub and sid so I don't check for
        # backchannel_logout_session_required

        enc_msg = self._encrypt_sid(sid)

        payload = {
            "sub": sub,
            "sid": enc_msg,
            "events": {BACK_CHANNEL_LOGOUT_EVENT: {}}
        }

        try:
            alg = cinfo["id_token_signed_response_alg"]
        except KeyError:
            alg = _cntx.provider_info["id_token_signing_alg_values_supported"][0]

        _jws = JWT(_cntx.keyjar, iss=_cntx.issuer, lifetime=86400, sign_alg=alg)
        _jws.with_jti = True
        _logout_token = _jws.pack(payload=payload, recv=cinfo["client_id"])

        return back_channel_logout_uri, _logout_token

    def clean_sessions(self, usids):
        # Revoke all sessions
        for sid in usids:
            self.endpoint_context.session_manager.revoke_client_session(sid)

    def logout_all_clients(self, sid):
        _mngr = self.endpoint_context.session_manager
        _session_info = _mngr.get_session_info(sid, user_session_info=True,
                                               client_session_info=True)

        # Front-/Backchannel logout ?
        _cdb = self.endpoint_context.cdb
        _iss = self.endpoint_context.issuer
        _user_id = _session_info["user_id"]
        bc_logouts = {}
        fc_iframes = {}
        _rel_sid = []
        for _client_id in _session_info["user_session_info"]["subordinate"]:
            if "backchannel_logout_uri" in _cdb[_client_id]:
                _sub = _mngr.get([_user_id, _client_id])["sub"]
                _sid = session_key(_user_id, _client_id)
                _rel_sid.append(_sid)
                _spec = self.do_back_channel_logout(_cdb[_client_id], _sub, _sid)
                if _spec:
                    bc_logouts[_client_id] = _spec
            elif "frontchannel_logout_uri" in _cdb[_client_id]:
                # Construct an IFrame
                _sid = session_key(_user_id, _client_id)
                _rel_sid.append(_sid)
                _spec = do_front_channel_logout_iframe(_cdb[_client_id], _iss, _sid)
                if _spec:
                    fc_iframes[_client_id] = _spec

        self.clean_sessions(_rel_sid)

        res = {}
        if bc_logouts:
            res["blu"] = bc_logouts
        if fc_iframes:
            res["flu"] = fc_iframes
        return res

    def unpack_signed_jwt(self, sjwt, sig_alg=""):
        _jwt = factory(sjwt)
        if _jwt:
            if sig_alg:
                alg = sig_alg
            else:
                alg = self.kwargs["signing_alg"]

            sign_keys = self.endpoint_context.keyjar.get_signing_key(alg2keytype(alg))
            _info = _jwt.verify_compact(keys=sign_keys, sigalg=alg)
            return _info
        else:
            raise ValueError("Not a signed JWT")

    def logout_from_client(self, sid):
        _cdb = self.endpoint_context.cdb
        _session_information = self.endpoint_context.session_manager.get_session_info(
            sid, client_session_info=True)
        _client_id = _session_information["client_id"]

        res = {}
        if "backchannel_logout_uri" in _cdb[_client_id]:
            _sub = _session_information["client_session_info"]["sub"]
            _spec = self.do_back_channel_logout(_cdb[_client_id], _sub, sid)
            if _spec:
                res["blu"] = {_client_id: _spec}
        elif "frontchannel_logout_uri" in _cdb[_client_id]:
            # Construct an IFrame
            _spec = do_front_channel_logout_iframe(
                _cdb[_client_id], self.endpoint_context.issuer, sid
            )
            if _spec:
                res["flu"] = {_client_id: _spec}

        self.clean_sessions([sid])
        return res

    def process_request(self, request=None, cookie=None, **kwargs):
        """
        Perform user logout

        :param request:
        :param cookie:
        :param kwargs:
        :return:
        """
        _cntx = self.endpoint_context
        _mngr = _cntx.session_manager

        if "post_logout_redirect_uri" in request:
            if "id_token_hint" not in request:
                raise InvalidRequest(
                    "If post_logout_redirect_uri then id_token_hint is a MUST"
                )
        _cookie_name = self.endpoint_context.cookie_name["session"]
        try:
            part = self.endpoint_context.cookie_dealer.get_cookie_value(
                cookie, cookie_name=_cookie_name
            )
        except IndexError:
            raise InvalidRequest("Cookie error")
        except (KeyError, AttributeError):
            part = None

        if part:
            # value is a base64 encoded JSON document
            _cookie_info = json.loads(as_unicode(b64d(as_bytes(part[0]))))
            logger.debug("Cookie info: {}".format(_cookie_info))
            try:
                _session_info = _mngr.get_session_info(_cookie_info["sid"],
                                                       client_session_info=True)
            except KeyError:
                raise ValueError("Can't find any corresponding session")
        else:
            logger.debug("No relevant cookie")
            raise ValueError("Missing cookie")

        if "id_token_hint" in request and _session_info:
            _id_token = request[verified_claim_name("id_token_hint")]
            logger.debug(
                "ID token hint: {}".format(_id_token)
            )

            _aud = _id_token["aud"]
            if _session_info["client_id"] not in _aud:
                raise ValueError("Client ID doesn't match")

            if _id_token["sub"] != _session_info["client_session_info"]["sub"]:
                raise ValueError("Sub doesn't match")
        else:
            _aud = []

        _cinfo = _cntx.cdb[_session_info["client_id"]]

        # verify that the post_logout_redirect_uri if present are among the ones
        # registered

        try:
            _uri = request["post_logout_redirect_uri"]
        except KeyError:
            if _cntx.issuer.endswith("/"):
                _uri = "{}{}".format(_cntx.issuer, self.kwargs["post_logout_uri_path"])
            else:
                _uri = "{}/{}".format(_cntx.issuer, self.kwargs["post_logout_uri_path"])
            plur = False
        else:
            plur = True
            verify_uri(_cntx, request, "post_logout_redirect_uri",
                       client_id=_session_info["client_id"])

        payload = {
            "sid": _session_info["session_id"],
        }

        # redirect user to OP logout verification page
        if plur and "state" in request:
            _uri = "{}?{}".format(_uri, urlencode({"state": request["state"]}))
            payload["state"] = request["state"]

        payload["redirect_uri"] = _uri

        logger.debug("JWS payload: {}".format(payload))
        # From me to me
        _jws = JWT(
            _cntx.keyjar,
            iss=_cntx.issuer,
            lifetime=86400,
            sign_alg=self.kwargs["signing_alg"],
        )
        sjwt = _jws.pack(payload=payload, recv=_cntx.issuer)

        location = "{}?{}".format(
            self.kwargs["logout_verify_url"], urlencode({"sjwt": sjwt})
        )
        return {"redirect_location": location}

    def parse_request(self, request, auth=None, **kwargs):
        """

        :param request:
        :param auth:
        :param kwargs:
        :return:
        """

        if not request:
            request = {}

        # Verify that the client is allowed to do this
        try:
            auth_info = self.client_authentication(request, auth, **kwargs)
        except UnknownOrNoAuthnMethod:
            pass
        else:
            if not auth_info:
                pass
            elif isinstance(auth_info, ResponseMessage):
                return auth_info
            else:
                request["client_id"] = auth_info["client_id"]
                request["access_token"] = auth_info["token"]

        if isinstance(request, dict):
            request = self.request_cls(**request)
            if not request.verify(keyjar=self.endpoint_context.keyjar, sigalg=""):
                raise InvalidRequest("Request didn't verify")
            # id_token_signing_alg_values_supported
            try:
                _ith = request[verified_claim_name("id_token_hint")]
            except KeyError:
                pass
            else:
                if (
                        _ith.jws_header["alg"]
                        not in self.endpoint_context.provider_info[
                    "id_token_signing_alg_values_supported"
                ]
                ):
                    raise JWSException("Unsupported signing algorithm")

        return request

    def do_verified_logout(self, sid, alla=False, **kwargs):
        if alla:
            _res = self.logout_all_clients(sid=sid)
        else:
            _res = self.logout_from_client(sid=sid)

        bcl = _res.get("blu")
        if bcl:
            # take care of Back channel logout first
            for _cid, spec in bcl.items():
                _url, sjwt = spec
                logger.info("logging out from {} at {}".format(_cid, _url))

                res = self.endpoint_context.httpc.post(
                    _url,
                    data="logout_token={}".format(sjwt),
                    **self.endpoint_context.httpc_params
                )

                if res.status_code < 300:
                    logger.info("Logged out from {}".format(_cid))
                elif res.status_code in [501, 504]:
                    logger.info("Got a %s which is acceptable", res.status_code)
                elif res.status_code >= 400:
                    logger.info("failed to logout from {}".format(_cid))

        return _res["flu"].values() if _res.get("flu") else []

    def kill_cookies(self):
        _ec = self.endpoint_context
        _dealer = _ec.cookie_dealer
        _kakor = append_cookie(
            _dealer.create_cookie(
                "none",
                typ="session",
                ttl=0,
                cookie_name=_ec.cookie_name["session_management"],
            ),
            _dealer.create_cookie(
                "none", typ="session", ttl=0, cookie_name=_ec.cookie_name["session"]
            ),
        )

        return _kakor
