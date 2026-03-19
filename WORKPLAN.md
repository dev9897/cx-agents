# Secure Checkout Refactor — Work Plan

**Branch**: `feature/secure-checkout-refactor`
**Created**: 2026-03-19
**Goal**: Stripe Checkout Session integration, layered architecture, smarter state, better UX

---

## Phase 1: Architecture Refactor (Foundation)

### Task 1.1 — Project restructure into layered architecture
**Status**: [x] DONE (2026-03-19)
**Priority**: P0 — everything depends on this

Restructure flat files into a proper layered architecture:

```
cx-agents/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app factory
│   ├── config.py                  # Centralized config (from agent_config.py)
│   │
│   ├── api/                       # API layer (routes only, no business logic)
│   │   ├── __init__.py
│   │   ├── chat.py                # POST /chat, POST /chat/approve
│   │   ├── auth.py                # POST /auth/login, /auth/status, /auth/logout
│   │   ├── checkout.py            # Stripe checkout routes
│   │   ├── health.py              # GET /health
│   │   ├── websocket.py           # WS /chat/stream
│   │   └── acp.py                 # ACP endpoints (move from acp/routes.py)
│   │
│   ├── services/                  # Business logic layer
│   │   ├── __init__.py
│   │   ├── agent_service.py       # LangGraph agent orchestration
│   │   ├── cart_service.py        # Cart operations + Redis persistence
│   │   ├── checkout_service.py    # Checkout flow state machine + Stripe
│   │   ├── order_service.py       # Order placement + reorder logic
│   │   ├── product_service.py     # Product search (keyword + semantic)
│   │   └── auth_service.py        # Authentication logic
│   │
│   ├── integrations/              # External system adapters
│   │   ├── __init__.py
│   │   ├── sap_client.py          # SAP Commerce OCC HTTP client (from sap_commerce_tools.py)
│   │   ├── stripe_client.py       # Stripe SDK wrapper
│   │   ├── redis_client.py        # Redis connection + helpers
│   │   └── qdrant_client.py       # Qdrant vector search (from qdrant_tool.py)
│   │
│   ├── models/                    # Data models / schemas
│   │   ├── __init__.py
│   │   ├── session.py             # Session state models
│   │   ├── cart.py                # Cart models
│   │   ├── checkout.py            # Checkout + payment models
│   │   ├── order.py               # Order models
│   │   └── acp.py                 # ACP protocol models (from acp/models.py)
│   │
│   ├── agent/                     # LangGraph agent
│   │   ├── __init__.py
│   │   ├── graph.py               # Graph definition + nodes
│   │   ├── tools.py               # LangChain tools (thin wrappers over services)
│   │   ├── prompts.py             # System prompts
│   │   └── state.py               # ShoppingState TypedDict
│   │
│   ├── middleware/                 # Cross-cutting concerns
│   │   ├── __init__.py
│   │   ├── security.py            # Injection detection, rate limiting
│   │   ├── error_handler.py       # Global error handling + circuit breaker
│   │   └── audit.py               # Audit logging
│   │
│   └── static/                    # Frontend
│       ├── index.html             # Main chat page
│       ├── css/
│       │   └── styles.css         # Extracted styles
│       ├── js/
│       │   ├── app.js             # Main app logic
│       │   ├── chat.js            # Chat messaging
│       │   ├── checkout.js        # Stripe checkout UI
│       │   └── components.js      # Cart summary, order cards
│       └── templates/
│           └── components.html    # HTML templates for rich cards
│
├── tests/                         # Test suite
│   ├── test_checkout_service.py
│   ├── test_cart_service.py
│   └── test_stripe_integration.py
│
├── WORKPLAN.md                    # This file
├── requirements.txt
├── Dockerfile
└── .env
```

**Files to create/move**:
- [ ] Create `app/` directory structure
- [ ] Move `api_server.py` → split into `app/main.py` + `app/api/*.py`
- [ ] Move `sap_commerce_tools.py` → split into `app/integrations/sap_client.py` + `app/agent/tools.py`
- [ ] Move `production_agent.py` → split into `app/agent/graph.py` + `app/services/agent_service.py`
- [ ] Move `security_layer.py` → `app/middleware/security.py` + `app/middleware/audit.py`
- [ ] Move `schemas/` → `app/models/`
- [ ] Move `agent_config.py` → `app/config.py`
- [ ] Move `acp/` → `app/api/acp.py` + `app/models/acp.py`
- [ ] Move `static/index.html` → split into `app/static/` (html + css + js)
- [ ] Update all imports
- [ ] Verify server starts and existing endpoints work

---

### Task 1.2 — Extract frontend into separate files
**Status**: [ ] TODO
**Priority**: P0

