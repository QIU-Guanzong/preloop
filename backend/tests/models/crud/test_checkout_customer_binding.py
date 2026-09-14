"""Checkout customer associations preserve identity and durable conflict evidence."""

import pytest

from preloop.models import models
from preloop.models.crud.billing import billing


@pytest.mark.parametrize(
    "existing,trusted,allowed",
    [
        (None, True, True),
        (None, False, False),
        ("cus_original", True, False),
        ("cus_paid", False, True),
    ],
)
def test_checkout_binding_requires_trusted_reference_and_never_replaces_customer(
    db_session, test_user, existing, trusted, allowed
):
    account = billing.lock_account(db_session, str(test_user.account_id))
    account.stripe_customer_id = existing
    db_session.flush()
    original_owner = account.primary_user_id
    for _ in range(2):
        assert (
            billing.bind_checkout_customer(
                db_session,
                account_id=str(account.id),
                customer_id="cus_paid",
                session_id="cs_paid",
                subscription_id="sub_paid",
                allow_association=trusted,
            )
            is allowed
        )
    db_session.refresh(account)
    assert account.stripe_customer_id == ("cus_paid" if allowed else existing)
    assert account.primary_user_id == original_owner
    rows = (
        db_session.query(models.BillingOperation)
        .filter_by(account_id=account.id, operation_key="checkout:cs_paid")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].result["status"] == (
        "associated" if allowed else "reconciliation_required"
    )
    assert rows[0].lease_until is None
    hold = billing.checkout_reconciliation_hold(
        db_session, customer_id="cus_paid", subscription_id="sub_paid"
    )
    assert (hold is None) is allowed
    assert (
        billing.checkout_reconciliation_hold(
            db_session, customer_id="cus_other", subscription_id="sub_paid"
        )
        is None
    )
    assert (
        billing.checkout_reconciliation_hold(
            db_session, customer_id="cus_paid", subscription_id="sub_other"
        )
        is None
    )


def test_checkout_replay_after_mapping_change_is_held(db_session, test_user):
    account_id = str(test_user.account_id)
    kwargs = dict(
        account_id=account_id,
        customer_id="cus_paid",
        session_id="cs_paid",
        subscription_id="sub_paid",
        allow_association=True,
    )
    assert billing.bind_checkout_customer(db_session, **kwargs)
    account = billing.lock_account(db_session, account_id)
    account.stripe_customer_id = "cus_repaired"
    db_session.flush()
    assert not billing.bind_checkout_customer(db_session, **kwargs)
    assert account.stripe_customer_id == "cus_repaired"
    assert (
        billing.checkout_reconciliation_hold(
            db_session, customer_id="cus_paid", subscription_id="sub_paid"
        )
        is not None
    )


def test_checkout_customer_owned_elsewhere_is_held(db_session, test_user):
    owner = models.Account(
        organization_name="existing customer owner", stripe_customer_id="cus_owned"
    )
    db_session.add(owner)
    db_session.flush()
    account_id = str(test_user.account_id)
    assert not billing.bind_checkout_customer(
        db_session,
        account_id=account_id,
        customer_id="cus_owned",
        session_id="cs_collision",
        subscription_id="sub_collision",
        allow_association=True,
    )
    db_session.refresh(owner)
    candidate = billing.lock_account(db_session, account_id)
    assert candidate.stripe_customer_id is None
    assert owner.stripe_customer_id == "cus_owned"
    hold = billing.checkout_reconciliation_hold(
        db_session, customer_id="cus_owned", subscription_id="sub_collision"
    )
    assert hold.account_id == test_user.account_id
    assert hold.result["reason"] == "customer_owned_by_another_account"
