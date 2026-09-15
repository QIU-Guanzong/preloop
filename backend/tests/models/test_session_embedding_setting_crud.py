"""Tests for the per account embedding opt in.

Enabling is the moment an account decides its session text may be sent to a
named endpoint, so the validation here is the guard on that decision: no
unknown provider, no unnamed model, no OpenAI compatible endpoint without a
base url, and no width the corpus column cannot store.
"""

import pytest

from preloop.models.crud import crud_session_embedding_setting
from preloop.models.crud.session_embedding_setting import SessionEmbeddingConfigError
from preloop.models.models.session_embedding_setting import (
    DEGRADED_DAILY_CAP,
    PROVIDER_LOCAL,
    PROVIDER_OPENAI_COMPATIBLE,
)
from preloop.models.models.session_search_document import EMBEDDING_DIMENSIONS


def test_an_account_that_never_asked_starts_disabled(db_session, test_user):
    """Off by default, and the row created on demand says so."""
    account_id = str(test_user.account_id)

    assert (
        crud_session_embedding_setting.get_for_account(
            db_session, account_id=account_id
        )
        is None
    )
    setting = crud_session_embedding_setting.get_or_create(
        db_session, account_id=account_id
    )
    assert setting.enabled is False
    assert setting.dimensions == EMBEDDING_DIMENSIONS


def test_enabling_names_the_provider_and_the_model(db_session, test_user):
    """The identity stored on every vector is built from the opt in."""
    account_id = str(test_user.account_id)

    setting = crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model_identifier="  text-embedding-3-small  ",
        base_url="https://embeddings.example.com/v1",
        daily_cap_usd=1.5,
    )

    assert setting.enabled is True
    assert setting.model_identifier == "text-embedding-3-small"
    assert setting.enabled_at is not None
    assert setting.model_identity == (
        f"openai_compatible:text-embedding-3-small@{EMBEDDING_DIMENSIONS}"
    )
    assert crud_session_embedding_setting.enabled_account_ids(db_session) == [
        account_id
    ]


def test_a_local_provider_keeps_no_base_url(db_session, test_user):
    """A local model runs in process; a base url would misstate where text goes."""
    setting = crud_session_embedding_setting.enable(
        db_session,
        account_id=str(test_user.account_id),
        provider=PROVIDER_LOCAL,
        model_identifier="all-MiniLM-L6-v2",
        base_url="https://ignored.example.com/v1",
    )

    assert setting.base_url is None


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"provider": "carrier-pigeon"}, "unknown_provider"),
        ({"model_identifier": "  "}, "model_required"),
        ({"base_url": None}, "base_url_required"),
        ({"dimensions": 512}, "unsupported_dimensions"),
        ({"daily_cap_usd": -1.0}, "invalid_daily_cap"),
    ],
)
def test_an_unusable_configuration_is_refused_at_the_opt_in(
    db_session, test_user, kwargs, code
):
    """A bad configuration fails when it is chosen, not on the worker thread."""
    base = {
        "provider": PROVIDER_OPENAI_COMPATIBLE,
        "model_identifier": "text-embedding-3-small",
        "base_url": "https://embeddings.example.com/v1",
    }
    base.update(kwargs)

    with pytest.raises(SessionEmbeddingConfigError) as excinfo:
        crud_session_embedding_setting.enable(
            db_session, account_id=str(test_user.account_id), **base
        )

    assert excinfo.value.code == code
    # Validation runs before anything is written, so a refused opt in leaves
    # no half configured row behind for a worker to read.
    assert (
        crud_session_embedding_setting.get_for_account(
            db_session, account_id=str(test_user.account_id)
        )
        is None
    )


def test_disabling_keeps_the_provider_details_for_a_re_enable(db_session, test_user):
    """Turning it off is not forgetting the decision that turned it on."""
    account_id = str(test_user.account_id)
    crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model_identifier="text-embedding-3-small",
        base_url="https://embeddings.example.com/v1",
    )

    setting = crud_session_embedding_setting.disable(db_session, account_id=account_id)

    assert setting is not None
    assert setting.enabled is False
    assert setting.base_url == "https://embeddings.example.com/v1"
    assert crud_session_embedding_setting.enabled_account_ids(db_session) == []


def test_a_degraded_marker_survives_until_a_run_clears_it(db_session, test_user):
    """Degraded is a state an operator can read, not a log line that scrolls."""
    account_id = str(test_user.account_id)
    crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model_identifier="text-embedding-3-small",
        base_url="https://embeddings.example.com/v1",
    )

    degraded = crud_session_embedding_setting.mark_degraded(
        db_session, account_id=account_id, reason=DEGRADED_DAILY_CAP
    )
    assert degraded is not None
    assert degraded.degraded_reason == DEGRADED_DAILY_CAP
    assert degraded.degraded_at is not None
    assert degraded.enabled is True

    cleared = crud_session_embedding_setting.clear_degraded(
        db_session, account_id=account_id
    )
    assert cleared is not None
    assert cleared.degraded_reason is None
    assert cleared.degraded_at is None
