"""仓库级 TLS 策略；默认验证证书，兼容设置只对配置的 HTTPS 来源生效。"""

import http.client
import ssl
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler

from errors import BuildError


def tls_options(verify_tls=True):
    """仅在显式关闭验证时传递扩展参数，保留独立调用者的默认接口行为。"""
    if type(verify_tls) is not bool:
        raise BuildError("verifyTls must be a boolean")
    return {} if verify_tls else {"verify_tls": False}


def tls_context(ca_file=None, verify_tls=True):
    """创建独立上下文，不修改进程级默认 HTTPS 验证设置。"""
    tls_options(verify_tls)
    if verify_tls:
        return ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def https_origin(url):
    parsed = urlsplit(url)
    return parsed.scheme.lower(), parsed.netloc.lower()


class RepositoryHTTPSHandler(HTTPSHandler):
    """urllib 的来源限定例外；即使直接复用 opener，其他来源仍严格验证。"""
    def __init__(self, origin, context, ca_file=None):
        super().__init__(context=context)
        self.origin, self.repository_context, self.ca_file = origin, context, ca_file

    def https_open(self, request):
        context = (self.repository_context if https_origin(request.full_url) == self.origin
                   else tls_context(self.ca_file))
        return self.do_open(http.client.HTTPSConnection, request, context=context)
