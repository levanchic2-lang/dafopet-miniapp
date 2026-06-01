"""企业微信回调消息加解密 + 签名校验。

官方协议参考：https://developer.work.weixin.qq.com/document/path/90968

URL 验证流程（GET）：
  企微发：msg_signature, timestamp, nonce, echostr
  我们：用 token+timestamp+nonce+echostr 排序 sha1 → 对比 msg_signature
  通过则 AES 解密 echostr → 返回明文（去掉 16 字节随机 + 4 字节长度 + corpid）

消息回调（POST）：
  body 是 XML，<Encrypt>base64 密文</Encrypt>
  query 带 msg_signature timestamp nonce
  解密后 XML：{MsgType, FromUserName, Content / Recognition / MediaId, ...}
"""
from __future__ import annotations
import base64
import hashlib
import os
import struct
import socket
import xml.etree.ElementTree as ET
from Crypto.Cipher import AES


class WXBizMsgCryptError(Exception):
    pass


def _sha1_sign(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    arr = sorted([token, timestamp, nonce, encrypt])
    s = "".join(arr)
    return hashlib.sha1(s.encode()).hexdigest()


def _pkcs7_unpad(b: bytes) -> bytes:
    n = b[-1]
    if n < 1 or n > 32:
        return b
    return b[:-n]


def _pkcs7_pad(b: bytes, block: int = 32) -> bytes:
    n = block - (len(b) % block)
    if n == 0:
        n = block
    return b + bytes([n] * n)


def _aes_key(aes_key_b64: str) -> bytes:
    """EncodingAESKey 是 43 字符，加个 '=' 变成 base64 → 32 字节 key。"""
    return base64.b64decode(aes_key_b64 + "=")


def decrypt_msg(encrypt_b64: str, aes_key_b64: str, corp_id: str) -> str:
    """AES-256-CBC 解密 → 验证 corpid → 返回明文 XML（或 echostr 明文）。"""
    key = _aes_key(aes_key_b64)
    iv = key[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    plain = cipher.decrypt(base64.b64decode(encrypt_b64))
    plain = _pkcs7_unpad(plain)
    # 结构：16B 随机 + 4B 大端长度 + 数据 + receiveid
    if len(plain) < 20:
        raise WXBizMsgCryptError("decrypt: payload too short")
    content_len = struct.unpack(">I", plain[16:20])[0]
    if content_len < 0 or 20 + content_len > len(plain):
        raise WXBizMsgCryptError("decrypt: invalid length field")
    content = plain[20:20 + content_len].decode("utf-8")
    receive_id = plain[20 + content_len:].decode("utf-8")
    if corp_id and receive_id != corp_id:
        raise WXBizMsgCryptError(f"decrypt: corp_id mismatch (got {receive_id!r})")
    return content


def encrypt_msg(plain_xml: str, aes_key_b64: str, corp_id: str) -> str:
    """加密回包（被动响应消息时用）。本 MVP 主要靠主动推送 send_app_message，
    回调里只返回空字符串即可。此函数留备份。"""
    key = _aes_key(aes_key_b64)
    iv = key[:16]
    random16 = os.urandom(16)
    content_bytes = plain_xml.encode("utf-8")
    length_bytes = struct.pack(">I", len(content_bytes))
    raw = random16 + length_bytes + content_bytes + corp_id.encode("utf-8")
    raw = _pkcs7_pad(raw, 32)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(cipher.encrypt(raw)).decode()


def verify_url(token: str, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> bool:
    """URL 验证：检查签名是否匹配。"""
    expected = _sha1_sign(token, timestamp, nonce, echostr)
    return expected == msg_signature


def verify_msg(token: str, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
    """消息体签名校验。"""
    expected = _sha1_sign(token, timestamp, nonce, encrypt)
    return expected == msg_signature


def parse_inbound_xml(plain_xml: str) -> dict:
    """把解密后的 XML 解析成 dict。
    关键字段：MsgType (text/voice/...), FromUserName (wecom userid),
    Content (text)、Recognition (voice→text)、MsgId, CreateTime
    """
    root = ET.fromstring(plain_xml)
    out: dict = {}
    for child in root:
        # ElementTree text 已经处理 CDATA
        out[child.tag] = (child.text or "").strip()
    return out


def parse_encrypt_envelope(body_xml: str) -> str:
    """从 <xml><Encrypt>...</Encrypt></xml> 拿到加密体。"""
    root = ET.fromstring(body_xml)
    enc = root.find("Encrypt")
    if enc is None or not (enc.text or "").strip():
        raise WXBizMsgCryptError("no <Encrypt> in envelope")
    return enc.text.strip()
