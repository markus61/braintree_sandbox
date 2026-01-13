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

from os import environ
from pathlib import Path
from contextlib import asynccontextmanager

from json import dumps, loads
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
import httpx
import braintree


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8")
    bt_env: str = "sandbox"  # "sandbox" | "production"
    bt_merchant_id: str
    bt_public_key: str
    bt_private_key: str
    bt_webhook_public_key: str | None = None
    bt_webhook_private_key: str | None = None


def _bt_environment(value: str) -> braintree.Environment:
    """Return the Braintree environment for the given setting value."""
    v = value.lower()
    if v == "production":
        return braintree.Environment.Production
    if v == "sandbox":
        return braintree.Environment.Sandbox
    raise ValueError("BT_ENV must be 'sandbox' or 'production'")


def create_gateway(config: Settings) -> braintree.BraintreeGateway:
    """Build a Braintree gateway client from configuration values."""
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
    """Initialize and release application resources for FastAPI lifespan."""
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
        environ.get("CORS_ALLOWED", ""),
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_mps_token() -> str:
    """Fetch and return the OAuth access token for MPS API calls."""

    req_url = "https://global.telekom.com/gcp-web-api/oauth"

    headers_list = {
        "Accept": "*/*",
        "User-Agent": "Thunder Client (https://www.thunderclient.com)",
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {environ.get('MPS_TOKEN', 'xxx==')}",
    }

    payload = "grant_type=client_credentials&scope=T00X7T70"

    with httpx.Client(timeout=10.0) as client:
        data = client.post(req_url, data=payload, headers=headers_list)
    return loads(data.text)["access_token"]


MPS_TOKEN = ""


def initialize_braintree(method: str) -> str:
    """Request a client token for the provided payment method type."""
    global MPS_TOKEN
    req_url = "https://pbs.acceptance.p5x.telekom-dienste.de/pbs-mapi-adapter/braintree/initializeClient"

    headers_dict = {
        "Accept": "*/*",
        "Authorization": f"bearer {MPS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "businessPartnerConfigId": "3023",
        "paymentMethodType": method
    }

    def post_call():
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                req_url, json=payload, headers=headers_dict)
            response.raise_for_status()
        if not response:
            raise RuntimeError(
                "Received empty response from Braintree initializeClient endpoint"
            )
        return response

    response = {}
    while not response:
        try:
            print("Refreshing MPS token...")
            if not MPS_TOKEN:
                MPS_TOKEN = get_mps_token()
            response = post_call()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                MPS_TOKEN = ""
                print("MPS token expired, refreshing...")
            raise RuntimeError(
                f"Failed to initialize Braintree (status {exc.response.status_code}): {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(
                "Failed to reach Braintree initializeClient endpoint") from exc

    return loads(response.text)["clientToken"]


def get_gateway(request: Request) -> braintree.BraintreeGateway:
    """Retrieve the shared Braintree gateway from the FastAPI app state."""
    return request.app.state.gateway


# ---------- Schemas ----------
class ClientTokenRequest(BaseModel):
    customer_id: str | None = None


class PaymentMethodCreateRequest(BaseModel):
    customer_id: str
    payment_method_nonce: str
    make_default: bool = True


class TransactionReserveRequest(BaseModel):
    amount: str
    payment_method_token: str | None = None
    payment_method_nonce: str | None = None
    order_id: str | None = None


@app.get("/")
def get_index():
    body = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Index</title>
</head>
<body>
    <ul>
        <li><a href="/html/recurring_payment_reserve_creditcard.html">Recurring Payment Reserve Credit Card</a></li>
        <li><a href="/html/recurring_payment_reserve_googlepay.html">Recurring Payment Reserve Google Pay</a></li>
        <li><a href="/html/recurring_payment_reserve_paypal.html">Recurring Payment Reserve PayPal</a></li>
    </ul>
</body>
</html>
"""
    return HTMLResponse(content=body)


@app.get("/untested.html")
def get_untested():
    body = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Index</title>
</head>
<body>
    The following endpoints are untested:
    <ul>
        <li><a href="/html/reserve_paypal.html">One Time Reserve PayPal</a></li>
        <li><a href="/html/reserve.html">reserve ?</a></li>
        <li><a href="/html/reserve_recurring.html">???</a></li>
        <li><a href="/html/webclient.html">Webclient</a></li>
    </ul>
</body>
</html>
"""
    return HTMLResponse(content=body)