Split the monolithic `static/index.html` (39KB) into:
- `index.html` — minimal HTML shell
- `css/styles.css` — all styles
- `js/app.js` — initialization, WebSocket/HTTP setup
- `js/chat.js` — message rendering, input handling
- `js/checkout.js` — Stripe Elements, payment flow UI
- `js/components.js` — cart summary card, order card, product card renderers

---

## Phase 2: Stripe Checkout Integration

### Task 2.1 — Stripe backend integration
**Status**: [x] DONE (2026-03-19) — stripe_client.py, checkout_service.py, checkout routes created
**Priority**: P0

- [ ] Add `stripe` to requirements.txt
- [ ] Create `app/integrations/stripe_client.py`:
  - `create_checkout_session(cart_items, customer_email, success_url, cancel_url)` → returns session URL
  - `handle_webhook(payload, sig_header)` → processes payment events
  - `get_payment_status(session_id)` → check if paid
- [ ] Create `app/services/checkout_service.py`:
  - Orchestrates: validate cart → create Stripe session → return payment URL
  - On webhook success: trigger SAP `place_order`
  - Handle failures: mark checkout as failed, allow retry
- [ ] Add routes in `app/api/checkout.py`:
  - `POST /checkout/create` — agent calls this when user confirms order
  - `POST /checkout/webhook` — Stripe calls this after payment
  - `GET /checkout/status/{session_id}` — poll payment status
