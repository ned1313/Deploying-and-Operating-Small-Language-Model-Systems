"""Module 6 shared pieces: the audit store, trace redaction, and client TLS."""

from __future__ import annotations

import datetime as dt
import sqlite3
import ssl

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from taco_shared.audit import AuditStore
from taco_shared.tls import ReloadingContext, TlsFiles, verify_arg
from taco_shared.tracing import REDACTED, redact


@pytest.fixture
def store(tmp_path) -> AuditStore:
    return AuditStore(tmp_path / "audit.db", Fernet.generate_key())


def test_content_is_encrypted_at_rest(store, tmp_path) -> None:
    event = store.append("intake", case_id="CASE-1", actor_id="rep-kop-01",
                         metadata={"store_id": "KOP"}, content={"complaint": "My two Carnitas tacos were missing"})
    raw = b"".join(path.read_bytes() for path in tmp_path.glob("audit.db*"))  # WAL mode: the -wal file too
    assert b"Carnitas" not in raw and b"KOP" in raw
    assert store.content(event["content_id"]) == {"complaint": "My two Carnitas tacos were missing"}
    assert AuditStore(tmp_path / "audit.db", Fernet.generate_key()).content(event["content_id"]) == {
        "error": "content cannot be decrypted with the configured key"}


def test_chain_detects_edits_and_deletions(store, tmp_path) -> None:
    for n in range(3):
        store.append("tool_access", case_id="CASE-1", metadata={"n": n})
    assert store.verify() == {"ok": True, "events": 3, "head": store.events()[-1]["hash"]}
    with sqlite3.connect(tmp_path / "audit.db") as connection:
        connection.execute("DELETE FROM events WHERE seq = 2")
    assert store.verify()["first_bad_seq"] == 3


def test_deleting_content_keeps_the_chain(store) -> None:
    kept = store.append("intake", case_id="CASE-2", content={"complaint": "x"})
    gone = store.append("intake", case_id="CASE-1", content={"complaint": "y"})
    assert store.delete_case_content("CASE-1") == 1
    assert store.content(gone["content_id"]) is None and store.content(kept["content_id"]) == {"complaint": "x"}
    assert store.verify()["ok"] is True


def test_cursor_round_trip(store) -> None:
    assert store.cursor("t1") == (0, 0)
    store.set_cursor("t1", 7, 4)
    assert store.cursor("t1") == (7, 4)


def test_redaction_keeps_structure_and_codes() -> None:
    inputs = {"messages": [{"role": "user", "content": "My order TA-10431 was missing two tacos"},
                           {"role": "tool", "name": "get_order",
                            "tool_calls": [{"name": "get_order", "args": {"order_id": "TA-10431"}}]}],
              "intake": {"case_id": "CASE-10431", "customer_name": "Ana", "complaint": "short"},
              "proposal": {"refund_amount": 8.5, "customer_message": "Sorry", "policy_rule_ids": ["R1"]}}
    out = redact(inputs)
    assert out["messages"][0] == {"role": "user", "content": REDACTED}
    assert out["messages"][1]["tool_calls"][0] == {"name": "get_order", "args": {"order_id": "TA-10431"}}
    assert out["intake"] == {"case_id": "CASE-10431", "customer_name": REDACTED, "complaint": REDACTED}
    assert out["proposal"] == {"refund_amount": 8.5, "customer_message": REDACTED, "policy_rule_ids": ["R1"]}


def _write_cert(directory, cn: str) -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now)
            .not_valid_after(now + dt.timedelta(days=1)).sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "tls.crt", directory / "tls.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return str(cert_path), str(key_path)


def test_tls_is_off_without_settings(monkeypatch) -> None:
    for name in ("INFERENCE_CA_FILE", "INFERENCE_CLIENT_CERT_FILE", "INFERENCE_CLIENT_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert TlsFiles.from_env().enabled is False and verify_arg() is True


def test_reloading_context_picks_up_a_rotated_certificate(tmp_path) -> None:
    cert, key = _write_cert(tmp_path, "complaint-api")
    files = TlsFiles(ca_file=cert, cert_file=cert, key_file=key)
    reloading = ReloadingContext(files)
    assert isinstance(reloading.context, ssl.SSLContext)
    first = reloading._stamp
    import os
    import time
    _write_cert(tmp_path, "complaint-api")
    later = time.time() + 5
    os.utime(cert, (later, later))
    reloading.refresh()
    assert reloading._stamp != first
    assert files.requests_args() == {"verify": cert, "cert": (cert, key)}
