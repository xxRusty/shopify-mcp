# server.py — Universal Shopify Admin API MCP (read-only, multi-store)
# 6 generic tools covering 100% of the Admin GraphQL API read surface.
# Supports:
#   - Multiple stores per container via SHOPIFY_STORES JSON
#   - Single-store fallback via SHOPIFY_DOMAIN + client_creds or access_token
#   - Client-credentials OAuth flow (Dev Dashboard custom apps, 2026-01+)
#   - Legacy shpat_ token fallback (pre-2026 custom apps)
#   - Read-only GraphQL enforcement via graphql-core
#   - Cost-based throttle awareness with Retry-After-style sleep
#   - Bulk Operations API (async exports)
#   - ShopifyQL analytics wrapper

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
import requests
from typing import Any
import json
import os
import time
import logging
import math
import re

from graphql import parse, GraphQLSyntaxError
from graphql.language.ast import OperationDefinitionNode

# --- Constants ---
SHOPIFY_API_VERSION = "2026-04"
ADMIN_PATH_TEMPLATE = "/admin/api/{version}/graphql.json"
TOKEN_PATH = "/admin/oauth/access_token"

SHOP_DOMAIN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
API_VERSION_PATTERN = re.compile(r"^(\d{4}-\d{2}|unstable)$")
OPERATION_ID_PATTERN = re.compile(r"^gid://shopify/BulkOperation/\d+$")
TYPE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
STORE_ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

REQUEST_TIMEOUT_ENV_VAR = "SHOPIFY_REQUEST_TIMEOUT_SECONDS"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
MAX_QUERY_BYTES = 100_000
MAX_THROTTLE_RETRIES = 3
TOKEN_REFRESH_BUFFER_SECONDS = 120.0
SINGLE_STORE_ALIAS = "default"

# Parser allowlist — `bulkOperationCancel` kills an in-flight bulk job (no
# shop-data write). `bulkOperationRunQuery` is NOT allowed here; it takes a
# sub-query as a string argument which the parser cannot inspect. Legitimate
# bulk exports go through `shopify_bulk_query`, which validates the inner
# query via `_assert_read_only` BEFORE wrapping it in the bulk mutation.
ALLOWED_MUTATIONS = frozenset({"bulkOperationCancel"})

REDACTION_PATTERNS = (
    re.compile(r"(?i)(X-Shopify-Access-Token\s*:\s*)(\S+)"),
    re.compile(r"(?i)([\"']access_token[\"']\s*:\s*[\"'])([^\"']+)([\"'])"),
    re.compile(r"(?i)([\"']client_secret[\"']\s*:\s*[\"'])([^\"']+)([\"'])"),
    re.compile(r"(?i)(client_secret=)([^&\s]+)"),
    re.compile(r"\b(shpua_|shpat_|shpss_|shpca_)[A-Za-z0-9]+\b"),
)

mcp = FastMCP("shopify")
LOGGER = logging.getLogger(__name__)

# Store registry populated at startup. Shape:
# {alias: {"domain": str, "client_id": str?, "client_secret": str?, "access_token": str?}}
_STORES: dict[str, dict[str, str]] = {}
# Per-store access token cache. Shape: {domain: {"token": str, "expires_at": float}}
_TOKEN_CACHE: dict[str, dict[str, Any]] = {}
# Per-store metadata cache. Shape: {domain: {"name": str|None, "currency": str|None}}.
# `name` is the merchant's own shop.name; None if the fetch failed.
_STORE_META: dict[str, dict[str, str | None]] = {}


# --- Security helpers ---


def _redact(text: str) -> str:
    def _replace(m: re.Match[str]) -> str:
        if (m.lastindex or 0) >= 3:
            return f"{m.group(1)}***REDACTED***{m.group(3)}"
        if (m.lastindex or 0) >= 2:
            return f"{m.group(1)}***REDACTED***"
        return "***REDACTED***"
    for pat in REDACTION_PATTERNS:
        text = pat.sub(_replace, text)
    return text


