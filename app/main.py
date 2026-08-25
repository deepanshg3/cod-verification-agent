import os
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
NOMINATIM_USER_AGENT = os.getenv(
    "NOMINATIM_USER_AGENT",
    "CODVerificationAgent/1.0"
)

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is not configured")

if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not configured")


app = FastAPI(
    title="COD Order Verification API",
    description="Backend for a Bolna-powered pre-dispatch COD verification agent.",
    version="1.0.0",
)


# -------------------------------------------------------------------
# Supabase helpers
# -------------------------------------------------------------------

def supabase_headers():
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def supabase_get_order(order_id: str):
    url = f"{SUPABASE_URL}/rest/v1/orders"

    response = requests.get(
        url,
        headers=supabase_headers(),
        params={
            "order_id": f"eq.{order_id}",
            "select": "*",
            "limit": "1",
        },
        timeout=10,
    )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Supabase error: {response.text}",
        )

    rows = response.json()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"Order {order_id} not found",
        )

    return rows[0]


def supabase_update_order(order_id: str, payload: dict):
    url = f"{SUPABASE_URL}/rest/v1/orders"

    response = requests.patch(
        url,
        headers={
            **supabase_headers(),
            "Prefer": "return=representation",
        },
        params={
            "order_id": f"eq.{order_id}",
        },
        json=payload,
        timeout=10,
    )

    if response.status_code not in (200, 204):
        raise HTTPException(
            status_code=502,
            detail=f"Supabase update error: {response.text}",
        )

    rows = response.json() if response.content else []

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"Order {order_id} not found",
        )

    return rows[0]


# -------------------------------------------------------------------
# Request models
# -------------------------------------------------------------------

class AddressValidationRequest(BaseModel):
    address: str = Field(
        ...,
        min_length=3,
        description="Full address supplied or corrected by the customer.",
    )


class OrderVerificationUpdate(BaseModel):
    customer_confirmed: Optional[bool] = None
    address_verified: Optional[bool] = None
    address: Optional[str] = None
    pincode: Optional[str] = None
    landmark: Optional[str] = None
    delivery_notes: Optional[str] = None
    verification_status: Optional[str] = None


# -------------------------------------------------------------------
# Health
# -------------------------------------------------------------------

@app.get("/")
def root():
    return {
        "service": "COD Order Verification API",
        "status": "running",
        "version": "1.0.0",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


# -------------------------------------------------------------------
# Tool 1: Get order
# -------------------------------------------------------------------

@app.get("/orders/{order_id}")
def get_order(order_id: str):
    """
    Retrieve the order that the Bolna agent is calling about.
    """

    order = supabase_get_order(order_id)

    # Return only information the voice agent actually needs.
    return {
        "success": True,
        "order": {
            "order_id": order["order_id"],
            "customer_name": order["customer_name"],
            "product_name": order["product_name"],
            "amount": float(order["amount"]),
            "payment_method": order["payment_method"],
            "address": order["address"],
            "pincode": order["pincode"],
            "landmark": order["landmark"],
            "delivery_notes": order["delivery_notes"],
            "verification_status": order["verification_status"],
        },
    }


# -------------------------------------------------------------------
# Tool 2: Validate address
# -------------------------------------------------------------------

@app.post("/validate-address")
def validate_address(request: AddressValidationRequest):
    """
    Validate a customer-provided address using OpenStreetMap Nominatim.

    This verifies whether the address can be geographically resolved.
    It does NOT guarantee courier deliverability.
    """

    address = request.address.strip()

    nominatim_url = "https://nominatim.openstreetmap.org/search"

    headers = {
        "User-Agent": NOMINATIM_USER_AGENT,
        "Accept": "application/json",
    }

    params = {
        "q": address,
        "format": "jsonv2",
        "addressdetails": "1",
        "limit": "3",
        "countrycodes": "in",
    }

    try:
        response = requests.get(
            nominatim_url,
            headers=headers,
            params=params,
            timeout=10,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Address service unavailable: {exc}",
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="Address validation service returned an error.",
        )

    results = response.json()

    if not results:
        return {
            "success": True,
            "valid": False,
            "message": "The supplied address could not be geographically verified.",
        }

    best = results[0]
    address_data = best.get("address", {})

    return {
        "success": True,
        "valid": True,
        "formatted_address": best.get("display_name"),
        "latitude": best.get("lat"),
        "longitude": best.get("lon"),
        "city": (
            address_data.get("city")
            or address_data.get("town")
            or address_data.get("municipality")
            or address_data.get("village")
        ),
        "state": address_data.get("state"),
        "pincode": address_data.get("postcode"),
        "country": address_data.get("country"),
        "match_type": best.get("type"),
    }


# -------------------------------------------------------------------
# Tool 3: Update order verification
# -------------------------------------------------------------------

@app.patch("/orders/{order_id}/verification")
def update_order_verification(
    order_id: str,
    request: OrderVerificationUpdate,
):
    """
    Update the verified customer/address information after the call.
    """

    allowed_statuses = {
        "PENDING",
        "VERIFIED",
        "HOLD",
        "CANCELLED",
    }

    payload = request.model_dump(exclude_none=True)

    if "verification_status" in payload:
        if payload["verification_status"] not in allowed_statuses:
            raise HTTPException(
                status_code=400,
                detail=(
                    "verification_status must be one of: "
                    "PENDING, VERIFIED, HOLD, CANCELLED"
                ),
            )

    payload["updated_at"] = "now()"

    # PostgREST cannot evaluate now() when passed as a JSON string.
    # Use the database timestamp generated automatically instead.
    payload.pop("updated_at", None)

    updated = supabase_update_order(order_id, payload)

    return {
        "success": True,
        "message": f"Order {order_id} updated successfully.",
        "order": {
            "order_id": updated["order_id"],
            "customer_confirmed": updated["customer_confirmed"],
            "address_verified": updated["address_verified"],
            "address": updated["address"],
            "pincode": updated["pincode"],
            "landmark": updated["landmark"],
            "delivery_notes": updated["delivery_notes"],
            "verification_status": updated["verification_status"],
        },
    }
