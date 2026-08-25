import os
from typing import Optional
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Header
from pydantic import BaseModel, Field, ConfigDict

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
NOMINATIM_USER_AGENT = os.getenv(
    "NOMINATIM_USER_AGENT",
    "CODVerificationAgent/1.0"
)
TOOL_API_SECRET = os.getenv("TOOL_API_SECRET", "")

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is not configured")

if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not configured")

if not TOOL_API_SECRET:
    raise RuntimeError("TOOL_API_SECRET is not configured")


app = FastAPI(
    title="COD Order Verification API",
    description="Backend for a Bolna-powered pre-dispatch COD verification agent.",
    version="1.0.0",
)


# -------------------------------------------------------------------
# Tool authentication
# -------------------------------------------------------------------

def verify_tool_secret(x_tool_secret: Optional[str]):
    """
    Authenticate requests coming from Bolna custom tools.
    """

    if not x_tool_secret or x_tool_secret != TOOL_API_SECRET:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized tool request",
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
# Call session helpers
# -------------------------------------------------------------------

def supabase_get_call_session(call_id: str):
    """
    Retrieve the server-side session associated with a call.
    """

    url = f"{SUPABASE_URL}/rest/v1/call_sessions"

    response = requests.get(
        url,
        headers=supabase_headers(),
        params={
            "call_id": f"eq.{call_id}",
            "select": "*",
            "limit": "1",
        },
        timeout=10,
    )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Supabase session error: {response.text}",
        )

    rows = response.json()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"Call session {call_id} not found",
        )

    return rows[0]


def supabase_create_call_session(call_id: str, order_id: str):
    """
    Create a server-side authorization binding between a call
    and exactly one order.
    """

    url = f"{SUPABASE_URL}/rest/v1/call_sessions"

    response = requests.post(
        url,
        headers={
            **supabase_headers(),
            "Prefer": "return=representation",
        },
        json={
            "call_id": call_id,
            "order_id": order_id,
            "status": "ACTIVE",
        },
        timeout=10,
    )

    if response.status_code not in (200, 201):
        raise HTTPException(
            status_code=502,
            detail=f"Supabase session creation error: {response.text}",
        )

    rows = response.json()

    if not rows:
        raise HTTPException(
            status_code=502,
            detail="Supabase did not return the created call session.",
        )

    return rows[0]