def _get_timeout() -> float:
    raw = os.environ.get(REQUEST_TIMEOUT_ENV_VAR)
    if raw is None:
        return DEFAULT_REQUEST_TIMEOUT_SECONDS
    try:
        t = float(raw)
        if not math.isfinite(t) or t <= 0:
            raise ValueError
        return t
    except ValueError:
        LOGGER.warning(
            "Invalid %s=%r, using %.1fs",
            REQUEST_TIMEOUT_ENV_VAR,
            raw,
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        return DEFAULT_REQUEST_TIMEOUT_SECONDS


def _normalize_shop_domain(raw: str) -> str:
    """Strip scheme/trailing-slash/case; validate *.myshopify.com shape."""
    s = raw.strip().lower()
    if s.startswith("https://"):
        s = s[8:]
    elif s.startswith("http://"):
        s = s[7:]
    s = s.rstrip("/")
    if not SHOP_DOMAIN_PATTERN.fullmatch(s):
        raise ValueError(
            f"Invalid shop domain '{raw}'. "
            f"Expected format: '<shop>.myshopify.com' (admin domain, not custom storefront)."
        )
    return s


def _validate_api_version(version: str) -> str:
    if not API_VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            f"Invalid API version '{version}'. Expected 'YYYY-MM' (e.g. '2026-04') or 'unstable'."
        )
    return version


def _admin_graphql_url(shop: str, version: str) -> str:
    return f"https://{shop}{ADMIN_PATH_TEMPLATE.format(version=version)}"


def _token_url(shop: str) -> str:
    return f"https://{shop}{TOKEN_PATH}"


# --- Store registry ---


def _load_stores_from_json_env() -> dict[str, dict[str, str]]:
    raw = os.environ.get("SHOPIFY_STORES")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"SHOPIFY_STORES is not valid JSON: {e.msg}") from e
    if not isinstance(parsed, dict):
        raise RuntimeError("SHOPIFY_STORES must be a JSON object keyed by alias")

    result: dict[str, dict[str, str]] = {}
    for alias, cfg in parsed.items():
        if not isinstance(alias, str) or not STORE_ALIAS_PATTERN.fullmatch(alias.lower()):
            raise RuntimeError(
                f"SHOPIFY_STORES alias '{alias}' invalid — "
                f"use lowercase alphanumeric/underscore/hyphen, <=64 chars"
            )
        if not isinstance(cfg, dict):
            raise RuntimeError(f"SHOPIFY_STORES['{alias}'] must be an object")
        domain = cfg.get("domain")
        if not domain:
            raise RuntimeError(f"SHOPIFY_STORES['{alias}'] missing 'domain'")
        domain = _normalize_shop_domain(domain)
        entry: dict[str, str] = {"domain": domain}
        if cfg.get("access_token"):
            entry["access_token"] = cfg["access_token"]
        if cfg.get("client_id") and cfg.get("client_secret"):
            entry["client_id"] = cfg["client_id"]
            entry["client_secret"] = cfg["client_secret"]
        if "access_token" not in entry and "client_id" not in entry:
            raise RuntimeError(
                f"SHOPIFY_STORES['{alias}'] needs either 'access_token' or "
                f"both 'client_id' and 'client_secret'"
            )
        result[alias.lower()] = entry
    return result


