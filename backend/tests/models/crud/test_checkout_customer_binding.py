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


HOLD_FIELDS = {
    "account_id",
    "operation_id",
    "session_id",
    "subscription_id",
    "customer_id",
    "reason",
    "created_at",
}


def test_checkout_reconciliation_hold_queue_lists_reasons_pages_and_skips_associated(
    db_session, test_user
):
    owner = models.Account(
        organization_name="hold-queue-owner", stripe_customer_id="cus_queue_owned"
    )
    mismatch = models.Account(
        organization_name="hold-queue-mismatch", stripe_customer_id="cus_original"
    )
    collision = models.Account(organization_name="hold-queue-collision")
    associated = models.Account(organization_name="hold-queue-associated")
    db_session.add_all([owner, mismatch, collision, associated])
    db_session.flush()
    assert not billing.bind_checkout_customer(
        db_session,
        account_id=str(test_user.account_id),
        customer_id="cus_unbound",
        session_id="cs_ref",
        subscription_id="sub_ref",
        allow_association=False,
    )
    assert not billing.bind_checkout_customer(
        db_session,
        account_id=str(mismatch.id),
        customer_id="cus_new",
        session_id="cs_mismatch",
        subscription_id="sub_mismatch",
        allow_association=True,
    )
    assert not billing.bind_checkout_customer(
        db_session,
        account_id=str(collision.id),
        customer_id="cus_queue_owned",
        session_id="cs_collision_queue",
        subscription_id="sub_collision_queue",
        allow_association=True,
    )
    assert billing.bind_checkout_customer(
        db_session,
        account_id=str(associated.id),
        customer_id="cus_associated",
        session_id="cs_associated",
        subscription_id="sub_associated",
        allow_association=True,
    )
    operations = (
        db_session.query(models.BillingOperation)
        .order_by(models.BillingOperation.id)
        .all()
    )
    before = [
        (row.id, dict(row.result), dict(row.payload), row.lease_until, row.status)
        for row in operations
    ]
    customers = {
        str(account.id): account.stripe_customer_id
        for account in db_session.query(models.Account).all()
    }
    rows = billing.list_checkout_reconciliation_holds(db_session)
    assert {row["reason"] for row in rows} == {
        "account_reference_required",
        "customer_mismatch",
        "customer_owned_by_another_account",
    }
    assert {row["session_id"] for row in rows} == {
        "cs_ref",
        "cs_mismatch",
        "cs_collision_queue",
    }
    assert all(set(row) == HOLD_FIELDS for row in rows)
    assert all("email" not in row for row in rows)
    first = billing.list_checkout_reconciliation_holds(db_session, limit=1)
    assert len(first) == 1
    rest = billing.list_checkout_reconciliation_holds(
        db_session, after_id=first[0]["operation_id"]
    )
    assert first[0]["operation_id"] not in {row["operation_id"] for row in rest}
    assert len(first) + len(rest) == 3
    with pytest.raises(ValueError):
        billing.list_checkout_reconciliation_holds(db_session, limit=0)
    with pytest.raises(ValueError):
        billing.list_checkout_reconciliation_holds(db_session, limit=101)
    after = [
        (row.id, dict(row.result), dict(row.payload), row.lease_until, row.status)
        for row in db_session.query(models.BillingOperation)
        .order_by(models.BillingOperation.id)
        .all()
    ]
    assert before == after
    assert customers == {
        str(account.id): account.stripe_customer_id
        for account in db_session.query(models.Account).all()
    }
