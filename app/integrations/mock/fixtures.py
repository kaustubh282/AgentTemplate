"""Deterministic mock fixtures (master prompt §9.1).

Fixture identifiers are stable so tests and evals can assert on them. All data is
synthetic: no real customer information appears anywhere in this repository.
"""

from __future__ import annotations

from datetime import date

from app.integrations.contracts.dtos import (
    Claim,
    ClaimStatus,
    Customer,
    Money,
    PaymentRecord,
    PaymentStatus,
    Policy,
    PolicyStatus,
    Product,
    ReferenceItem,
)

CUSTOMER_A = "CUST-1001"
CUSTOMER_B = "CUST-2002"
AGENT_ASSIGNED_CUSTOMER = CUSTOMER_A

CUSTOMERS: dict[str, Customer] = {
    CUSTOMER_A: Customer(
        customer_id=CUSTOMER_A,
        full_name="Asha Verma",
        mobile="9876543210",
        email="asha.verma@example.com",
        date_of_birth=date(1990, 4, 12),
        pincode="400001",
        tenant_id="TENANT-IN",
    ),
    CUSTOMER_B: Customer(
        customer_id=CUSTOMER_B,
        full_name="Rohit Nair",
        mobile="9812345678",
        email="rohit.nair@example.com",
        date_of_birth=date(1985, 9, 3),
        pincode="560001",
        tenant_id="TENANT-IN",
    ),
}

POLICY_A_MOTOR = "POL-MTR-0001"
POLICY_A_TRAVEL = "POL-TRV-0003"
POLICY_B_MOTOR = "POL-MTR-0002"

POLICIES: dict[str, Policy] = {
    POLICY_A_MOTOR: Policy(
        policy_id=POLICY_A_MOTOR,
        policy_number="PTC0000001234",
        customer_id=CUSTOMER_A,
        product="Private Car Package",
        domain="motor",
        status=PolicyStatus.ACTIVE,
        annual_premium=Money(amount=18450.0),
        addons=["Zero Depreciation", "Roadside Assistance"],
        inception_date=date(2026, 1, 15),
        renewal_due=date(2027, 1, 15),
        sum_insured=Money(amount=650000.0),
    ),
    POLICY_A_TRAVEL: Policy(
        policy_id=POLICY_A_TRAVEL,
        policy_number="PTC0000005678",
        customer_id=CUSTOMER_A,
        product="Overseas Travel Secure",
        domain="travel",
        status=PolicyStatus.ACTIVE,
        annual_premium=Money(amount=3200.0),
        addons=["Baggage Loss"],
        inception_date=date(2026, 6, 1),
        renewal_due=date(2027, 6, 1),
        sum_insured=Money(amount=4000000.0),
    ),
    POLICY_B_MOTOR: Policy(
        policy_id=POLICY_B_MOTOR,
        policy_number="PTC0000009999",
        customer_id=CUSTOMER_B,
        product="Two Wheeler Package",
        domain="motor",
        status=PolicyStatus.ACTIVE,
        annual_premium=Money(amount=4100.0),
        addons=[],
        inception_date=date(2026, 3, 10),
        renewal_due=date(2027, 3, 10),
        sum_insured=Money(amount=95000.0),
    ),
}

CLAIM_A = "CLM-0001"
CLAIM_B = "CLM-0002"

CLAIMS: dict[str, Claim] = {
    CLAIM_A: Claim(
        claim_id=CLAIM_A,
        claim_number="CLMPTC000111",
        policy_id=POLICY_A_MOTOR,
        customer_id=CUSTOMER_A,
        status=ClaimStatus.UNDER_REVIEW,
        registered_on=date(2026, 7, 2),
        last_updated_on=date(2026, 7, 9),
        estimated_amount=Money(amount=24500.0),
    ),
    CLAIM_B: Claim(
        claim_id=CLAIM_B,
        claim_number="CLMPTC000222",
        policy_id=POLICY_B_MOTOR,
        customer_id=CUSTOMER_B,
        status=ClaimStatus.SETTLED,
        registered_on=date(2026, 2, 11),
        last_updated_on=date(2026, 3, 1),
        estimated_amount=Money(amount=8100.0),
    ),
}

PAYMENT_A = "PAY-0001"

PAYMENTS: dict[str, PaymentRecord] = {
    PAYMENT_A: PaymentRecord(
        payment_id=PAYMENT_A,
        payment_reference="PAYREF00012345",
        customer_id=CUSTOMER_A,
        quote_id=None,
        amount=Money(amount=18450.0),
        status=PaymentStatus.SUCCESS,
    ),
}

PRODUCTS: dict[str, Product] = {
    "MTR-PVT-CAR": Product(
        product_code="MTR-PVT-CAR",
        name="Private Car Package",
        domain="motor",
        available_addons=["Zero Depreciation", "Roadside Assistance", "Engine Protect"],
        min_age=18,
        max_age=80,
    ),
    "MTR-TW": Product(
        product_code="MTR-TW",
        name="Two Wheeler Package",
        domain="motor",
        available_addons=["Zero Depreciation", "Roadside Assistance"],
        min_age=18,
        max_age=80,
    ),
    "TRV-OVERSEAS": Product(
        product_code="TRV-OVERSEAS",
        name="Overseas Travel Secure",
        domain="travel",
        available_addons=["Baggage Loss", "Trip Cancellation", "Adventure Sports"],
        min_age=1,
        max_age=70,
    ),
}

REFERENCE_DATA: dict[str, list[ReferenceItem]] = {
    "motor_fuel_type": [
        ReferenceItem(code="PETROL", label="Petrol", group="motor_fuel_type"),
        ReferenceItem(code="DIESEL", label="Diesel", group="motor_fuel_type"),
        ReferenceItem(code="CNG", label="CNG", group="motor_fuel_type"),
        ReferenceItem(code="EV", label="Electric", group="motor_fuel_type"),
    ],
    "travel_region": [
        ReferenceItem(code="ASIA", label="Asia (excluding Japan)", group="travel_region"),
        ReferenceItem(code="SCHENGEN", label="Schengen", group="travel_region"),
        ReferenceItem(code="WORLDWIDE", label="Worldwide", group="travel_region"),
    ],
}

#: Base premium assumptions used only by the mock rating stub. REQUIRES_VERIFICATION -
#: real rating must come from the authoritative rating engine / InsureMO.
MOCK_BASE_PREMIUM: dict[str, float] = {
    "MTR-PVT-CAR": 14500.0,
    "MTR-TW": 3200.0,
    "TRV-OVERSEAS": 2400.0,
}

MOCK_ADDON_PREMIUM: dict[str, float] = {
    "Zero Depreciation": 2800.0,
    "Roadside Assistance": 850.0,
    "Engine Protect": 1600.0,
    "Baggage Loss": 400.0,
    "Trip Cancellation": 650.0,
    "Adventure Sports": 900.0,
}