def _load_stores_from_prefix_env() -> dict[str, dict[str, str]]:
    """Scan env for SHOPIFY_STORE_<ALIAS>_DOMAIN keys and build a registry.

    Convention (Klaviyo-style):
        SHOPIFY_STORE_<ALIAS>_DOMAIN          (required per store)
        SHOPIFY_STORE_<ALIAS>_CLIENT_ID       (with _CLIENT_SECRET, Dev Dashboard)
        SHOPIFY_STORE_<ALIAS>_CLIENT_SECRET
        SHOPIFY_STORE_<ALIAS>_ACCESS_TOKEN    (legacy shpat_, alternative auth)

    Alias is whatever lies between `SHOPIFY_STORE_` and `_DOMAIN`, lowercased.
    Example: `SHOPIFY_STORE_MAIN_DOMAIN` → alias `main`.
    """
    domain_re = re.compile(r"^SHOPIFY_STORE_(.+)_DOMAIN$")
    result: dict[str, dict[str, str]] = {}
    for key, value in os.environ.items():
        m = domain_re.match(key)
        if not m or not value:
            continue
        alias_upper = m.group(1)
        alias = alias_upper.lower()
        if not STORE_ALIAS_PATTERN.fullmatch(alias):
            raise RuntimeError(
                f"Invalid alias in env key '{key}' — alias '{alias}' "
                f"must match {STORE_ALIAS_PATTERN.pattern}"
            )
        domain = _normalize_shop_domain(value)
        entry: dict[str, str] = {"domain": domain}
        access_token = os.environ.get(f"SHOPIFY_STORE_{alias_upper}_ACCESS_TOKEN")
        client_id = os.environ.get(f"SHOPIFY_STORE_{alias_upper}_CLIENT_ID")
        client_secret = os.environ.get(f"SHOPIFY_STORE_{alias_upper}_CLIENT_SECRET")
        if access_token:
            entry["access_token"] = access_token
        if client_id and client_secret:
            entry["client_id"] = client_id
            entry["client_secret"] = client_secret
        if "access_token" not in entry and "client_id" not in entry:
            raise RuntimeError(
                f"Store alias '{alias}' has SHOPIFY_STORE_{alias_upper}_DOMAIN "
                f"but missing auth. Set SHOPIFY_STORE_{alias_upper}_ACCESS_TOKEN "
                f"OR both SHOPIFY_STORE_{alias_upper}_CLIENT_ID and "
                f"SHOPIFY_STORE_{alias_upper}_CLIENT_SECRET."
            )
        result[alias] = entry
    return result


def _load_stores_from_single_env() -> dict[str, dict[str, str]]:
    raw = os.environ.get("SHOPIFY_SHOP_DOMAIN") or os.environ.get("SHOPIFY_DOMAIN")
    if not raw:
        return {}
    domain = _normalize_shop_domain(raw)
    entry: dict[str, str] = {"domain": domain}
    legacy = os.environ.get("SHOPIFY_ACCESS_TOKEN")
    client_id = os.environ.get("SHOPIFY_CLIENT_ID")
    client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET")
    if legacy:
        entry["access_token"] = legacy
    if client_id and client_secret:
        entry["client_id"] = client_id
        entry["client_secret"] = client_secret
    if "access_token" not in entry and "client_id" not in entry:
        return {}  # domain set but no auth — caller reports a better error
    return {SINGLE_STORE_ALIAS: entry}


def _build_registry() -> dict[str, dict[str, str]]:
    """Assemble the store registry. Precedence: JSON blob → prefix keys → single-store."""
    multi = _load_stores_from_json_env()
    if multi:
        return multi
    prefix = _load_stores_from_prefix_env()
    if prefix:
        return prefix
    single = _load_stores_from_single_env()
    return single