@app.get("/html/{filename}", response_class=HTMLResponse)
def get_html_file(filename: str):
    """Serve an HTML file from the local html directory if it is safe."""
    html_root = Path("html").resolve()
    candidate = (html_root / filename).resolve()
    if html_root not in candidate.parents or candidate.suffix != ".html":
        raise HTTPException(status_code=400, detail="Invalid path segment")
    try:
        content = candidate.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    return HTMLResponse(content=content, headers={"Cache-Control": "no-cache"})


@app.post("/client-token/{payment_method}")
def create_client_token(payment_method: str) -> dict:
    """Generate a client token for the specified payment method."""
    if payment_method not in ["creditcard", "applepay", "googlepay", "paypal"]:
        raise HTTPException(
            status_code=400,
            detail="Unsupported payment method type",
        )
    token = initialize_braintree(payment_method)
    print(f"\nToken: {token}\n")
    return {"clientToken": token}


@app.post("/reserve")
def reserve(
    body: TransactionReserveRequest,
):
    """Reserve a one-time transaction in the Telekom checkout API."""
    req_url = "https://pbs.acceptance.p5x.telekom-dienste.de/pbs-checkout-api/direct/reserve"
    headers_dict = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Authorization": f"bearer {MPS_TOKEN}",
    }

    payload = dumps({
        "paymentMethod": "creditcard_braintree",
        "businessPartnerConfigId": "3023",
        "currency": "EUR",
        "locale": "de_DE",
        "description": "Ihr Multibrand Zahlungsmandat",
        "returnUrl": "http://www.telekom.de",
        "lineItems": [
            {
                "name": "Zahlung + Speicherung des Zahlungsmandats",
                "description": "Initial 15 € + Speicherung des Zahlungsmandats",
                "grossAmount": body.amount,
                "taxRate": 19,
                "quantity": 1,
                "uiDetails": {},
            }
        ],
        "paymentServiceData": {"nonce": body.payment_method_nonce},
        "settlementData": {"settlementConfigurationId": "32727"},
    })
    with httpx.Client(timeout=10.0) as client:
        data = client.post(req_url, data=payload, headers=headers_dict)

    print(data.text)
    return data.text


@app.post("/recurring/payment/reserve")
def reserve_recurring(
    body: TransactionReserveRequest,
):
    """Reserve a recurring mandate-based transaction via the checkout API."""
    req_url = "https://pbs.acceptance.p5x.telekom-dienste.de/pbs-checkout-api/recurring/payment/direct/reserve"
    headers_dict = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Authorization": f"bearer {MPS_TOKEN}",
    }

    payload = dumps({
        "paymentMethod": f"{body.payment_method_token}_braintree",
        "businessPartnerConfigId": "3023",
        "currency": "EUR",
        "locale": "de_DE",
        "description": "Ihr Multibrand Zahlungsmandat",
        "returnUrl": "http://www.telekom.de",
        "lineItems": [
            {
                "name": "Zahlung + Speicherung des Zahlungsmandats",
                "description": "Initial 15 € + Speicherung des Zahlungsmandats",
                "grossAmount": body.amount,
                "taxRate": 19,
                "quantity": 1,
                "uiDetails": {},
            }
        ],
        "paymentServiceData": {"nonce": body.payment_method_nonce},
        "settlementData": {"settlementConfigurationId": "32727"},
    })
    with httpx.Client(timeout=10.0) as client:
        data = client.post(req_url, data=payload, headers=headers_dict)

    print(data.text)
    return data.text


@app.post("/recurring/paypal")
def recurring_paypal(
    body: TransactionReserveRequest,
):
    """Create a recurring PayPal checkout session in the Telekom API."""
    req_url = "https://pbs.acceptance.p5x.telekom-dienste.de/pbs-checkout-api/recurring/payment"
    headers_dict = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Authorization": f"bearer {MPS_TOKEN}",
    }

    payload = dumps({
        "paymentMethod": "paypal_braintree",
        "businessPartnerConfigId": "3023",
        "currency": "EUR",
        "locale": "de_DE",
        "description": "Ihr Multibrand Zahlungsmandat",
        "returnUrl": "http://www.telekom.de",
        "lineItems": [
            {
                "name": "Zahlung + Speicherung des Zahlungsmandats",
                "description": f"Initial {body.amount} € + Speicherung des Zahlungsmandats",
                "grossAmount": body.amount,
                "taxRate": 19,
                "quantity": 1,
                "uiDetails": {},
            }
        ],
        "settlementData": {"settlementConfigurationId": "32727"},
    })
    with httpx.Client(timeout=10.0) as client:
        data = client.post(req_url, data=payload, headers=headers_dict)

    print(data.text)
    return data.json()
