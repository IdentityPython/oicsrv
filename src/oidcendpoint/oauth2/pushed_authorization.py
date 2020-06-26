import uuid

from oidcendpoint.oauth2.authorization import Authorization
from oidcmsg import oauth2


class PushedAuthorization(Authorization):
    request_cls = oauth2.PushedAuthorizationRequest
    response_cls = oauth2.Message
    endpoint_name = "pushed_authorization_request_endpoint"
    request_placement = "body"
    request_format = "urlencoded"
    response_placement = "body"
    response_format = "json"
    name = "pushed_authorization"

    def __init__(self, endpoint_context, **kwargs):
        Authorization.__init__(self, endpoint_context, **kwargs)
        # self.pre_construct.append(self._pre_construct)
        self.post_parse_request.append(self._post_parse_request)
        self.ttl = kwargs.get("ttl", 3600)

    def process_request(self, request=None, **kwargs):
        """
        Store the request and return a URI.

        :param request:
        """
        # create URN

        _urn = "urn:uuid:{}".format(uuid.uuid4())
        self.endpoint_context.par_db[_urn] = request

        return {
            "http_response": {"request_uri": _urn, "expires_in": self.ttl},
            "return_uri": request["redirect_uri"],
        }