def _resolve_store(shop: str | None) -> tuple[str, dict[str, str]]:
    """Resolve a shop argument to (alias, config). Accepts alias or domain."""
    if not _STORES:
        raise RuntimeError(
            "No Shopify stores configured. Set SHOPIFY_STORES (JSON), "
            "SHOPIFY_STORE_<ALIAS>_DOMAIN + credentials (prefix keys), or "
            "SHOPIFY_DOMAIN + credentials (single-store fallback)."
        )
    if shop is None:
        if len(_STORES) == 1:
            alias = next(iter(_STORES))
            return alias, _STORES[alias]
        raise ValueError(
            f"Multiple stores configured ({sorted(_STORES)}); "
            f"pass `shop` as an alias or domain. "
            f"Call `shopify_list_stores` to see available aliases."
        )
    key = shop.strip().lower()
    if key in _STORES:
        return key, _STORES[key]
    # Maybe they passed a domain — try to find by domain match.
    try:
        candidate_domain = _normalize_shop_domain(key)
    except ValueError:
        candidate_domain = None
    if candidate_domain:
        for alias, cfg in _STORES.items():
            if cfg["domain"] == candidate_domain:
                return alias, cfg
    raise ValueError(
        f"Unknown shop '{shop}'. Known aliases: {sorted(_STORES)}. "
        f"Call `shopify_list_stores` to see configured stores."
    )


# --- Auth ---


def _exchange_client_credentials(cfg: dict[str, str]) -> tuple[str, float]:
    """Exchange client_id + client_secret for a ~24h access token."""
    domain = cfg["domain"]
    client_id = cfg.get("client_id")
    client_secret = cfg.get("client_secret")
    if not client_id or not client_secret:
        raise RuntimeError(
            f"Store '{domain}' needs client_id + client_secret for OAuth refresh, "
            f"or an access_token for legacy auth."
        )
    body = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }
    try:
        resp = requests.post(
            _token_url(domain),
            data=body,
            timeout=_get_timeout(),
            allow_redirects=False,
            headers={"Accept": "application/json"},
        )
        if 300 <= resp.status_code < 400:
            raise requests.exceptions.RequestException(
                "Redirect responses are not allowed"
            )
        resp.raise_for_status()
        payload = resp.json()
    except requests.exceptions.RequestException as e:
        safe_err = _redact(str(e))
        LOGGER.error("Token exchange failed for %s: %s", domain, safe_err)
        raise requests.exceptions.RequestException(
            f"Shopify token exchange failed: {safe_err}. "
            f"Verify the app is installed on {domain} via Dev Dashboard → Install on store."
        ) from e

    token = payload.get("access_token")
    expires_in = float(payload.get("expires_in", 86399))
    if not token:
        raise RuntimeError("Token exchange response missing access_token")
    return token, time.time() + expires_in - TOKEN_REFRESH_BUFFER_SECONDS


def _get_access_token(cfg: dict[str, str], force_refresh: bool = False) -> str:
    """Return a valid access token for the given store, refreshing if stale.

    Priority: per-store `access_token` (legacy shpat_) → client-credentials exchange.
    """
    if cfg.get("access_token"):
        return cfg["access_token"]

    domain = cfg["domain"]
    now = time.time()
    cache = _TOKEN_CACHE.get(domain)
    if (
        not force_refresh
        and cache is not None
        and cache.get("token")
        and now < cache.get("expires_at", 0.0)
    ):
        return cache["token"]

    token, expires_at = _exchange_client_credentials(cfg)
    _TOKEN_CACHE[domain] = {"token": token, "expires_at": expires_at}
    return token


def _invalidate_token(domain: str) -> None:
    _TOKEN_CACHE.pop(domain, None)


def _fetch_shop_meta(cfg: dict[str, str]) -> dict[str, str | None]:
    """Fetch shop name + currency via `query { shop { name currencyCode } }`.

    Never raises — returns {name: None, currency: None} on failure so a single
    misconfigured store doesn't break `shopify_list_stores` for the rest.
    """
    query = "{ shop { name currencyCode } }"
    try:
        payload = _graphql_call(cfg, query, None, None)
    except Exception as e:
        LOGGER.warning("shop-meta fetch failed for %s: %s", cfg["domain"], _redact(str(e)))
        return {"name": None, "currency": None}
    shop = ((payload.get("data") or {}).get("shop")) or {}
    return {"name": shop.get("name"), "currency": shop.get("currencyCode")}


# --- GraphQL enforcement ---


