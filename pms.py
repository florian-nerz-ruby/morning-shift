"""Read-only encrypted PMS database client for Morning Shift."""

from __future__ import annotations

import base64
import json
import os
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Mapping

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import JSON, Date, DateTime, String, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class PMSReadError(RuntimeError):
    """Raised without including encrypted payloads or identifiers."""


PMS_DATABASE_URL = os.environ.get("PMS_DATABASE_URL", "").strip()
PMS_DECRYPTION_PRIVATE_KEY = os.environ.get("PMS_DECRYPTION_PRIVATE_KEY", "").replace("\\n", "\n")
PMS_DECRYPTION_KEY_ID = os.environ.get("PMS_DECRYPTION_KEY_ID", "").strip()


class PMSBase(DeclarativeBase):
    pass


class PMSImportFile(PMSBase):
    __tablename__ = "pms_import_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_type: Mapped[str] = mapped_column(String(32))
    business_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PMSInhouseObservation(PMSBase):
    __tablename__ = "pms_inhouse_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date)
    property_lookup: Mapped[str] = mapped_column(String(64))
    reservation_lookup: Mapped[str] = mapped_column(String(64))
    import_file_id: Mapped[str] = mapped_column(String(36))
    payload_fingerprint: Mapped[str] = mapped_column(String(64))
    encrypted_payload: Mapped[dict[str, object]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))


class PMSCancellationEvent(PMSBase):
    __tablename__ = "pms_cancellation_events"

    event_lookup: Mapped[str] = mapped_column(String(64), primary_key=True)
    property_lookup: Mapped[str] = mapped_column(String(64))
    reservation_lookup: Mapped[str] = mapped_column(String(64))
    cancellation_date: Mapped[date] = mapped_column(Date)
    payload_fingerprint: Mapped[str] = mapped_column(String(64))
    encrypted_payload: Mapped[dict[str, object]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))


def _configured() -> bool:
    return bool(PMS_DATABASE_URL and PMS_DECRYPTION_PRIVATE_KEY)


@lru_cache
def _session_factory() -> sessionmaker:
    if not _configured():
        raise PMSReadError("PMS reader is not configured")
    connect_args = {"check_same_thread": False} if PMS_DATABASE_URL.startswith("sqlite") else {}
    return sessionmaker(bind=create_engine(PMS_DATABASE_URL, pool_pre_ping=True, connect_args=connect_args), expire_on_commit=False)


@lru_cache
def _private_key() -> rsa.RSAPrivateKey:
    if not _configured():
        raise PMSReadError("PMS reader is not configured")
    try:
        key = serialization.load_pem_private_key(PMS_DECRYPTION_PRIVATE_KEY.encode("utf-8"), password=None)
    except (TypeError, ValueError) as error:
        raise PMSReadError("PMS decryption private key is invalid") from error
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise PMSReadError("PMS decryption private key is invalid")
    return key


def _decode(envelope: Mapping[str, object], *, aad: str) -> dict[str, object]:
    try:
        if PMS_DECRYPTION_KEY_ID and envelope.get("kid") != PMS_DECRYPTION_KEY_ID:
            raise ValueError
        wrapped_key = base64.b64decode(str(envelope["wrapped_key"]), validate=True)
        encoded_aes_key = _private_key().decrypt(
            wrapped_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None
            ),
        )
        aes_key = base64.b64decode(encoded_aes_key, validate=True)
        ciphertext = base64.b64decode(str(envelope["ciphertext"]), validate=True)
        tag = base64.b64decode(str(envelope["tag"]), validate=True)
        iv = base64.b64decode(str(envelope["iv"]), validate=True)
        payload = AESGCM(aes_key).decrypt(iv, ciphertext + tag, aad.encode("utf-8"))
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError
        return decoded
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise PMSReadError("PMS encrypted record could not be decrypted safely") from error


def _inhouse_aad(row: PMSInhouseObservation) -> str:
    return (
        f"v1|pms|inhouse|{row.snapshot_date.isoformat()}|{row.property_lookup}|"
        f"{row.reservation_lookup}"
    )


def _cancellation_aad(row: PMSCancellationEvent) -> str:
    return (
        f"v1|pms|cancellation|{row.cancellation_date.isoformat()}|{row.property_lookup}|"
        f"{row.reservation_lookup}|{row.event_lookup}"
    )


@lru_cache
def _load_inhouse_import(
    import_file_id: str, snapshot_date: date
) -> tuple[tuple[dict[str, object], str, str], ...]:
    """Decrypt one immutable completed snapshot once per worker.

    The import-file id is part of the cache key, so a successful PMS upload
    automatically selects a new cache entry without retaining source data on
    disk or changing the encrypted database.
    """

    factory = _session_factory()
    with factory() as session:
        rows = session.scalars(
            select(PMSInhouseObservation).where(
                PMSInhouseObservation.snapshot_date == snapshot_date,
                PMSInhouseObservation.import_file_id == import_file_id,
            )
        ).all()
        return tuple(
            (_decode(row.encrypted_payload, aad=_inhouse_aad(row)), row.reservation_lookup, row.payload_fingerprint)
            for row in rows
        )


def load_current_inhouse() -> list[tuple[dict[str, object], str, str]]:
    """Return decrypted latest-snapshot payloads plus opaque state identifiers."""

    factory = _session_factory()
    with factory() as session:
        import_file = session.scalar(
            select(PMSImportFile)
            .where(PMSImportFile.report_type == "inhouse", PMSImportFile.status == "completed")
            .order_by(PMSImportFile.business_date.desc(), PMSImportFile.completed_at.desc())
            .limit(1)
        )
        if import_file is None:
            return []
        import_file_id = import_file.id
        snapshot_date = import_file.business_date
    return list(_load_inhouse_import(import_file_id, snapshot_date))


@lru_cache
def _load_cancellation_import(import_file_id: str) -> tuple[tuple[dict[str, object], str, str], ...]:
    """Decrypt cancellation history once per worker for a completed import."""

    factory = _session_factory()
    with factory() as session:
        rows = session.scalars(
            select(PMSCancellationEvent).order_by(PMSCancellationEvent.cancellation_date.desc())
        ).all()
        return tuple(
            (_decode(row.encrypted_payload, aad=_cancellation_aad(row)), row.event_lookup, row.payload_fingerprint)
            for row in rows
        )


def load_cancellation_events() -> list[tuple[dict[str, object], str, str]]:
    factory = _session_factory()
    with factory() as session:
        import_file = session.scalar(
            select(PMSImportFile)
            .where(PMSImportFile.report_type == "cancellation", PMSImportFile.status == "completed")
            .order_by(PMSImportFile.completed_at.desc())
            .limit(1)
        )
        if import_file is None:
            return []
        import_file_id = import_file.id
    return list(_load_cancellation_import(import_file_id))
