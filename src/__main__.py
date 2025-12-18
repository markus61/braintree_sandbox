"""
FastAPI application that wraps selected Braintree payment gateway features.

The module configures gateway credentials via Pydantic settings, exposes
helpers to obtain a singleton `BraintreeGateway`, and implements REST
endpoints for:
* health checks,
* generating client tokens used by web and mobile clients,
* vaulting payment methods,
* creating and managing sales transactions,
* handling webhook notifications for downstream business logic integration.
"""

from contextlib import asynccontextmanager

import braintree
import uvicorn
from braintree.exceptions.not_found_error import NotFoundError
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
    bt_env: str = "sandbox"  # "sandbox" | "production"
    bt_merchant_id: str
    bt_public_key: str
    bt_private_key: str
    bt_webhook_public_key: str | None = None
    bt_webhook_private_key: str | None = None


def _bt_environment(value: str) -> braintree.Environment:
    v = value.lower()
    if v == "production":
        return braintree.Environment.Production
    if v == "sandbox":
        return braintree.Environment.Sandbox
    raise ValueError("BT_ENV must be 'sandbox' or 'production'")


def create_gateway(config: Settings) -> braintree.BraintreeGateway:
    return braintree.BraintreeGateway(
        braintree.Configuration(
            environment=_bt_environment(config.bt_env),
            merchant_id=config.bt_merchant_id,
            public_key=config.bt_public_key,
            private_key=config.bt_private_key,
        )
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()
    settings = Settings()
    app.state.settings = settings
    app.state.gateway = create_gateway(settings)
    try:
        yield
    finally:
        app.state.gateway = None


app = FastAPI(title="ETC Payments (Braintree)", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_gateway(request: Request) -> braintree.BraintreeGateway:
    return request.app.state.gateway


# ---------- Schemas ----------
class ClientTokenRequest(BaseModel):
    customer_id: str | None = None


class PaymentMethodCreateRequest(BaseModel):
    customer_id: str
    payment_method_nonce: str
    make_default: bool = True


class SaleRequest(BaseModel):
    amount: str
    payment_method_token: str | None = None
    payment_method_nonce: str | None = None
    submit_for_settlement: bool = True
    order_id: str | None = None


class TransactionReserveRequest(BaseModel):
    amount: str
    payment_method_token: str | None = None
    payment_method_nonce: str | None = None
    order_id: str | None = None


def _ensure_single_payment_method(token: str | None, nonce: str | None) -> None:
    if (token is None) == (nonce is None):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of payment_method_token or payment_method_nonce",
        )


def _serialize_transaction(tx: braintree.Transaction) -> dict:
    """Expose a JSON-friendly subset of transaction attributes."""

    def _iso(dt):
        return dt.isoformat() if dt else None

    return {
        "id": getattr(tx, "id", None),
        "status": getattr(tx, "status", None),
        "amount": getattr(tx, "amount", None),
        "currencyIsoCode": getattr(tx, "currency_iso_code", None),
        "orderId": getattr(tx, "order_id", None),
        "paymentInstrumentType": getattr(tx, "payment_instrument_type", None),
        "customerId": getattr(tx, "customer_details", None)
        and getattr(tx.customer_details, "id", None),
        "authorizationExpiresAt": _iso(getattr(tx, "authorization_expires_at", None)),
        "createdAt": _iso(getattr(tx, "created_at", None)),
        "updatedAt": _iso(getattr(tx, "updated_at", None)),
    }


# ---------- Endpoints ----------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/braintree/client-token")
def create_client_token(
    body: ClientTokenRequest,
    gw: braintree.BraintreeGateway = Depends(get_gateway),
):
    try:
        token = gw.client_token.generate(
            {"customer_id": body.customer_id} if body.customer_id else {}
        )
        return {"clientToken": token}
    except Exception as exc:  # SDK surfaces various runtime errors
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/braintree/payment-methods")
def vault_payment_method(
    body: PaymentMethodCreateRequest,
    gw: braintree.BraintreeGateway = Depends(get_gateway),
):
    result = gw.payment_method.create(
        {
            "customer_id": body.customer_id,
            "payment_method_nonce": body.payment_method_nonce,
            "options": {"make_default": body.make_default},
        }
    )
    if not result.is_success:
        raise HTTPException(status_code=400, detail=str(result.message))

    pm = result.payment_method
    return {
        "token": pm.token,
        "type": pm.__class__.__name__,
        "isDefault": getattr(pm, "default", None),
    }


@app.post("/braintree/transactions/sale")
def sale(
    body: SaleRequest,
    gw: braintree.BraintreeGateway = Depends(get_gateway),
):
    _ensure_single_payment_method(body.payment_method_token, body.payment_method_nonce)

    payload: dict[str, object] = {
        "amount": body.amount,
        "options": {"submit_for_settlement": body.submit_for_settlement},
    }
    if body.order_id:
        payload["order_id"] = body.order_id
    if body.payment_method_token:
        payload["payment_method_token"] = body.payment_method_token
    else:
        payload["payment_method_nonce"] = body.payment_method_nonce

    result = gw.transaction.sale(payload)
    if not result.is_success:
        raise HTTPException(status_code=402, detail=str(result.message))

    return _serialize_transaction(result.transaction)


@app.post("/braintree/transactions/reserve")
def reserve(
    body: TransactionReserveRequest,
    gw: braintree.BraintreeGateway = Depends(get_gateway),
):
    _ensure_single_payment_method(body.payment_method_token, body.payment_method_nonce)

    payload: dict[str, object] = {
        "amount": body.amount,
        "options": {"submit_for_settlement": False},
    }
    if body.order_id:
        payload["order_id"] = body.order_id
    if body.payment_method_token:
        payload["payment_method_token"] = body.payment_method_token
    else:
        payload["payment_method_nonce"] = body.payment_method_nonce

    result = gw.transaction.sale(payload)
    if not result.is_success:
        raise HTTPException(status_code=400, detail=str(result.message))

    return _serialize_transaction(result.transaction)


@app.get("/braintree/transactions/{transaction_id}")
def get_transaction(
    transaction_id: str,
    gw: braintree.BraintreeGateway = Depends(get_gateway),
):
    try:
        transaction = gw.transaction.find(transaction_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Transaction not found") from exc

    return _serialize_transaction(transaction)


@app.post("/braintree/transactions/{transaction_id}/claim")
def claim_transaction(
    transaction_id: str,
    gw: braintree.BraintreeGateway = Depends(get_gateway),
):
    try:
        result = gw.transaction.submit_for_settlement(transaction_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Transaction not found") from exc

    if not result.is_success:
        raise HTTPException(status_code=409, detail=str(result.message))

    return _serialize_transaction(result.transaction)


@app.post("/braintree/transactions/{transaction_id}/cancel")
def cancel_transaction(
    transaction_id: str,
    gw: braintree.BraintreeGateway = Depends(get_gateway),
):
    try:
        result = gw.transaction.void(transaction_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Transaction not found") from exc

    if not result.is_success:
        raise HTTPException(status_code=409, detail=str(result.message))

    return _serialize_transaction(result.transaction)


@app.post("/braintree/webhooks")
async def webhooks(
    request: Request,
    gw: braintree.BraintreeGateway = Depends(get_gateway),
):
    form = await request.form()
    bt_signature = form.get("bt_signature")
    bt_payload = form.get("bt_payload")
    if not bt_signature or not bt_payload:
        raise HTTPException(status_code=400, detail="Missing bt_signature/bt_payload")

    try:
        notification = gw.webhook_notification.parse(bt_signature, bt_payload)
        return {
            "kind": str(notification.kind),
            "timestamp": str(notification.timestamp),
            "subject_id": getattr(notification.subject, "id", None),
        }
    except Exception as exc:  # surface parse errors to clients for debugging
        raise HTTPException(status_code=400, detail=str(exc)) from exc


if __name__ == "__main__":
    uvicorn.run("src.__main__:app", host="0.0.0.0", port=8000)