def _assert_read_only(query_str: str) -> None:
    """Parse GraphQL and reject non-read-only operations.

    Allows: query, fragment, allowed mutations (bulkOperationCancel only).
    Rejects: subscription, any other mutation, unparseable input.

    Note: `bulkOperationRunQuery` is blocked at this layer because its `query`
    string argument is not inspected by this parser. The dedicated
    `shopify_bulk_query` tool handles bulk exports safely — it validates the
    inner query with this same function BEFORE wrapping it in the bulk mutation.
    """
    if len(query_str.encode("utf-8")) > MAX_QUERY_BYTES:
        raise ValueError(f"Query exceeds {MAX_QUERY_BYTES} bytes")
    try:
        doc = parse(query_str)
    except GraphQLSyntaxError as e:
        raise ValueError(f"Invalid GraphQL: {e.message}") from e

    for defn in doc.definitions:
        if not isinstance(defn, OperationDefinitionNode):
            continue
        op_kind = defn.operation.value
        if op_kind == "subscription":
            raise ValueError("Subscription operations are not supported")
        if op_kind == "mutation":
            for sel in defn.selection_set.selections:
                field_name = getattr(getattr(sel, "name", None), "value", None)
                if field_name not in ALLOWED_MUTATIONS:
                    raise ValueError(
                        f"Mutation '{field_name}' is not permitted. "
                        f"Read-only MCP allows only: {sorted(ALLOWED_MUTATIONS)}"
                    )


# --- HTTP call ---


def _sleep_for_throttle(cost: dict[str, Any] | None) -> float:
    if not cost:
        return 1.0
    status = cost.get("throttleStatus") or {}
    requested = float(cost.get("requestedQueryCost") or 0)
    available = float(status.get("currentlyAvailable") or 0)
    restore = float(status.get("restoreRate") or 50)
    deficit = max(requested - available, 0.0)
    return max(math.ceil(deficit / max(restore, 1)), 1.0)


def _is_throttled(errors: list[dict[str, Any]] | None) -> bool:
    if not errors:
        return False
    for e in errors:
        code = ((e.get("extensions") or {}).get("code") or "").upper()
        if code == "THROTTLED":
            return True
    return False


def _extract_error_code(errors: list[dict[str, Any]] | None) -> str | None:
    if not errors:
        return None
    first = errors[0]
    return ((first.get("extensions") or {}).get("code") or "").upper() or None


def _graphql_call(
    cfg: dict[str, str],
    query: str,
    variables: dict[str, Any] | None = None,
    api_version: str | None = None,
    _retry_auth: bool = True,
) -> dict[str, Any]:
    """POST a GraphQL query to the given store with auth, throttle retry, and one auth re-mint."""
    domain = cfg["domain"]
    version = _validate_api_version(api_version or SHOPIFY_API_VERSION)
    url = _admin_graphql_url(domain, version)
    body: dict[str, Any] = {"query": query}
    if variables is not None:
        if not isinstance(variables, dict):
            raise ValueError("variables must be a JSON object (dict)")
        body["variables"] = variables

    for attempt in range(1, MAX_THROTTLE_RETRIES + 1):
        token = _get_access_token(cfg)
        headers = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = requests.post(
                url,
                data=json.dumps(body),
                headers=headers,
                timeout=_get_timeout(),
                allow_redirects=False,
            )
            if 300 <= resp.status_code < 400:
                raise requests.exceptions.RequestException(
                    "Redirect responses are not allowed"
                )
            if resp.status_code == 401 and _retry_auth:
                LOGGER.warning("401 from %s — refreshing token once", url)
                _invalidate_token(domain)
                return _graphql_call(cfg, query, variables, api_version, _retry_auth=False)
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.RequestException as e:
            safe_err = _redact(str(e))
            LOGGER.error("GraphQL error %s: %s", url, safe_err)
            raise requests.exceptions.RequestException(
                f"Shopify GraphQL request failed ({domain}): {safe_err}"
            ) from e

        errors = payload.get("errors")
        if _is_throttled(errors) and attempt < MAX_THROTTLE_RETRIES:
            sleep_s = _sleep_for_throttle(payload.get("extensions", {}).get("cost"))
            LOGGER.warning(
                "Throttled on %s (attempt %d) — sleeping %.1fs",
                domain,
                attempt,
                sleep_s,
            )
            time.sleep(sleep_s)
            continue

        code = _extract_error_code(errors)
        if code == "ACCESS_DENIED" and _retry_auth:
            LOGGER.warning("ACCESS_DENIED from %s — refreshing token once", domain)
            _invalidate_token(domain)
            return _graphql_call(cfg, query, variables, api_version, _retry_auth=False)

        return payload

    return payload


