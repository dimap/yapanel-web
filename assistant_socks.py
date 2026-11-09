"""SOCKS5 transport for urllib.request on Android and desktop Python.

RFC 1928 CONNECT and RFC 1929 username/password. No third-party dependencies
and no global socket monkey-patching.
"""

import functools
import http.client
import ipaddress
import socket
import ssl
import struct
import urllib.parse
import urllib.request


_SOCKS_REPLIES = {
    1: "general SOCKS5 failure",
    2: "connection forbidden by proxy rules",
    3: "network unreachable",
    4: "destination unreachable",
    5: "destination refused connection",
    6: "TTL expired",
    7: "SOCKS5 command unsupported",
    8: "SOCKS5 address type unsupported",
}


class SocksProxyError(OSError):
    """Ошибка SOCKS5 без раскрытия учётных данных прокси."""


def parse_socks_url(proxy_url):
    """(host, port, username|None, password|None, remote_dns)."""
    try:
        parsed = urllib.parse.urlsplit(proxy_url)
        scheme = parsed.scheme.lower()
        if scheme not in ("socks5", "socks5h"):
            raise SocksProxyError("ожидается socks5:// или socks5h://")
        host = parsed.hostname
        port = parsed.port
        if not host or port is None or not (1 <= port <= 65535):
            raise SocksProxyError("неверный адрес или порт SOCKS5-прокси")
        if parsed.path or parsed.query or parsed.fragment:
            raise SocksProxyError("адрес SOCKS5-прокси не должен содержать путь, ? или #")
        has_user = parsed.username is not None
        has_password = parsed.password is not None
        if has_user != has_password:
            raise SocksProxyError("укажите одновременно логин и пароль SOCKS5 или не указывайте их")
        username = urllib.parse.unquote_to_bytes(parsed.username) if has_user else None
        password = urllib.parse.unquote_to_bytes(parsed.password) if has_password else None
        if has_user and (not username or not password or len(username) > 255 or len(password) > 255):
            raise SocksProxyError("логин и пароль SOCKS5 должны содержать от 1 до 255 байт")
        return host, port, username, password, scheme == "socks5h"
    except (ValueError, TypeError, AttributeError):
        raise SocksProxyError("неверный формат SOCKS5-прокси") from None


def _read_exact(sock, size):
    out = bytearray()
    while len(out) < size:
        chunk = sock.recv(size - len(out))
        if not chunk:
            raise SocksProxyError("SOCKS5-прокси преждевременно закрыл соединение")
        out.extend(chunk)
    return bytes(out)


def _socks_address(host, port, remote_dns):
    # socks5:// разрешает DNS локально, socks5h:// передаёт имя прокси.
    if not remote_dns:
        try:
            target = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
        except OSError:
            raise SocksProxyError("не удалось разрешить DNS-имя назначения локально") from None
        address = ipaddress.ip_address(target)
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
    if address is not None:
        atyp = 1 if address.version == 4 else 4
        value = address.packed
    else:
        name = host.encode("idna")
        if len(name) > 255:
            raise SocksProxyError("слишком длинное имя назначения для SOCKS5")
        atyp = 3
        value = bytes((len(name),)) + name
    return bytes((atyp,)) + value + struct.pack("!H", port)


def socks_connect(proxy, dest_host, dest_port, timeout):
    proxy_host, proxy_port, username, password, remote_dns = proxy
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        # При переданных учётных данных не предлагаем анонимный fallback.
        method = 2 if username is not None else 0
        sock.sendall(bytes((5, 1, method)))
        version, selected = _read_exact(sock, 2)
        if version != 5:
            raise SocksProxyError("прокси вернул ответ, несовместимый с SOCKS5")
        if selected != method:
            if selected == 255:
                raise SocksProxyError("SOCKS5-прокси отклонил предложенную авторизацию")
            raise SocksProxyError("SOCKS5-прокси выбрал неожиданный метод авторизации")
        if method == 2:
            sock.sendall(bytes((1, len(username))) + username + bytes((len(password),)) + password)
            auth_version, status = _read_exact(sock, 2)
            if auth_version != 1 or status != 0:
                raise SocksProxyError("SOCKS5-прокси отклонил логин или пароль")
        sock.sendall(b"\x05\x01\x00" + _socks_address(dest_host, dest_port, remote_dns))
        version, status, reserved, atyp = _read_exact(sock, 4)
        if version != 5 or reserved != 0:
            raise SocksProxyError("некорректный ответ SOCKS5-прокси")
        if status:
            raise SocksProxyError(_SOCKS_REPLIES.get(status, "unknown SOCKS5 error"))
        if atyp == 1:
            _read_exact(sock, 4)
        elif atyp == 4:
            _read_exact(sock, 16)
        elif atyp == 3:
            _read_exact(sock, _read_exact(sock, 1)[0])
        else:
            raise SocksProxyError("SOCKS5-прокси вернул неизвестный тип адреса")
        _read_exact(sock, 2)
        return sock
    except BaseException:
        sock.close()
        raise


class _SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *, proxy, **kwargs):
        super().__init__(host, **kwargs)
        self._socks_proxy = proxy

    def connect(self):
        self.sock = socks_connect(self._socks_proxy, self.host, self.port, self.timeout)
        if self._tunnel_host:
            self._tunnel()


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, proxy, **kwargs):
        super().__init__(host, **kwargs)
        self._socks_proxy = proxy

    def connect(self):
        self.sock = socks_connect(self._socks_proxy, self.host, self.port, self.timeout)
        if self._tunnel_host:
            self._tunnel()
        name = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=name)


class _SocksHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, proxy):
        super().__init__()
        self._socks_proxy = proxy

    def http_open(self, request):
        return self.do_open(
            functools.partial(_SocksHTTPConnection, proxy=self._socks_proxy),
            request,
        )


class _SocksHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, proxy):
        super().__init__()  # Стандартная проверка TLS, включая SSL_CERT_FILE.
        self._socks_proxy = proxy

    def https_open(self, request):
        return self.do_open(
            functools.partial(_SocksHTTPSConnection, proxy=self._socks_proxy),
            request,
            context=self._context,
        )


def build_socks_opener(proxy_url):
    """Изолированный urllib opener для HTTP/HTTPS через SOCKS5."""
    proxy = parse_socks_url(proxy_url)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _SocksHTTPHandler(proxy),
        _SocksHTTPSHandler(proxy),
    )
