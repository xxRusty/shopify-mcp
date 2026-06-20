"""
Tests for the read-only enforcement layer — the core safety guarantee of this MCP.

These run without any Shopify credentials: they import the server module and
exercise the pure validation functions directly. No network, no secrets.
"""

import os
import sys

import pytest

# Import the server module from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402


# ── Read-only parser enforcement ──────────────────────────────────────────────

VALID_READ_QUERIES = [
    "{ shop { name } }",
    "query { products(first: 10) { edges { node { id title } } } }",
    "query GetOrders { orders(first: 5) { edges { node { id } } } }",
    "{ customers(first: 1) { edges { node { email } } } }",
]

REJECTED_MUTATIONS = [
    'mutation { productCreate(input: {title: "x"}) { product { id } } }',
    "mutation { orderClose(input: {id: \"gid://shopify/Order/1\"}) { order { id } } }",
    'mutation { customerUpdate(input: {id: "gid://shopify/Customer/1"}) { customer { id } } }',
]


@pytest.mark.parametrize("query", VALID_READ_QUERIES)
def test_read_queries_pass(query):
    # Should not raise.
    server._assert_read_only(query)


@pytest.mark.parametrize("query", REJECTED_MUTATIONS)
def test_mutations_are_rejected(query):
    with pytest.raises(Exception):
        server._assert_read_only(query)


def test_bulk_operation_cancel_is_allowed():
    # The one permitted mutation: cancelling an in-flight bulk job (no data write).
    server._assert_read_only("mutation { bulkOperationCancel { bulkOperation { id } } }")


def test_bulk_operation_run_query_is_rejected_by_generic_parser():
    # bulkOperationRunQuery must NOT pass the generic guard — it goes through the
    # dedicated bulk tool, which validates the inner query first.
    q = 'mutation { bulkOperationRunQuery(query: "{ products { edges { node { id } } } }") { bulkOperation { id } } }'
    with pytest.raises(Exception):
        server._assert_read_only(q)


def test_malformed_query_is_rejected():
    with pytest.raises(Exception):
        server._assert_read_only("{ this is not valid graphql")


# ── Domain validation (SSRF defense) ──────────────────────────────────────────

def test_valid_shop_domain_normalizes():
    assert server._normalize_shop_domain("My-Store.myshopify.com") == "my-store.myshopify.com"
    assert server._normalize_shop_domain("https://my-store.myshopify.com/") == "my-store.myshopify.com"


@pytest.mark.parametrize("bad", [
    "evil.com",
    "my-store.myshopify.com.evil.com",
    "http://169.254.169.254/",
    "not a domain",
])
def test_invalid_shop_domain_rejected(bad):
    with pytest.raises(Exception):
        server._normalize_shop_domain(bad)


# ── API version validation ────────────────────────────────────────────────────

def test_valid_api_version():
    assert server._validate_api_version("2026-04") == "2026-04"


@pytest.mark.parametrize("bad", ["2026", "latest", "'; DROP TABLE", "2026-13-99"])
def test_invalid_api_version_rejected(bad):
    with pytest.raises(Exception):
        server._validate_api_version(bad)