# --- Tools ---


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Shopify stores",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def shopify_list_stores(refresh: bool = False) -> str:
    """List all Shopify stores configured for this agent.

    Returns a JSON array of `{alias, domain, name, currency, auth_mode}`. The
    `name` field is the merchant's own shop name from Shopify (fetched once per
    process, cached). Use this list to match user intent — e.g., a user saying
    "check the outlet" maps to the store whose `name` or `domain` contains
    "outlet".

    If only one store is configured, `shop` can be omitted on tool calls.

    Args:
        refresh: If True, bypass the shop-name cache and re-fetch from Shopify.
            Use after a merchant renames a shop in the Shopify admin.
    """
    if refresh:
        _STORE_META.clear()
    out = []
    for alias, cfg in sorted(_STORES.items()):
        domain = cfg["domain"]
        if domain not in _STORE_META:
            _STORE_META[domain] = _fetch_shop_meta(cfg)
        meta = _STORE_META[domain]
        auth = (
            "access_token"
            if cfg.get("access_token")
            else "client_credentials"
            if cfg.get("client_id")
            else "none"
        )
        out.append({
            "alias": alias,
            "domain": domain,
            "name": meta.get("name"),
            "currency": meta.get("currency"),
            "auth_mode": auth,
        })
    return json.dumps(out, indent=2)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Run read-only Shopify GraphQL query",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def shopify_graphql_query(
    query: str,
    variables: dict[str, Any] | None = None,
    shop: str | None = None,
    api_version: str | None = None,
) -> str:
    """Execute a read-only GraphQL query against a Shopify store's Admin API.

    Universal entry point for reading any data the token's scopes permit:
    products, variants, orders, customers, inventory, fulfillments, discounts,
    locations, markets, metafields, metaobjects, segments, shop settings, etc.

    Full GraphQL reference: https://shopify.dev/docs/api/admin-graphql

    Write mutations are rejected by design. The query string is parsed and
    validated as read-only before transmission.

    Idiomatic patterns:
      - Pagination: `first: <=250`, `after: <cursor>`, read `pageInfo { hasNextPage endCursor }`.
      - Search: pass a Shopify search string to the `query:` arg on connections,
        e.g. `orders(first: 100, query: "created_at:>=2026-04-01 financial_status:paid")`.
      - Money: `totalPriceSet { shopMoney { amount currencyCode } }`.
      - For datasets >10k records, use `shopify_bulk_query` instead.

    Args:
        query: GraphQL document. Must be a `query` or a `fragment`. Subscription
            and mutation operations are rejected — with one narrow exception,
            `bulkOperationCancel`. For bulk exports, use `shopify_bulk_query`.
        variables: Optional variables dict passed as GraphQL `variables`.
        shop: Store alias (from `shopify_list_stores`) or domain. Required when
            multiple stores are configured; optional (auto-selected) when there's
            only one store.
        api_version: Override API version (default "2026-04"). Format "YYYY-MM".
    """
    _assert_read_only(query)
    _, cfg = _resolve_store(shop)
    data = _graphql_call(cfg, query, variables, api_version)
    return json.dumps(data, indent=2)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Introspect Shopify GraphQL schema",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def shopify_graphql_introspect(
    type_name: str | None = None,
    shop: str | None = None,
    api_version: str | None = None,
) -> str:
    """Introspect a Shopify store's Admin GraphQL schema.

    Pass `type_name` to fetch a single type's fields (cheap, ~50 cost points).
    Omit it for the full schema type catalog (expensive, ~800 cost points —
    use sparingly).

    Args:
        type_name: GraphQL type name (e.g. "Order", "Product", "Customer").
            Must match `[A-Za-z_][A-Za-z0-9_]*`. Omit for full schema.
        shop: Store alias or domain (see `shopify_list_stores`). Required when
            multiple stores are configured.
        api_version: Override API version (default "2026-04").
    """
    _, cfg = _resolve_store(shop)
    if type_name is not None:
        if not TYPE_NAME_PATTERN.fullmatch(type_name):
            raise ValueError(f"Invalid type_name '{type_name}'")
        query = """
        query IntrospectType($name: String!) {
          __type(name: $name) {
            name kind description
            fields(includeDeprecated: false) {
              name description
              args { name description type { name kind ofType { name kind } } }
              type { name kind ofType { name kind ofType { name kind } } }
            }
            inputFields {
              name description
              type { name kind ofType { name kind } }
            }
            enumValues { name description }
            interfaces { name }
            possibleTypes { name }
          }
        }
        """
        data = _graphql_call(cfg, query, {"name": type_name}, api_version)
    else:
        query = """
        query IntrospectSchema {
          __schema {
            queryType { name }
            mutationType { name }
            subscriptionType { name }
            types {
              name kind description
            }
          }
        }
        """
        data = _graphql_call(cfg, query, None, api_version)
    return json.dumps(data, indent=2)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Launch read-only Shopify bulk export",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def shopify_bulk_query(
    query: str,
    shop: str | None = None,
    api_version: str | None = None,
) -> str:
    """Launch an async bulk export of a read-only GraphQL query.

    Use for exporting datasets too large for paginated queries (>10k records,
    or anything you'd otherwise paginate hundreds of times). Shopify runs the
    query in the background and produces a JSONL file with all results.

    Restrictions (enforced by Shopify):
      - Exactly one top-level connection per query.
      - Max 5 total connections, max depth 2.
      - Every nested connection node must select `id` without an alias.
      - API ≤ 2025-10: one bulk op at a time per shop. API ≥ 2026-01: up to 5.

    Returns the BulkOperation ID — poll with `shopify_bulk_poll`.

    Args:
        query: Read-only GraphQL document with a single root connection.
        shop: Store alias or domain. Required when multiple stores are configured.
        api_version: Override API version (default "2026-04").
    """
    _assert_read_only(query)
    _, cfg = _resolve_store(shop)
    mutation = """
    mutation BulkExport($q: String!) {
      bulkOperationRunQuery(query: $q) {
        bulkOperation {
          id status createdAt
          query
          objectCount fileSize
        }
        userErrors { field message }
      }
    }
    """
    data = _graphql_call(cfg, mutation, {"q": query}, api_version)
    return json.dumps(data, indent=2)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Poll Shopify bulk operation status",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def shopify_bulk_poll(
    operation_id: str,
    shop: str | None = None,
    api_version: str | None = None,
) -> str:
    """Poll a bulk operation's status by ID.

    Statuses: CREATED, RUNNING, COMPLETED, CANCELED, EXPIRED, FAILED.
    On COMPLETED, the `url` field is a pre-signed JSONL download (valid 7 days).
    On FAILED, `partialDataUrl` may hold partial results; `errorCode` explains why.

    Args:
        operation_id: Global ID of the bulk operation
            (format: `gid://shopify/BulkOperation/<numeric-id>`).
        shop: Store alias or domain the bulk op was launched on. Required when
            multiple stores are configured.
        api_version: Override API version (default "2026-04").
    """
    if not OPERATION_ID_PATTERN.fullmatch(operation_id):
        raise ValueError(
            f"Invalid operation_id '{operation_id}'. "
            f"Expected 'gid://shopify/BulkOperation/<numeric-id>'."
        )
    _, cfg = _resolve_store(shop)
    query = """
    query PollBulk($id: ID!) {
      node(id: $id) {
        ... on BulkOperation {
          id status errorCode createdAt completedAt
          objectCount fileSize
          url partialDataUrl
          query
          type
        }
      }
    }
    """
    data = _graphql_call(cfg, query, {"id": operation_id}, api_version)
    return json.dumps(data, indent=2)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Run ShopifyQL analytics query",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
