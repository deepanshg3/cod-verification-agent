# COD Verification Voice AI

A Voice AI agent for automated **Cash-on-Delivery (COD) order verification**.

The system is designed around a Flipkart-style customer verification workflow where a voice agent calls a customer, verifies their identity, retrieves the authorized order, confirms the customer's intent to receive it, verifies the delivery location, and finally updates the order verification status.

The project combines a conversational Voice AI layer with a secure backend API and database authorization layer.

---

## Overview

COD orders can fail at the final stage when customers are unavailable, reject the order, or have an incorrect delivery location.

This project automates the verification process through a voice conversation while keeping the actual order operations behind a controlled backend.

The agent is responsible for the conversation, while the backend remains the source of truth for:

- Order information
- Call-to-order authorization
- Session expiration
- Address validation
- Verification status updates

The agent cannot directly modify sensitive order information such as the product, price, payment method, customer identity, or phone number.

---

## Architecture

```text
                         Customer
                            │
                            ▼
                    ┌───────────────┐
                    │   Bolna Voice │
                    │      AI       │
                    └───────┬───────┘
                            │
             ┌──────────────┼──────────────┐
             │              │              │
             ▼              ▼              ▼
      get_order_details  validate_address  update_order_verification
             │              │              │
             └──────────────┼──────────────┘
                            ▼
                    ┌───────────────┐
                    │    FastAPI    │
                    │    Backend    │
                    └───────┬───────┘
                            │
                    ┌───────┴────────┐
                    ▼                ▼
              Supabase          Nominatim
             PostgreSQL        Geocoding API
