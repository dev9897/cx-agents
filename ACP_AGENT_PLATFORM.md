# ACP Agent Platform — One-Click Purchase Implementation Plan

**Branch**: `feature/secure-checkout-refactor`
**Created**: 2026-03-19
**Goal**: Enable one-click in-chat purchases using ACP with saved cards (Stripe SetupIntent + PaymentIntent)

---

## Architecture

```
Save card once (Settings panel, Stripe Elements)
        |
User: "checkout" -> Agent checks saved cards -> Shows order summary
        |
User: "Confirm" -> Agent creates ACP session -> Charges saved card -> SAP order placed
        |
Order confirmation in chat. No redirect. No card entry.
```

### Roles

- **Agent Platform** (your LangGraph agent): holds user's saved payment methods, orchestrates checkout
- **Seller** (your ACP endpoints): receives payment token, charges via Stripe, places SAP order
- **Stripe**: stores cards (SetupIntent), processes payments (PaymentIntent), never exposes card data

### No PCI DSS needed

Stripe.js handles all card input client-side. Your server only sees `pm_xxx` IDs (opaque references).

---

## Implementation Tasks

### Phase 1: Backend — Stripe Customer & Card Management

#### Task 1.1 — Payment models (`app/models/payment.py`)
**Status**: [x] DONE (2026-03-19)
- SavedCard, SavedAddress, SetupIntentResponse, CardListResponse, AddressListResponse

#### Task 1.2 — Config update (`app/config.py`)
**Status**: [x] DONE (2026-03-19)
- Add `STRIPE_PUBLISHABLE_KEY` to StripeConfig

#### Task 1.3 — Stripe client extensions (`app/integrations/stripe_client.py`)
**Status**: [x] DONE (2026-03-19)
- `create_customer(email, name)` — create Stripe Customer
- `get_or_create_customer(email, name)` — idempotent lookup/create
- `create_setup_intent(customer_id)` — for client-side card saving
- `list_payment_methods(customer_id)` — list saved cards
- `detach_payment_method(pm_id)` — remove a saved card
- `create_payment_intent(customer_id, pm_id, amount, currency)` — charge saved card off-session

#### Task 1.4 — Payment service (`app/services/payment_service.py`)
**Status**: [x] DONE (2026-03-19)
- `ensure_stripe_customer(session_id, email, name)` — get/create Stripe Customer, store in Redis
- `create_card_setup(session_id)` — create SetupIntent, return client_secret
- `list_saved_cards(session_id)` — list user's saved cards
- `remove_card(session_id, pm_id)` — detach a card
- `charge_saved_card(customer_id, pm_id, amount, currency, metadata)` — charge via PaymentIntent
- Redis keys: `stripe_customer:{email}`, `saved_addresses:{email}`

#### Task 1.5 — Payment API routes (`app/api/payment.py`)
**Status**: [x] DONE (2026-03-19)
- `GET /payment/config` — returns publishable key
- `POST /payment/setup-intent` — creates SetupIntent for card saving
- `GET /payment/cards` — lists saved cards
- `DELETE /payment/cards/{pm_id}` — removes a card
- `GET /payment/addresses` — lists saved addresses
- `POST /payment/addresses` — saves new address
- `DELETE /payment/addresses/{id}` — removes address

#### Task 1.6 — Register payment router (`app/main.py`)
**Status**: [x] DONE (2026-03-19)

---

### Phase 2: ACP Integration — Real Stripe Charges

#### Task 2.1 — Modify `acp/service.py` for real payments
**Status**: [x] DONE (2026-03-19)
- Replace placeholder card `4111...` in `_sap_set_payment_details` with actual Stripe charge
- In `complete_checkout`: charge saved card via PaymentIntent BEFORE SAP order
- If Stripe charge fails → return PAYMENT_DECLINED, don't place SAP order
- If SAP order fails after Stripe charge → refund PaymentIntent
- Add `stripe_customer_id` to `_ACPSession`

---

### Phase 3: Agent Flow — One-Click Tools

#### Task 3.1 — New agent tools (`app/agent/tools.py`)
**Status**: [x] DONE (2026-03-19)
- `acp_checkout` tool: creates ACP session → charges saved card → places order (all in one)
- `list_saved_cards` tool: returns user's saved payment methods
- Keep `initiate_checkout` as fallback for users without saved cards

#### Task 3.2 — Agent graph updates (`app/agent/graph.py`)
**Status**: [x] DONE (2026-03-19)
- Route `acp_checkout` through `human_approval` node (same as `place_order`)
- Handle `acp_checkout` in approval node

#### Task 3.3 — Agent prompts (`app/agent/prompts.py`)
**Status**: [x] DONE (2026-03-19)
- New checkout instructions: check saved cards first → one-click if available → Stripe redirect if not
- Updated checkout sequence in system prompt

#### Task 3.4 — Agent state (`app/agent/state.py`)
**Status**: [x] DONE (2026-03-19)
- Add `stripe_customer_id`, `saved_payment_methods` fields

---

### Phase 4: Frontend — Settings Panel & Stripe Elements

#### Task 4.1 — Settings panel with card management
**Status**: [x] DONE (2026-03-19)
- Settings modal (gear icon in header)
- List saved cards (brand, last4, expiry, remove button)
- "Add New Card" with Stripe Elements CardElement
- Load Stripe.js, mount CardElement, confirmCardSetup

#### Task 4.2 — Address management UI
**Status**: [x] DONE (2026-03-19)
- List saved addresses in settings panel
- Add/remove addresses

#### Task 4.3 — Updated checkout flow in chat
**Status**: [x] DONE (2026-03-20)
- One-click confirm card in chat (no redirect)
- Order confirmation card after successful purchase

---

## Implementation Order

| Step | Task | Depends On | Effort |
|------|------|-----------|--------|
| 1 | 1.1 Payment models | — | Small |
| 2 | 1.2 Config update | — | Trivial |
| 3 | 1.3 Stripe client extensions | — | Medium |
| 4 | 1.4 Payment service | 1.1, 1.3 | Medium |
| 5 | 1.5 Payment API routes | 1.4 | Medium |
| 6 | 1.6 Register router | 1.5 | Trivial |
| 7 | 2.1 ACP real payments | 1.4 | Medium |
| 8 | 3.4 Agent state | — | Trivial |
| 9 | 3.1 Agent tools | 1.4, 2.1 | Medium |
| 10 | 3.2 Agent graph | 3.1 | Small |
| 11 | 3.3 Agent prompts | 3.1 | Small |
| 12 | 4.1 Settings + Stripe Elements | 1.5 | Large |
| 13 | 4.2 Address management | 1.5 | Medium |
| 14 | 4.3 Checkout flow UI | 3.1 | Medium |

---

## Key Design Decisions

1. **PaymentMethod ID as ACP token** — `PaymentData.token = "pm_xxx"`. ACP seller charges it via PaymentIntent.
2. **Fallback preserved** — Users without saved cards use Stripe Checkout redirect (`initiate_checkout`).
3. **Human approval required** — `acp_checkout` goes through LangGraph interrupt.
4. **3DS handling** — If card requires 3D Secure, return Stripe auth URL (edge case, handled later).
5. **Refund on SAP failure** — If Stripe charge succeeds but SAP order fails, auto-refund.
6. **Redis for customer mapping** — `stripe_customer:{email} → cus_xxx` (TTL: 30 days).

---

## Resume

**Resume command**: "continue from ACP_AGENT_PLATFORM.md"
