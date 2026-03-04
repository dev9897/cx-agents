from schemas.session import CommerceState


def can_transition(current: CommerceState, target: CommerceState) -> bool:
    allowed = {
        CommerceState.ANONYMOUS: [CommerceState.AUTHENTICATED],
        CommerceState.AUTHENTICATED: [CommerceState.CART_CREATED],
        CommerceState.CART_CREATED: [CommerceState.ITEMS_ADDED],
        CommerceState.ITEMS_ADDED: [CommerceState.ADDRESS_SET],
        CommerceState.ADDRESS_SET: [CommerceState.DELIVERY_MODE_SET],
        CommerceState.DELIVERY_MODE_SET: [CommerceState.PAYMENT_SET],
        CommerceState.PAYMENT_SET: [CommerceState.READY_FOR_ORDER],
        CommerceState.READY_FOR_ORDER: [CommerceState.ORDER_PLACED],
    }

    return target in allowed.get(current, [])