def ensure_call_session(call_id: str, order_id: str):
    """
    Idempotently establish the call -> order binding.

    If a session already exists for this call, it may only refer to
    the same order. A call can never be rebound to a different order.
    """

    try:
        existing = supabase_get_call_session(call_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return supabase_create_call_session(call_id, order_id)

    if existing["order_id"] != order_id:
        raise HTTPException(
            status_code=403,
            detail="This call session is already bound to a different order.",
        )

    if existing["status"] != "ACTIVE":
        raise HTTPException(
            status_code=403,
            detail="This call session is no longer active.",
        )

    expires_at = existing.get("expires_at")
    if expires_at:
        try:
            expires = datetime.strptime(
                expires_at,
                "%Y-%m-%dT%H:%M:%S.%f%z",
            )
            if expires <= datetime.now(timezone.utc):
                raise HTTPException(
                    status_code=403,
                    detail="This call session has expired.",
                )
        except ValueError:
            raise HTTPException(
                status_code=500,
                detail="Invalid call session expiration timestamp.",
            )

    return existing


def verify_call_order_access(call_id: str, order_id: str):
    """
    Verify that the requested order belongs to the active,
    non-expired call session.

    The Bolna pre-call webhook is fire-and-forget, so it can arrive
    slightly after the main function request. We therefore retry the
    session lookup briefly rather than failing on a normal race.
    """

    session = None

    for attempt in range(5):
        try:
            session = supabase_get_call_session(call_id)
            break
        except HTTPException as exc:
            if exc.status_code != 404 or attempt == 4:
                raise

            import time
            time.sleep(0.25)

    if session["status"] != "ACTIVE":
        raise HTTPException(
            status_code=403,
            detail="This call session is no longer active.",
        )

    expires_at = session.get("expires_at")

    if expires_at:
        try:
            expires = datetime.strptime(
                expires_at,
                "%Y-%m-%dT%H:%M:%S.%f%z",
            )

            if expires <= datetime.now(timezone.utc):
                raise HTTPException(
                    status_code=403,
                    detail="This call session has expired.",
                )

        except ValueError:
            raise HTTPException(
                status_code=500,
                detail="Invalid call session expiration timestamp.",
            )

    if session["order_id"] != order_id:
        raise HTTPException(
            status_code=403,
            detail="Order is not authorized for this call session.",
        )

    return session

# -------------------------------------------------------------------
# Request models
# -------------------------------------------------------------------

class AddressValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(
        ...,
        min_length=3,
        description="Full address supplied or corrected by the customer.",
    )

    pincode: Optional[str] = Field(
        default=None,
        description="Postal code supplied by the customer, if available.",
    )


class OrderVerificationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    call_id: str


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
    return {
        "status": "healthy",
    }


# -------------------------------------------------------------------
# Bolna pre-call webhook: establish call -> order binding
# -------------------------------------------------------------------

@app.post("/webhooks/bolna-pre-call")
def bolna_pre_call_webhook(payload: dict):
    """
    Receive Bolna's pre-call webhook for get_order_details.

    Bolna sends the normal execution record plus the fields configured
    in pre_call_webhook_param. For this agent, that extra field is
    order_id.

    The endpoint is intentionally idempotent:
    - first request creates call_id -> order_id
    - repeated requests for the same pair are harmless
    - a call can never be rebound to another order

    This endpoint does not require X-Tool-Secret because Bolna's
    pre-call webhook configuration does not use the custom tool
    authentication header. The actual order-reading and write
    endpoints remain protected by X-Tool-Secret.
    """
    
    print("=== BOLNA PRE-CALL PAYLOAD ===")
    print(payload)
    print("=== END BOLNA PRE-CALL PAYLOAD ===")

    # In Bolna execution records, the execution/call identifier may
    # appear as call_id or id depending on the payload surface.
    call_id = (
        payload.get("call_id")
        or payload.get("execution_id")
        or payload.get("id")
    )

    order_id = payload.get("order_id")

    if not call_id:
        raise HTTPException(
            status_code=400,
            detail="Missing call_id/execution id in Bolna pre-call payload.",
        )

    if not order_id:
        raise HTTPException(
            status_code=400,
            detail="Missing order_id in Bolna pre-call payload.",
        )

    # Confirm that the target order actually exists before binding
    # a live call to it.
    supabase_get_order(order_id)

    session = ensure_call_session(
        call_id=str(call_id),
        order_id=str(order_id),
    )

    return {
        "success": True,
        "session": {
            "call_id": session["call_id"],
            "order_id": session["order_id"],
            "status": session["status"],
            "created_at": session.get("created_at"),
            "expires_at": session.get("expires_at"),
        },
    }


# -------------------------------------------------------------------
# Tool 0: Create call session
# -------------------------------------------------------------------

@app.post("/call-sessions")
def create_call_session(
    call_id: str = Query(
        ...,
        min_length=1,
        description="Unique identifier for the current voice call.",
    ),
    order_id: str = Query(
        ...,
        min_length=1,
        description="Order authorized for this call.",
    ),
    x_tool_secret: Optional[str] = Header(default=None),
):
    """
    Create a server-side authorization binding between a call
    and exactly one order.

    This is currently also useful for local/manual testing.
    """

    verify_tool_secret(x_tool_secret)

    # Confirm that the order exists before creating the binding.
    supabase_get_order(order_id)

    session = ensure_call_session(
        call_id=call_id,
        order_id=order_id,
    )

    return {
        "success": True,
        "session": session,
    }


# -------------------------------------------------------------------
# Tool 1: Get order
# -------------------------------------------------------------------

@app.get("/orders")
def get_order_by_query(
    order_id: str = Query(
        ...,
        description="The order ID associated with the current call.",
    ),
    call_id: str = Query(
        ...,
        description="The server-side call session identifier.",
    ),
    x_tool_secret: Optional[str] = Header(default=None),
):
    """
    Retrieve an order only when that order is authorized
    for the current call session.
    """

    verify_tool_secret(x_tool_secret)

    verify_call_order_access(
        call_id=call_id,
        order_id=order_id,
    )

    order = supabase_get_order(order_id)

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
            "customer_confirmed": order.get("customer_confirmed"),
            "address_verified": order.get("address_verified"),
            "verification_status": order["verification_status"],
        },
    }


# -------------------------------------------------------------------
# Legacy/manual order endpoint
# -------------------------------------------------------------------

@app.get("/orders/{order_id}")
def get_order(
    order_id: str,
    x_tool_secret: Optional[str] = Header(default=None),
):
    """
    Retrieve an order directly by ID.

    This endpoint is retained for manual/backend testing.

    The Bolna production tool should use /orders with call_id
    so that the order is bound to the active call session.
    """

    verify_tool_secret(x_tool_secret)

    order = supabase_get_order(order_id)

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
            "customer_confirmed": order.get("customer_confirmed"),
            "address_verified": order.get("address_verified"),
            "verification_status": order["verification_status"],
        },
    }


# -------------------------------------------------------------------
# Tool 2: Validate address
# -------------------------------------------------------------------

