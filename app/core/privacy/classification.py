"""Data classification model (master prompt §10)."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class DataClass(StrEnum):
    """Classification applied to every field that can reach a log, trace or model."""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    PII = "PII"
    SENSITIVE_PII = "SENSITIVE_PII"
    SECRET = "SECRET"


class DataSensitivity(IntEnum):
    """Ordered sensitivity so policies can express 'at or above' rules."""

    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    PII = 3
    SENSITIVE_PII = 4
    SECRET = 5


SENSITIVITY: dict[DataClass, DataSensitivity] = {
    DataClass.PUBLIC: DataSensitivity.PUBLIC,
    DataClass.INTERNAL: DataSensitivity.INTERNAL,
    DataClass.CONFIDENTIAL: DataSensitivity.CONFIDENTIAL,
    DataClass.PII: DataSensitivity.PII,
    DataClass.SENSITIVE_PII: DataSensitivity.SENSITIVE_PII,
    DataClass.SECRET: DataSensitivity.SECRET,
}

#: Field-name -> classification. Used by the log redactor and the model boundary.
FIELD_CLASSIFICATION: dict[str, DataClass] = {
    # secrets
    "password": DataClass.SECRET,
    "passwd": DataClass.SECRET,
    "otp": DataClass.SECRET,
    "pin": DataClass.SECRET,
    "access_token": DataClass.SECRET,
    "accesstoken": DataClass.SECRET,
    "refresh_token": DataClass.SECRET,
    "refreshtoken": DataClass.SECRET,
    "id_token": DataClass.SECRET,
    "authorization": DataClass.SECRET,
    "api_key": DataClass.SECRET,
    "apikey": DataClass.SECRET,
    "secret": DataClass.SECRET,
    "client_secret": DataClass.SECRET,
    "private_key": DataClass.SECRET,
    "encryption_key": DataClass.SECRET,
    "cvv": DataClass.SECRET,
    "card_number": DataClass.SECRET,
    "cardnumber": DataClass.SECRET,
    "bearer": DataClass.SECRET,
    "jwt": DataClass.SECRET,
    "session_secret": DataClass.SECRET,
    # sensitive personal data
    "aadhaar": DataClass.SENSITIVE_PII,
    "aadhar": DataClass.SENSITIVE_PII,
    "health_condition": DataClass.SENSITIVE_PII,
    "medical_history": DataClass.SENSITIVE_PII,
    "diagnosis": DataClass.SENSITIVE_PII,
    "pre_existing_disease": DataClass.SENSITIVE_PII,
    "bank_account": DataClass.SENSITIVE_PII,
    "account_number": DataClass.SENSITIVE_PII,
    "ifsc": DataClass.SENSITIVE_PII,
    # personal data
    "name": DataClass.PII,
    "full_name": DataClass.PII,
    "first_name": DataClass.PII,
    "last_name": DataClass.PII,
    "mobile": DataClass.PII,
    "phone": DataClass.PII,
    "phone_number": DataClass.PII,
    "email": DataClass.PII,
    "address": DataClass.PII,
    "pincode": DataClass.PII,
    "dob": DataClass.PII,
    "date_of_birth": DataClass.PII,
    "pan": DataClass.PII,
    "gstin": DataClass.PII,
    "registration_number": DataClass.PII,
    "vehicle_number": DataClass.PII,
    "chassis_number": DataClass.PII,
    "engine_number": DataClass.PII,
    "passport_number": DataClass.PII,
    "driving_licence": DataClass.PII,
    "customer_id": DataClass.PII,
    "customerid": DataClass.PII,
    # confidential business data
    "policy_number": DataClass.CONFIDENTIAL,
    "policynumber": DataClass.CONFIDENTIAL,
    "claim_number": DataClass.CONFIDENTIAL,
    "quote_id": DataClass.CONFIDENTIAL,
    "payment_reference": DataClass.CONFIDENTIAL,
}

#: Never written to any log, trace, audit record or model context under any condition.
NEVER_LOG_FIELDS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "otp",
        "pin",
        "access_token",
        "accesstoken",
        "refresh_token",
        "refreshtoken",
        "id_token",
        "authorization",
        "api_key",
        "apikey",
        "secret",
        "client_secret",
        "private_key",
        "encryption_key",
        "cvv",
        "card_number",
        "cardnumber",
        "bearer",
        "jwt",
        "session_secret",
    }
)

#: Classifications that must never cross the model boundary (§10.3).
FORBIDDEN_IN_MODEL_CONTEXT: frozenset[DataClass] = frozenset({DataClass.SECRET})


def classify_field(field_name: str) -> DataClass:
    """Classify a field by name, defaulting to INTERNAL when unknown."""
    normalized = field_name.strip().lower().replace("-", "_")
    if normalized in FIELD_CLASSIFICATION:
        return FIELD_CLASSIFICATION[normalized]
    compact = normalized.replace("_", "")
    if compact in FIELD_CLASSIFICATION:
        return FIELD_CLASSIFICATION[compact]
    for known, klass in FIELD_CLASSIFICATION.items():
        if known in normalized:
            return klass
    return DataClass.INTERNAL


def is_never_loggable(field_name: str) -> bool:
    normalized = field_name.strip().lower().replace("-", "_")
    compact = normalized.replace("_", "")
    return normalized in NEVER_LOG_FIELDS or compact in NEVER_LOG_FIELDS


def at_least(value: DataClass, threshold: DataClass) -> bool:
    return SENSITIVITY[value] >= SENSITIVITY[threshold]
