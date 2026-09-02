"""How the Postgres URL is composed, and the empty string that broke it.

config reads the environment once at import, so every test here reloads the
module under a patched environ rather than poking at the constants.
"""

import importlib
import os

import pytest

from argus.core import config as _config


def load(**env):
    """Import config with exactly this environment overlaid."""
    keys = (
        "ARGUS_DATABASE_URL",
        "DATABASE_URL",
        "SUPABASE_REF",
        "SUPABASE_REGION",
        "SUPABASE_DB_PASSWORD",
    )
    saved = {k: os.environ.pop(k, None) for k in keys}
    os.environ.update({k: v for k, v in env.items() if v is not None})
    try:
        return importlib.reload(_config)
    finally:
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in saved.items() if v is not None})


@pytest.fixture(autouse=True)
def _restore():
    """Leave the module as it was found, whatever the test did to it."""
    yield
    importlib.reload(_config)


def test_no_password_means_sqlite():
    """A fresh clone with no configuration must not try to reach Postgres."""
    assert load().database_url() is None


def test_the_region_has_a_committed_default_but_the_ref_does_not():
    """The region is generic infrastructure and a fair default. The ref names
    one specific database on a public port and cannot be rotated, so it is
    supplied per environment and never committed."""
    c = load(SUPABASE_REF="proj", SUPABASE_DB_PASSWORD="pw")
    assert "postgres.proj:pw@aws-0-us-west-2." in c.database_url()


def test_a_password_without_a_ref_stays_on_sqlite():
    """Half a configuration is not a configuration. Composing a URL around a
    missing ref produced postgres.@aws-0-us-west-2 and failed at connect time
    rather than here."""
    assert load(SUPABASE_DB_PASSWORD="pw").database_url() is None


def test_an_empty_variable_falls_back_to_the_default():
    """The bug this file exists for. A workflow step written as

        SUPABASE_REF: ${{ vars.SUPABASE_REF }}

    sets the variable to "" when the repository has no such variable, and
    os.getenv returns "" for a present-but-empty key rather than the default.
    That composed `aws-0-.pooler.supabase.com` and failed to connect with an
    error naming DNS rather than configuration.
    """
    c = load(SUPABASE_REGION="", SUPABASE_REF="proj", SUPABASE_DB_PASSWORD="pw")
    assert "aws-0-us-west-2.pooler.supabase.com" in c.database_url()
    assert "aws-0-." not in c.database_url()


def test_an_empty_password_is_absent_not_blank():
    """Same shape, worse outcome: a blank password would compose a URL that
    looks valid and authenticates as nobody, instead of falling back."""
    assert load(SUPABASE_DB_PASSWORD="").database_url() is None


def test_an_explicit_url_wins_over_the_composed_one():
    c = load(
        ARGUS_DATABASE_URL="postgresql://x@localhost/y",
        SUPABASE_REF="proj",
        SUPABASE_DB_PASSWORD="pw",
    )
    assert c.database_url() == "postgresql://x@localhost/y"


def test_the_pooled_port_is_the_transaction_pooler():
    """Workers hold one connection for a long poll (5432); the API opens many
    short ones (6543). Swapping them exhausts the session pooler."""
    c = load(SUPABASE_REF="proj", SUPABASE_DB_PASSWORD="pw")
    assert ":5432/postgres" in c.database_url()
    assert ":6543/postgres" in c.database_url(pooled=True)
