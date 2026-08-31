from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import pms


def _envelope(public_key, payload: dict[str, object], aad: str, kid: str) -> dict[str, object]:
    aes_key = os.urandom(32)
    iv = os.urandom(12)
    encrypted = AESGCM(aes_key).encrypt(
        iv,
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        aad.encode("utf-8"),
    )
    wrapped_key = public_key.encrypt(
        base64.b64encode(aes_key),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return {
        "version": 1,
        "kid": kid,
        "key_algorithm": "RSA-OAEP-SHA256",
        "content_algorithm": "AES-256-GCM",
        "wrapped_key": base64.b64encode(wrapped_key).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ciphertext": base64.b64encode(encrypted[:-16]).decode("ascii"),
        "tag": base64.b64encode(encrypted[-16:]).decode("ascii"),
    }


class PMSReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite+pysqlite:///{Path(self.tmp.name) / 'pms.db'}"
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pms.PMS_DATABASE_URL = self.database_url
        pms.PMS_DECRYPTION_PRIVATE_KEY = self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
        pms.PMS_DECRYPTION_KEY_ID = "morning-shift-test-key"
        pms._session_factory.cache_clear()
        pms._private_key.cache_clear()
        pms.PMSBase.metadata.create_all(pms._session_factory().kw["bind"])

    def tearDown(self) -> None:
        pms._session_factory().kw["bind"].dispose()
        pms._session_factory.cache_clear()
        pms._private_key.cache_clear()
        self.tmp.cleanup()

    def test_loads_only_latest_completed_snapshot_and_decrypts_in_memory(self) -> None:
        old_day = date(2026, 8, 26)
        current_day = date(2026, 8, 27)
        property_lookup = "a" * 64
        reservation_lookup = "b" * 64
        payload = {
            "property_code": "AMSEM",
            "confirmation_number": "1769812",
            "reservation_id": "705896",
            "number_of_rooms": 2,
        }
        envelope = _envelope(
            self.private_key.public_key(), payload,
            f"v1|pms|inhouse|{current_day.isoformat()}|{property_lookup}|{reservation_lookup}",
            "morning-shift-test-key",
        )
        with pms._session_factory()() as session:
            session.add_all(
                [
                    pms.PMSImportFile(id="old", report_type="inhouse", business_date=old_day, status="completed", completed_at=datetime(2026, 8, 26)),
                    pms.PMSImportFile(id="latest", report_type="inhouse", business_date=current_day, status="completed", completed_at=datetime(2026, 8, 27)),
                    pms.PMSInhouseObservation(id="row", snapshot_date=current_day, property_lookup=property_lookup, reservation_lookup=reservation_lookup, import_file_id="latest", payload_fingerprint="c" * 64, encrypted_payload=envelope),
                ]
            )
            session.commit()

        rows = pms.load_current_inhouse()
        self.assertEqual(rows, [(payload, reservation_lookup, "c" * 64)])

    def test_private_key_is_parsed_once_per_worker(self) -> None:
        with patch.object(pms.serialization, "load_pem_private_key", wraps=pms.serialization.load_pem_private_key) as loader:
            self.assertIs(pms._private_key(), pms._private_key())
        loader.assert_called_once()


if __name__ == "__main__":
    unittest.main()
