"""小程序客服消息推送的签名和 AES 解密。

明文模式：signature = sha1(sort(token, timestamp, nonce))，原样回 echostr。
安全模式：msg_signature = sha1(sort(token, timestamp, nonce, encrypt))，
EncodingAESKey 为 43 位，AES-256-CBC，IV 取密钥前 16 字节，PKCS#7 块长 32。
明文结构：16 字节随机数 + 4 字节大端长度 + 内容 + appid。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import xml.etree.ElementTree as ET

from Crypto.Cipher import AES


class WeChatCsCryptoError(Exception):
    pass


def sha1_sign(*parts: str) -> str:
    raw = "".join(sorted(str(part) for part in parts))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def signatures_match(expected: str, provided: str) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected, provided.strip())


def plain_signature(token: str, timestamp: str, nonce: str) -> str:
    return sha1_sign(token, timestamp, nonce)


def secure_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    return sha1_sign(token, timestamp, nonce, encrypt)


def _aes_key(aes_key_b64: str) -> bytes:
    key = (aes_key_b64 or "").strip()
    if len(key) != 43:
        raise WeChatCsCryptoError("EncodingAESKey 须为 43 位")
    try:
        raw = base64.b64decode(key + "=")
    except Exception as exc:
        raise WeChatCsCryptoError("EncodingAESKey 无法解码") from exc
    if len(raw) != 32:
        raise WeChatCsCryptoError("EncodingAESKey 解码后不是 32 字节")
    return raw


def _pkcs7_pad(data: bytes, block: int = 32) -> bytes:
    pad = block - (len(data) % block)
    if pad == 0:
        pad = block
    return data + bytes([pad] * pad)


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise WeChatCsCryptoError("解密结果为空")
    pad = data[-1]
    if pad < 1 or pad > 32 or data[-pad:] != bytes([pad] * pad):
        raise WeChatCsCryptoError("PKCS#7 填充不正确")
    return data[:-pad]


def encrypt_text(plain: str, aes_key_b64: str, appid: str) -> str:
    key = _aes_key(aes_key_b64)
    content = (plain or "").encode("utf-8")
    raw = os.urandom(16) + struct.pack(">I", len(content)) + content + (appid or "").encode("utf-8")
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    return base64.b64encode(cipher.encrypt(_pkcs7_pad(raw))).decode("ascii")


def decrypt_text(encrypt_b64: str, aes_key_b64: str, appid: str) -> str:
    key = _aes_key(aes_key_b64)
    try:
        encrypted = base64.b64decode(encrypt_b64)
    except Exception as exc:
        raise WeChatCsCryptoError("密文不是合法 Base64") from exc
    if not encrypted or len(encrypted) % 16 != 0:
        raise WeChatCsCryptoError("密文长度不正确")
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    plain = _pkcs7_unpad(cipher.decrypt(encrypted))
    if len(plain) < 20:
        raise WeChatCsCryptoError("解密内容过短")
    content_len = struct.unpack(">I", plain[16:20])[0]
    if content_len < 0 or 20 + content_len > len(plain):
        raise WeChatCsCryptoError("解密长度字段不正确")
    content = plain[20:20 + content_len].decode("utf-8")
    receive_id = plain[20 + content_len:].decode("utf-8")
    expected = (appid or "").strip()
    if not expected:
        raise WeChatCsCryptoError("未配置小程序 AppId，无法核对安全模式")
    if receive_id != expected:
        raise WeChatCsCryptoError("AppId 不匹配")
    return content


def parse_xml_map(body: str) -> dict:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise WeChatCsCryptoError("XML 无法解析") from exc
    out: dict = {}
    for child in list(root):
        out[child.tag] = (child.text or "").strip()
    return out


def parse_message_text(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        raise WeChatCsCryptoError("消息为空")
    if raw.startswith("<"):
        return parse_xml_map(raw)
    if raw.startswith("{") or raw.startswith("["):
        import json
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WeChatCsCryptoError("JSON 无法解析") from exc
        if not isinstance(data, dict):
            raise WeChatCsCryptoError("消息不是对象")
        return data
    raise WeChatCsCryptoError("消息既不是 JSON 也不是 XML")
