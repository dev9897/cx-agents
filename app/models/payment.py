"""Payment models — saved cards, addresses, setup intents."""

from typing import Optional

from pydantic import BaseModel


class SavedCard(BaseModel):
    id: str                  # pm_xxx
    brand: str               # "visa", "mastercard"
    last4: str               # "4242"
    exp_month: int
    exp_year: int
    is_default: bool = False


class SavedAddress(BaseModel):
    id: str                  # uuid
    label: str = ""          # "Home", "Office"
    name: str
    line1: str
    line2: str = ""
    city: str
    state: str = ""
    postal_code: str
    country: str = "US"


class SetupIntentResponse(BaseModel):
    client_secret: str
    customer_id: str


class CardListResponse(BaseModel):
    cards: list[SavedCard]


class AddressListResponse(BaseModel):
    addresses: list[SavedAddress]