- [ ] Add env vars: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_SUCCESS_URL`, `STRIPE_CANCEL_URL`

### Task 2.2 — Agent checkout flow update
**Status**: [x] DONE (2026-03-19) — initiate_checkout tool, updated system prompt
**Priority**: P0

- [ ] Remove `set_payment_details` tool from agent (no more card info in chat)
- [ ] Add new `initiate_checkout` tool: creates Stripe session, returns payment URL
- [ ] Update system prompt: agent should NOT ask for payment details, instead trigger secure checkout
- [ ] Update human-approval node: after approval, create Stripe session instead of calling `place_order` directly
- [ ] `place_order` triggered ONLY by Stripe webhook, never by agent directly

### Task 2.3 — Checkout UI in chat
**Status**: [ ] TODO
**Priority**: P0

- [ ] When agent triggers checkout, chat UI renders:
  ```
  ┌─────────────────────────────────┐
  │  Order Summary                  │
  │  ─────────────────────────────  │
  │  Canon EOS 450D        $574.88  │
  │  Shipping (Standard)     $9.99  │
  │  Tax                    $46.79  │
  │  ─────────────────────────────  │
  │  Total                 $631.66  │
  │                                 │
  │  📍 123 Main St, New York, NY   │
  │                                 │
  │  [Pay Securely with Stripe]     │
  └─────────────────────────────────┘
  ```
- [ ] "Pay Securely" button opens Stripe Checkout in new tab/popup
- [ ] After payment, auto-detect success via polling or redirect
- [ ] Show order confirmation card in chat

---

## Phase 3: Smart State Management

### Task 3.1 — Cart persistence with Redis
**Status**: [ ] TODO
**Priority**: P1

- [ ] Create `app/integrations/redis_client.py`: connection pool, get/set/delete with TTL
- [ ] Create `app/services/cart_service.py`:
  - `save_cart(session_id, cart_data)` — persist to Redis (TTL: 24h)
  - `load_cart(session_id)` — restore cart on reconnect
  - `clear_cart(session_id)` — after order placed
- [ ] On WebSocket disconnect → cart persists in Redis
- [ ] On reconnect with same session_id → restore cart state
- [ ] Agent system prompt updated: "User has an existing cart with X items"

### Task 3.2 — Checkout progress tracking
**Status**: [ ] TODO
**Priority**: P1

- [ ] Enhance `CommerceState` enum with granular states:
  - `CHECKOUT_INITIATED`, `PAYMENT_PENDING`, `PAYMENT_COMPLETED`, `ORDER_PLACING`, `ORDER_CONFIRMED`, `ORDER_FAILED`
- [ ] Persist state transitions in Redis with timestamps
- [ ] On failure at any step → record failure state + reason
- [ ] On resume → agent knows exactly where checkout left off:
  - "You were at the payment step. Want to continue?"
- [ ] Add `GET /checkout/resume/{session_id}` endpoint

### Task 3.3 — Partial failure recovery
**Status**: [ ] TODO
**Priority**: P1

- [ ] If Stripe payment succeeds but SAP `place_order` fails:
  - Store payment confirmation in Redis
  - Retry SAP order placement (3 attempts with backoff)
  - If still fails → alert + manual resolution queue
  - Never charge without fulfillment
- [ ] If SAP cart expires mid-checkout:
  - Recreate cart from Redis-persisted items
  - Re-apply address + delivery mode
  - Resume from payment step

---

## Phase 4: Order History & One-Click Reorder

### Task 4.1 — Order history storage
**Status**: [ ] TODO
**Priority**: P2

- [ ] Store completed orders in Redis (per user, TTL: 30 days):
  - Order code, items, total, address, delivery mode, timestamp
- [ ] `GET /orders/history?session_id=xxx` endpoint
- [ ] Agent can query: "Your recent orders: ..."

### Task 4.2 — One-click reorder
**Status**: [ ] TODO
**Priority**: P2

- [ ] Save delivery addresses per user in Redis
- [ ] Agent tool `reorder(order_code)`:
  - Fetch previous order details
  - Create new cart with same items
  - Pre-fill saved address
  - Skip to payment step
- [ ] "Reorder your Canon EOS 450D? Same address?" → one confirmation → checkout

### Task 4.3 — Saved addresses
**Status**: [ ] TODO
**Priority**: P2

- [ ] Store addresses per user session in Redis
- [ ] Agent offers saved addresses: "Ship to your usual address (123 Main St)?"
- [ ] User can choose saved or enter new

---

## Phase 5: Better Error Recovery & Resilience

### Task 5.1 — Retry with exponential backoff for SAP calls
**Status**: [ ] TODO
**Priority**: P1

- [ ] Create `app/middleware/error_handler.py`:
  - `@with_retry(max_attempts=3, backoff=2)` decorator
  - Retries on: connection errors, timeouts, 500/502/503
  - Does NOT retry on: 400/401/404 (client errors)
- [ ] Apply to all SAP HTTP calls in `sap_client.py`
- [ ] Circuit breaker integration (already exists, refactor into middleware)

### Task 5.2 — Graceful degradation
**Status**: [ ] TODO
**Priority**: P2

- [ ] If SAP is down during product search → return cached results from Qdrant
- [ ] If Redis is down → fall back to in-memory session store (current behavior)
- [ ] If Stripe is unreachable → show error + offer to retry later
- [ ] All failures logged with correlation IDs for debugging

---

## Phase 6: Rich Checkout Summary UI

### Task 6.1 — Cart summary card component
**Status**: [ ] TODO
**Priority**: P1

- [ ] Create rich HTML card rendered in chat when cart changes:
  - Product image, name, quantity, price per item
  - Subtotal, shipping, tax, total
  - Edit quantity / remove item buttons
- [ ] Rendered as a special message type (not plain text)

### Task 6.2 — Order confirmation card
**Status**: [ ] TODO
**Priority**: P1

- [ ] After successful order:
  - Order number, total, estimated delivery
  - Link to order tracking (SAP permalink)
  - "Reorder" button
- [ ] Rendered as a rich card in chat

### Task 6.3 — Checkout progress indicator
**Status**: [ ] TODO
**Priority**: P2

- [ ] Visual steps in chat UI:
  ```
  ✅ Cart → ✅ Address → ✅ Shipping → 🔵 Payment → ○ Confirmation
  ```
- [ ] Updates as checkout progresses
- [ ] Clickable to jump back to a step

---

## Implementation Order

| Order | Task | Depends On | Effort |
|-------|------|-----------|--------|
| 1 | 1.1 Project restructure | — | Large |
| 2 | 1.2 Frontend split | 1.1 | Medium |
| 3 | 2.1 Stripe backend | 1.1 | Medium |
| 4 | 2.2 Agent flow update | 2.1 | Medium |
| 5 | 2.3 Checkout UI | 1.2, 2.1 | Medium |
| 6 | 3.1 Redis cart persistence | 1.1 | Medium |
| 7 | 3.2 Checkout progress tracking | 3.1, 2.1 | Small |
| 8 | 3.3 Partial failure recovery | 3.1, 2.1 | Medium |
| 9 | 5.1 Retry + backoff | 1.1 | Small |
| 10 | 6.1 Cart summary card | 1.2 | Medium |
| 11 | 6.2 Order confirmation card | 1.2, 2.1 | Small |
| 12 | 4.1 Order history | 3.1 | Small |
| 13 | 4.2 One-click reorder | 4.1 | Medium |
| 14 | 4.3 Saved addresses | 3.1 | Small |
| 15 | 6.3 Checkout progress indicator | 1.2, 3.2 | Small |
| 16 | 5.2 Graceful degradation | 5.1 | Small |

---

## Current Progress

**Last completed**: Task 1.1 (restructure), Task 2.1 (Stripe backend), Task 2.2 (agent flow).
**Next up**: Task 1.2 — Extract frontend into separate files (CSS/JS split).
**Blocked on**: Nothing.
**Resume command**: "continue from WORKPLAN.md"

---

## Notes

- All Stripe operations use `stripe` Python SDK
- Redis used for: sessions, carts, order history, saved addresses, checkout state
- SAP Commerce remains the source of truth for inventory, pricing, orders
- Agent NEVER sees payment details — Stripe handles all PCI-sensitive data
- Frontend uses vanilla JS (no framework) — keep it lightweight