@app.post("/validate-address")
def validate_address(
    request: AddressValidationRequest,
    x_tool_secret: Optional[str] = Header(default=None),
):
    """
    Validate a customer-provided address using OpenStreetMap Nominatim.

    The service:
    - checks whether the location can be geographically resolved
    - restricts results to India
    - checks an explicitly supplied pincode against the resolved pincode

    It does NOT guarantee courier deliverability.
    """

    verify_tool_secret(x_tool_secret)

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
            "reason": "LOCATION_NOT_FOUND",
            "message": (
                "The supplied address could not be geographically verified."
            ),
        }

    best = results[0]
    address_data = best.get("address", {})

    # ---------------------------------------------------------------
    # Country restriction
    # ---------------------------------------------------------------

    country_code = (
        address_data.get("country_code") or ""
    ).lower()

    if country_code != "in":
        return {
            "success": True,
            "valid": False,
            "reason": "OUTSIDE_SERVICE_AREA",
            "message": (
                "The supplied location is outside the supported "
                "delivery country."
            ),
            "country": address_data.get("country"),
        }

    verified_pincode = address_data.get("postcode")

    # ---------------------------------------------------------------
    # Pincode consistency check
    # ---------------------------------------------------------------

    if request.pincode and verified_pincode:
        supplied_pincode = request.pincode.strip()

        if supplied_pincode != verified_pincode:
            return {
                "success": True,
                "valid": False,
                "reason": "PINCODE_MISMATCH",
                "message": (
                    "The supplied pincode does not match the "
                    "geographically resolved location."
                ),
                "provided_pincode": supplied_pincode,
                "verified_pincode": verified_pincode,
                "formatted_address": best.get("display_name"),
            }

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
        "pincode": verified_pincode,
        "country": address_data.get("country"),
        "country_code": country_code,
        "match_type": best.get("type"),
    }


# -------------------------------------------------------------------
# Tool 3: Update order verification
# -------------------------------------------------------------------

@app.post("/orders/{order_id}/verification")
def update_order_verification(
    order_id: str,
    request: OrderVerificationUpdate,
    x_tool_secret: Optional[str] = Header(default=None),
):
    """
    Update only customer verification and delivery-address fields.

    The requested order must belong to the active call session.

    Immutable fields such as:
    - product
    - amount
    - payment method
    - customer name
    - phone number

    cannot be changed through this endpoint.
    """

    verify_tool_secret(x_tool_secret)

    # ---------------------------------------------------------------
    # Call → order authorization
    # ---------------------------------------------------------------

    verify_call_order_access(
        call_id=request.call_id,
        order_id=order_id,
    )

    # ---------------------------------------------------------------
    # Allowed statuses
    # ---------------------------------------------------------------

    allowed_statuses = {
        "PENDING",
        "VERIFIED",
        "HOLD",
        "CANCELLED",
    }

    payload = request.model_dump(
        exclude_none=True,
        exclude={"call_id"},
    )
    # ---------------------------------------------------------------
    # Verification status validation
    # ---------------------------------------------------------------

    if "verification_status" in payload:
        status = payload["verification_status"]

        if status not in allowed_statuses:
            raise HTTPException(
                status_code=400,
                detail=(
                    "verification_status must be one of: "
                    "PENDING, VERIFIED, HOLD, CANCELLED"
                ),
            )
    else:
        status = None

    customer_confirmed = payload.get("customer_confirmed")
    address_verified = payload.get("address_verified")
    address = payload.get("address")

    # ---------------------------------------------------------------
    # VERIFIED requires explicit customer + address confirmation
    # ---------------------------------------------------------------

    if status == "VERIFIED":

        if customer_confirmed is not True:
            raise HTTPException(
                status_code=400,
                detail=(
                    "An order cannot be VERIFIED without "
                    "explicit customer confirmation."
                ),
            )

        if address_verified is not True:
            raise HTTPException(
                status_code=400,
                detail=(
                    "An order cannot be VERIFIED without "
                    "address verification."
                ),
            )

        if not address or not address.strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    "A VERIFIED order must contain a delivery address."
                ),
            )

    # ---------------------------------------------------------------
    # CANCELLED requires explicit rejection
    # ---------------------------------------------------------------

    if status == "CANCELLED":

        if customer_confirmed is not False:
            raise HTTPException(
                status_code=400,
                detail=(
                    "A CANCELLED order must have "
                    "customer_confirmed=false."
                ),
            )

    # ---------------------------------------------------------------
    # HOLD cannot claim successful verification
    # ---------------------------------------------------------------

    if status == "HOLD":

        if customer_confirmed is True and address_verified is True:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Use VERIFIED when both customer and "
                    "address have been explicitly confirmed."
                ),
            )

    # ---------------------------------------------------------------
    # Explicit update allowlist
    #
    # Pydantic extra="forbid" already rejects unknown fields.
    # This second allowlist makes the database boundary explicit.
    # ---------------------------------------------------------------

    allowed_fields = {
        "customer_confirmed",
        "address_verified",
        "address",
        "pincode",
        "landmark",
        "delivery_notes",
        "verification_status",
    }

    unexpected_fields = set(payload.keys()) - allowed_fields

    if unexpected_fields:
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "Attempted to update immutable or "
                    "unsupported fields."
                ),
                "fields": sorted(unexpected_fields),
            },
        )

    payload = {
        key: value
        for key, value in payload.items()
        if key in allowed_fields
    }

    # ---------------------------------------------------------------
    # Perform database update
    # ---------------------------------------------------------------

    updated = supabase_update_order(
        order_id,
        payload,
    )

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

