"""按 RFC 8032 实现无第三方依赖的 Ed25519，用于离线证明签名。

Python 大整数运算不是常量时间；私钥应只存放在受控构建主机。
"""

import hashlib


P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
I = pow(2, (P - 1) // 4, P)
IDENTITY = (0, 1, 1, 0)


def _recover_x(y, sign):
    if y >= P:
        raise ValueError("Invalid Ed25519 point encoding")
    xx = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P:
        x = x * I % P
    if (x * x - xx) % P or (x == 0 and sign):
        raise ValueError("Invalid Ed25519 point")
    return P - x if x & 1 != sign else x


BASE_Y = 4 * pow(5, P - 2, P) % P
BASE_X = _recover_x(BASE_Y, 0)
BASE = (BASE_X, BASE_Y, 1, BASE_X * BASE_Y % P)


def _add(first, second):
    x1, y1, z1, t1 = first
    x2, y2, z2, t2 = second
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = 2 * D * t1 * t2 % P
    d = 2 * z1 * z2 % P
    e, f, g, h = (b - a) % P, (d - c) % P, (d + c) % P, (b + a) % P
    return e * f % P, g * h % P, f * g % P, e * h % P


def _multiply(point, scalar):
    result = IDENTITY
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _encode(point):
    x, y, z, _ = point
    inverse = pow(z, P - 2, P)
    value = y * inverse % P | ((x * inverse % P & 1) << 255)
    return value.to_bytes(32, "little")


def _decode(raw):
    if len(raw) != 32:
        raise ValueError("Ed25519 point must be 32 bytes")
    value = int.from_bytes(raw, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return x, y, 1, x * y % P


def _expanded_seed(seed):
    if len(seed) != 32:
        raise ValueError("Ed25519 private seed must be 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def public_key(seed):
    """从 32 字节 Ed25519 私钥种子推导公钥。"""
    scalar, _ = _expanded_seed(seed)
    return _encode(_multiply(BASE, scalar))


def sign(seed, message):
    """生成 RFC 8032 Ed25519 签名；此纯 Python 实现不是常量时间算法。"""
    scalar, prefix = _expanded_seed(seed)
    public = _encode(_multiply(BASE, scalar))
    nonce = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % L
    encoded_r = _encode(_multiply(BASE, nonce))
    challenge = int.from_bytes(hashlib.sha512(encoded_r + public + message).digest(), "little") % L
    s = (nonce + challenge * scalar) % L
    return encoded_r + s.to_bytes(32, "little")


def verify(public, message, signature):
    """验证 Ed25519 签名，拒绝非法编码或与消息不匹配的签名。"""
    if len(public) != 32 or len(signature) != 64:
        return False
    encoded_r, encoded_s = signature[:32], signature[32:]
    scalar = int.from_bytes(encoded_s, "little")
    if scalar >= L:
        return False
    try:
        point_a, point_r = _decode(public), _decode(encoded_r)
    except ValueError:
        return False
    if (_encode(_multiply(point_a, L)) != _encode(IDENTITY) or
            _encode(_multiply(point_r, L)) != _encode(IDENTITY) or
            _encode(_multiply(point_a, 8)) == _encode(IDENTITY) or
            _encode(_multiply(point_r, 8)) == _encode(IDENTITY)):
        return False
    challenge = int.from_bytes(hashlib.sha512(encoded_r + public + message).digest(), "little") % L
    return _encode(_multiply(BASE, scalar)) == _encode(_add(point_r, _multiply(point_a, challenge)))
