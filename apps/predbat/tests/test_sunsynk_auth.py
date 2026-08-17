# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk Cloud authentication
# -----------------------------------------------------------------------------

"""Tests for Sunsynk login: the pure-Python RSA helper and the three auth methods."""

import base64
from sunsynk_const import parse_rsa_public_key, rsa_encrypt_pkcs1v15

# A real 1024-bit RSA public key, DER SubjectPublicKeyInfo, base64 — the same shape
# Sunsynk's /anonymous/publicKey returns (no PEM armour).
SUNSYNK_TEST_PUBLIC_KEY = "MIGeMA0GCSqGSIb3DQEBAQUAA4GMADCBiAKBgFwp+M48x3PUYA63ZF2xEl4pFrh+1qQuk4B0UeTKCAqU51A8BURJdRs4ECXJEdJnxgO3hlkJyjVgBaeJgajxTu+c1oyOtQn9KVvW" "/Se0LEytkZRABnOsJkGprKWuNDm6N5YXPEH5yfnAfCL7Drnsn8rj3RjPmkzCg8XHI6xHGQD3AgMBAAE="
SUNSYNK_TEST_MODULUS = 0x5C29F8CE3CC773D4600EB7645DB1125E2916B87ED6A42E93807451E4CA080A94E7503C054449751B381025C911D267C603B7865909CA356005A78981A8F14EEF9CD68C8EB509FD295BD6FD27B42C4CAD9194400673AC2641A9ACA5AE3439BA3796173C41F9C9F9C07C22FB0EB9EC9FCAE3DD18CF9A4CC283C5C723AC471900F7
# Private exponent, used ONLY by this test to decrypt and inspect the padding.
SUNSYNK_TEST_PRIVATE_D = 0x438FD67F5964328527E5A1E046CE87A87F2128C927E53394ED95AD1DB5A784C4F8CCD888593180521E71B7EC0379E54398CB4606AA2691A4D28053F7B8E12CA643EC0257950C49747469A6092B548F9358DCBD311FC69088457B4A76213C07F7937C8745144F9F8EF7DA792DA35AA4FB5E5458B6A36ACDAD3327C4066AF8A01
SUNSYNK_TEST_KEY_BYTES = 128


def _decrypt(ciphertext_b64):
    """Decrypt with the test private key, returning the raw padded encryption block."""
    raw = base64.b64decode(ciphertext_b64)
    plain = pow(int.from_bytes(raw, "big"), SUNSYNK_TEST_PRIVATE_D, SUNSYNK_TEST_MODULUS)
    return plain.to_bytes(SUNSYNK_TEST_KEY_BYTES, "big")


def test_parse_rsa_public_key():
    """The DER SubjectPublicKeyInfo parser recovers the exact modulus and exponent."""
    failed = False
    modulus, exponent = parse_rsa_public_key(base64.b64decode(SUNSYNK_TEST_PUBLIC_KEY))
    if modulus != SUNSYNK_TEST_MODULUS:
        print(f"ERROR: modulus mismatch, got {hex(modulus)}")
        failed = True
    if exponent != 65537:
        print(f"ERROR: exponent expected 65537, got {exponent}")
        failed = True
    assert not failed, "test_parse_rsa_public_key"


def test_parse_rsa_public_key_rejects_rubbish():
    """A body that is not a DER key raises rather than returning a bogus modulus.

    The near-full-length truncations (``[:-1]``, ``[:-2]``, ``[:-3]``) are here because a
    truncation that only clips the last one to three bytes of the exponent still parses
    every earlier TLV correctly, so it exercises a different failure path from an
    early/gross truncation: without a bounds check in ``_read_tlv``, Python's silent
    slice truncation lets these "succeed" with a corrupted exponent (0, 1 or 256 for
    this key) instead of raising — and an exponent of 1 would send the password
    effectively unencrypted.
    """
    failed = False
    full_key = base64.b64decode(SUNSYNK_TEST_PUBLIC_KEY)
    cases = [
        ("empty", b""),
        ("not a sequence", b"\x02\x01\x05"),
        ("truncated", full_key[:20]),
        ("truncated by 1 byte", full_key[:-1]),
        ("truncated by 2 bytes", full_key[:-2]),
        ("truncated by 3 bytes", full_key[:-3]),
    ]
    for name, payload in cases:
        try:
            modulus, exponent = parse_rsa_public_key(payload)
            print(f"ERROR: {name} was accepted as a public key (modulus={hex(modulus)}, exponent={exponent})")
            failed = True
        except (ValueError, IndexError):
            pass
    assert not failed, "test_parse_rsa_public_key_rejects_rubbish"


def test_rsa_encrypt_round_trip():
    """Encryption produces a well-formed PKCS#1 v1.5 type-2 block recovering the password."""
    failed = False
    for password in ("hunter2", "a", "x" * 117, "pa55 w0rd! £é"):
        block = _decrypt(rsa_encrypt_pkcs1v15(SUNSYNK_TEST_PUBLIC_KEY, password))
        if block[0] != 0x00 or block[1] != 0x02:
            print(f"ERROR: {password!r} block header {block[:2].hex()} is not 0002")
            failed = True
            continue
        separator = block.index(b"\x00", 2)
        padding = block[2:separator]
        if len(padding) < 8:
            print(f"ERROR: {password!r} padding only {len(padding)} bytes, PKCS#1 requires >= 8")
            failed = True
        if not all(padding):
            print(f"ERROR: {password!r} padding contains a zero byte, which truncates the message")
            failed = True
        recovered = block[separator + 1 :].decode("utf-8")
        if recovered != password:
            print(f"ERROR: {password!r} round-tripped to {recovered!r}")
            failed = True
    assert not failed, "test_rsa_encrypt_round_trip"


def test_rsa_encrypt_is_randomised():
    """The same password encrypts differently each time, as PKCS#1 v1.5 requires."""
    failed = False
    first = rsa_encrypt_pkcs1v15(SUNSYNK_TEST_PUBLIC_KEY, "hunter2")
    second = rsa_encrypt_pkcs1v15(SUNSYNK_TEST_PUBLIC_KEY, "hunter2")
    if first == second:
        print("ERROR: identical ciphertexts, padding is not randomised")
        failed = True
    if len(base64.b64decode(first)) != SUNSYNK_TEST_KEY_BYTES:
        print(f"ERROR: ciphertext is {len(base64.b64decode(first))} bytes, expected {SUNSYNK_TEST_KEY_BYTES}")
        failed = True
    assert not failed, "test_rsa_encrypt_is_randomised"


def test_rsa_encrypt_rejects_oversize_password():
    """A password too long for the key is refused rather than silently truncated."""
    failed = False
    try:
        rsa_encrypt_pkcs1v15(SUNSYNK_TEST_PUBLIC_KEY, "x" * 118)
        print("ERROR: oversize password was accepted")
        failed = True
    except ValueError:
        pass
    assert not failed, "test_rsa_encrypt_rejects_oversize_password"


def run_sunsynk_auth_tests(my_predbat):
    """Run all Sunsynk authentication tests."""
    failed = False
    for name, fn in [
        ("parse_public_key", test_parse_rsa_public_key),
        ("parse_rejects_rubbish", test_parse_rsa_public_key_rejects_rubbish),
        ("encrypt_round_trip", test_rsa_encrypt_round_trip),
        ("encrypt_randomised", test_rsa_encrypt_is_randomised),
        ("encrypt_oversize", test_rsa_encrypt_rejects_oversize_password),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_auth.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_auth.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
