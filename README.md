# Shopify MCP — Universal, Read-Only

A single MCP server exposing **100% of the Shopify Admin GraphQL API read surface** (version `2026-04`) through 6 universal tools. **Read-only is enforced at the query-parser level** — mutations are rejected before they ever reach Shopify, not merely discouraged. **Multi-store** by design: one server instance can serve many shops.

Built and maintained by [ScalablyAI](https://scalably.io). Runs on the [Model Context Protocol](https://modelcontextprotocol.io). License: MIT.

## Quick start

```bash
pip install -r requirements.txt

# Single store (simplest)
export SHOPIFY_DOMAIN="my-store.myshopify.com"
export SHOPIFY_ACCESS_TOKEN="shpat_..."   # or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET
python server.py
```

Then point any MCP client at the server. See **Authentication** below for multi-store and OAuth client-credentials setup.

---

## Tools (6)

| Tool | Description |
|------|-------------|
| `shopify_list_stores` | Lists configured stores. Agent calls first. |
| `shopify_graphql_query` | Arbitrary read-only GraphQL. Mutations rejected by the parser. |
| `shopify_graphql_introspect` | Schema introspection — full catalog or single type. |
| `shopify_bulk_query` | Launch async bulk export (JSONL). |
| `shopify_bulk_poll` | Poll bulk operation status + download URL. |
| `shopify_shopifyql` | ShopifyQL analytics (SQL-like; requires `read_reports`). |

Every non-list tool takes an optional `shop` argument (alias or domain). Required when >1 store configured; auto-selected when exactly 1.

## Coverage

100% of Admin GraphQL API read surface — any object, field, or connection accessible with the token's scopes is reachable via `shopify_graphql_query`. Anything large-scale (>10k records) should use `shopify_bulk_query`. Analytics goes through `shopify_shopifyql`.

## Authentication

### Multi-store (preferred for agency setups)

Set `SHOPIFY_STORES` to a JSON object mapping alias → store config:

```json
{
  "main":   {"domain": "my-store.myshopify.com",        "client_id": "...", "client_secret": "..."},
  "outlet": {"domain": "my-store-outlet.myshopify.com", "client_id": "...", "client_secret": "..."},
  "legacy": {"domain": "legacy-store.myshopify.com",    "access_token": "shpat_..."}
}
```

- Each store can use EITHER `client_id`+`client_secret` (Dev Dashboard OAuth, 24h tokens auto-refreshed per store) OR `access_token` (legacy `shpat_`).
- Aliases: `[a-z0-9][a-z0-9_-]{0,63}`. Lowercase-normalized on load.
- Token cache is per-store-domain; one throttled store doesn't block others.

### Single-store (backward-compat)

If `SHOPIFY_STORES` is unset, the MCP falls back to single-store env vars:

- `SHOPIFY_DOMAIN` or `SHOPIFY_SHOP_DOMAIN` — `<shop>.myshopify.com`
- Auth path A: `SHOPIFY_CLIENT_ID` + `SHOPIFY_CLIENT_SECRET` (Dev Dashboard)
- Auth path B: `SHOPIFY_ACCESS_TOKEN` (legacy `shpat_`)

The single store registers under alias `default` — callers can omit `shop` argument on tool calls.

## Read-only enforcement

Every query is parsed with `graphql-core` before transmission. The parser rejects:
- `subscription` operations (not supported by Admin API anyway)
- Any top-level `mutation` EXCEPT `bulkOperationCancel` (cancels an in-flight bulk job, no shop-data write)
- Malformed GraphQL (syntax errors)
- Queries > 100KB

`bulkOperationRunQuery` is **NOT** in the generic parser allowlist. Legitimate bulk exports go through the dedicated `shopify_bulk_query` tool, which validates the inner query with `_assert_read_only` BEFORE wrapping it in the bulk mutation. Single source of truth — no reliance on Shopify-side validation.

## Rate limiting

Per-store cost-based leaky bucket (Shopify's model). Each response includes `extensions.cost.throttleStatus`. On `THROTTLED` errors, the MCP sleeps `ceil((requestedQueryCost - currentlyAvailable) / restoreRate)` seconds (minimum 1s) and retries up to 3 times. Over limit → surfaced to caller.

Buckets are independent per store — throttle on store A doesn't affect store B.

## Security hardening

- Shop domain validated against `^[a-z0-9][a-z0-9-]*\.myshopify\.com$` (no arbitrary hostnames — defeats SSRF).
- Alias pattern `^[a-z0-9][a-z0-9_-]{0,63}$`.
- `allow_redirects=False` on both token exchange and GraphQL calls.
- `X-Shopify-Access-Token`, `client_secret`, and all token prefixes (`shpua_`, `shpat_`, `shpss_`, `shpca_`) redacted from logs and error messages.
- Input validation: GraphQL parsed, operation IDs regex-matched, type names validated, query-size ceiling 100KB.
- Per-store token cache in memory only; flushed on 401/ACCESS_DENIED with one retry.

## Service name

`shopify`

## API version

Default pin: `2026-04`. Callers can override per-tool via `api_version="YYYY-MM"`. Bump the module constant `SHOPIFY_API_VERSION` quarterly after smoke-testing new versions.

## Scopes needed (read-only)

Minimum viable: `read_products read_orders read_customers`.

Recommended baseline:
`read_products read_orders read_customers read_inventory read_locations read_fulfillments read_discounts read_content read_themes read_files read_markets read_metaobjects read_metaobject_definitions read_reports read_translations read_locales read_shipping`.

Add `read_all_orders` for >60-day order history. Enable **Protected customer data access** in Dev Dashboard → Configuration if the agent needs customer PII.

## Privacy Policy

This connector runs **locally**, on your own machine, under your own Shopify credentials. It is a thin read-only bridge between your MCP client and Shopify's Admin API.

- **Data collection:** The connector collects **no** personal data and contains **no** telemetry, analytics, or external reporting. It does not phone home.
- **Data usage:** Shopify store data you query is returned to your local MCP client to fulfill your request, and is not used for any other purpose.
- **Data storage:** The connector stores **nothing** persistently. Access tokens are held in memory only for the life of the process and are never written to disk. The only network destination is Shopify's own API (`*.myshopify.com`), enforced by a domain allowlist.
- **Third-party sharing:** **None.** Data flows only between your machine and Shopify. No third party (including the connector's author) ever receives your data or credentials.
- **Retention:** No data is retained by the connector after the process exits.
- **Secret handling:** Access tokens, client secrets, and all Shopify token prefixes are redacted from logs and error messages.
- **Contact:** hello@scalably.io

The canonical hosted version of this policy: https://scalably.io/connector-privacy.html

## References

- [Shopify Admin GraphQL API](https://shopify.dev/docs/api/admin-graphql)
- [Client credentials grant](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/client-credentials-grant)
- [Rate limits](https://shopify.dev/docs/api/usage/rate-limits)
- [Bulk operations](https://shopify.dev/docs/api/usage/bulk-operations/queries)
- [ShopifyQL](https://shopify.dev/docs/api/shopifyql)
