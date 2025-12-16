"""
FastAPI application that wraps selected Braintree payment gateway features.

The module configures gateway credentials via Pydantic settings, exposes 
helpers to obtain a singleton `BraintreeGateway`, and implements REST 
endpoints for:
* health checks,
* generating client tokens used by web and mobile clients,
* vaulting payment methods,
* creating sales transactions with optional settlement,
* handling webhook notifications for downstream business logic integration.
"""

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv
import uvicorn
import braintree


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
    bt_env: str = "sandbox"  # "sandbox" | "production"
    bt_merchant_id: str
    bt_public_key: str
    bt_private_key: str
    bt_webhook_public_key: str | None = None
    bt_webhook_private_key: str | None = None


def _bt_environment(value: str):
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
    customer_id: str | None = None  # euer ETC-User/Customer Identifier


class PaymentMethodCreateRequest(BaseModel):
    customer_id: str
    payment_method_nonce: str  # kommt aus Web/iOS/Android/RN SDK
    make_default: bool = True


class SaleRequest(BaseModel):
    amount: str
    payment_method_token: str | None = None
    payment_method_nonce: str | None = None
    submit_for_settlement: bool = True
    order_id: str | None = None


# ---------- Endpoints ----------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/braintree/client-token")
def create_client_token(
    body: ClientTokenRequest,
    gw: braintree.BraintreeGateway = Depends(get_gateway),
):
    # Client token -> an Frontend, damit es nonces erzeugen kann
    try:
        token = gw.client_token.generate(
            {"customer_id": body.customer_id} if body.customer_id else {}
        )
        return {"clientToken": token}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/braintree/payment-methods")
def vault_payment_method(
    body: PaymentMethodCreateRequest,
    gw: braintree.BraintreeGateway = Depends(get_gateway),
):
    result = gw.payment_method.create({
        "customer_id": body.customer_id,
        "payment_method_nonce": body.payment_method_nonce,
        "options": {"make_default": body.make_default},
    })
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
    if (body.payment_method_token is None) == (body.payment_method_nonce is None):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of payment_method_token or payment_method_nonce",
        )

    payload = {
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

    tx = result.transaction
    return {
        "id": tx.id,
        "status": tx.status,
        "amount": tx.amount,
        "createdAt": str(tx.created_at),
    }


@app.post("/braintree/webhooks")
async def webhooks(request: Request, gw: braintree.BraintreeGateway = Depends(get_gateway)):
    form = await request.form()
    bt_signature = form.get("bt_signature")
    bt_payload = form.get("bt_payload")
    if not bt_signature or not bt_payload:
        raise HTTPException(status_code=400, detail="Missing bt_signature/bt_payload")

    try:
        notification = gw.webhook_notification.parse(bt_signature, bt_payload)
        # TODO: hier eure Business-Logik: status updates, retries, etc.
        return {
            "kind": str(notification.kind),
            "timestamp": str(notification.timestamp),
            "subject_id": getattr(notification.subject, "id", None),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    uvicorn.run("src.__main__:app", host="0.0.0.0", port=8000)