def shopify_shopifyql(
    query: str,
    shop: str | None = None,
    api_version: str | None = None,
) -> str:
    """Run a ShopifyQL analytics query against a Shopify store.

    ShopifyQL is Shopify's SQL-like reporting language. Requires `read_reports`
    scope. Consumes the same cost bucket as GraphQL.

    Syntax:
      FROM <dataset> SHOW <metric>[, <metric>...]
        [BY <dimension>] [GROUP BY <dim>] [WHERE <filter>]
        [SINCE -Nd UNTIL today] [ORDER BY <col> [ASC|DESC]] [LIMIT N]

    Datasets: sales, orders, products, customers, inventory, sessions.
    Time tokens: -Nd / -Nw / -Nm / -Nq / -Ny, or named (today, yesterday,
      this_week, last_week, this_month, last_month, last_year).

    Examples:
      - `FROM sales SHOW total_sales GROUP BY day SINCE -7d UNTIL today ORDER BY day ASC`
      - `FROM sales SHOW total_sales BY product_title ORDER BY total_sales DESC LIMIT 10 SINCE -30d`
      - `FROM sales SHOW returning_customer_rate GROUP BY month SINCE -6m`
      - `FROM sales SHOW net_sales SINCE -1q UNTIL today`
      - `FROM sessions SHOW sessions, conversion_rate GROUP BY referrer_source SINCE -14d`

    Returns `tableData.columns`, `tableData.rows`, and `parseErrors`.
    When parsing fails, `tableData` is `null` and `parseErrors` contains
    a list of human-readable error strings (e.g. "Column 'total_sale' not found").

    Args:
        query: ShopifyQL query string.
        shop: Store alias or domain. Required when multiple stores are configured.
        api_version: Override API version (default "2026-04").
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("ShopifyQL query must be a non-empty string")
    _, cfg = _resolve_store(shop)
    wrapped = """
    query ShopifyQL($q: String!) {
      shopifyqlQuery(query: $q) {
        tableData {
          columns { name displayName dataType }
          rows
        }
        parseErrors
      }
    }
    """
    data = _graphql_call(cfg, wrapped, {"q": query}, api_version)
    return json.dumps(data, indent=2)


# --- Main ---


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _STORES = _build_registry()
    if not _STORES:
        raise RuntimeError(
            "No Shopify stores configured. Choose one:\n"
            "  (a) SHOPIFY_STORES = JSON object, e.g. {\"main\":{\"domain\":\"x.myshopify.com\",\"client_id\":\"...\",\"client_secret\":\"...\"}}\n"
            "  (b) SHOPIFY_STORE_<ALIAS>_DOMAIN + SHOPIFY_STORE_<ALIAS>_CLIENT_ID + SHOPIFY_STORE_<ALIAS>_CLIENT_SECRET (one set per store)\n"
            "  (c) SHOPIFY_DOMAIN + (SHOPIFY_ACCESS_TOKEN OR SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET) — single store only"
        )
    LOGGER.info("Shopify MCP registered %d store(s): %s", len(_STORES), sorted(_STORES))
    mcp.run()
