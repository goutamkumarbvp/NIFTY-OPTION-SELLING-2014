# Broker capability matrix (generated from `amrt/brokers/*.py`)

Status meanings: **VERIFIED** = exercised against the live broker API with recorded evidence; **PARTIAL** = implemented against the official SDK and contract-tested (every SDK call binds to the installed SDK's real signature) but not exercised live from this build environment; **UNAVAILABLE** = the broker does not offer it officially; **BLOCKED** = not used by policy.

Live verification status: **BLOCKED in the build environment** — broker APIs are not reachable from it, so no feature is marked VERIFIED.

## Kotak Neo — overall PARTIAL

* SDK: `kotakneoapi (neo_api_client)` (tested 3.0.6); API Neo Trade API v2 (SDK 3.0.6)
* Official docs: https://github.com/Kotak-Neo/Kotak-neo-api-v2 (SDK docstrings); Kotak Neo Trade API documentation
* Auth: consumer key + TOTP login (mobile, UCC, TOTP) + MPIN validate; session per day
* Order tag field: tag (echoed as GuiOrdId) (max 20)

| Feature | Status | Notes |
|---|---|---|
| authentication | PARTIAL | live login BLOCKED: network policy |
| option_chain | PARTIAL | live payload not verified |
| quotes | PARTIAL |  |
| orders | BLOCKED | no live verification possible |
| order_book/positions/funds | PARTIAL |  |
| websocket | UNAVAILABLE |  |

## Zerodha Kite Connect — overall PARTIAL

* SDK: `kiteconnect` (tested 5.2.2); API Kite Connect v3
* Official docs: https://kite.trade/docs/connect/v3/
* Auth: API key + daily request_token → access_token (OAuth redirect)
* Order tag field: tag (max 20)

| Feature | Status | Notes |
|---|---|---|
| authentication | PARTIAL | live BLOCKED: network policy |
| option_chain | PARTIAL |  |
| quotes | PARTIAL |  |
| orders | BLOCKED |  |
| order_book/positions/funds | PARTIAL |  |
| websocket | UNAVAILABLE |  |

## Angel One SmartAPI — overall PARTIAL

* SDK: `smartapi-python (SmartApi)` (tested 1.5.5); API SmartAPI REST v1
* Official docs: https://smartapi.angelbroking.com/docs
* Auth: API key + client code + PIN + TOTP → JWT session
* Order tag field: ordertag (max 20)

| Feature | Status | Notes |
|---|---|---|
| authentication | PARTIAL | live BLOCKED: network policy |
| option_chain | PARTIAL |  |
| orders | BLOCKED |  |
| order_book/positions/funds | PARTIAL |  |
| websocket | UNAVAILABLE |  |

## Upstox — overall PARTIAL

* SDK: `upstox-python-sdk (upstox_client)` (tested 2.30.0); API v2 REST
* Official docs: https://upstox.com/developer/api-documentation/
* Auth: OAuth 2 daily access token
* Order tag field: tag (max 20)

| Feature | Status | Notes |
|---|---|---|
| authentication | PARTIAL | live BLOCKED: network policy |
| option_chain | PARTIAL |  |
| orders | BLOCKED |  |
| order_book/positions/funds | PARTIAL |  |
| websocket | UNAVAILABLE |  |

## Groww Trade API — overall PARTIAL

* SDK: `growwapi` (tested 1.5.0); API Groww Trade API v1 (SDK 1.5.0)
* Official docs: https://groww.in/trade-api/docs
* Auth: API key + secret (or TOTP) → access token
* Order tag field: order_reference_id (max 20)

| Feature | Status | Notes |
|---|---|---|
| authentication | PARTIAL | live BLOCKED: network policy |
| option_chain | PARTIAL |  |
| orders | BLOCKED |  |
| order_book/positions/funds | PARTIAL |  |
| websocket | UNAVAILABLE |  |

