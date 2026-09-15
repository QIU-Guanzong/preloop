# Session embedding worker

Keyword indexing of session content is separate from vectors. Turning
embedding off does not stop the corpus from taking writes.

## Kill switch vs keyword indexing

`SESSION_EMBEDDING_ENABLED` (default true) is the deployment kill switch for
vectors only. `SESSION_SEARCH_INDEX_ENABLED` gates keyword chunks. Accounts
must still opt in through `session_embedding_setting`; the shipped default
embeds nothing.

## Daily cap

`SESSION_EMBEDDING_DAILY_CAP_USD` (default 2.0) is the per-account money
ceiling for purpose-tagged `session_embedding` usage. An account may set its
own cap on the setting row. Reaching the cap is a degraded state, not an
error: chunks stay pending for the next day's run. An OpenAI-compatible model
missing from the price catalogue is refused before the provider call
(`unpriced_model`) so an unmetered name cannot bypass the cap.

## Shared API key

`SESSION_EMBEDDING_API_KEY` is a deployment credential. It is attached as a
Bearer token only when the account's `base_url` is listed in
`SESSION_EMBEDDING_API_KEY_BASE_URLS` (comma-separated exact https URLs,
trailing slash ignored). An empty allow-list, the shipped default, means the
key is never sent. Store the key in the same secret handling as other
provider credentials. `enable()` refuses a non-https URL and a private,
loopback, or link-local IP host so the worker cannot be pointed at metadata
or internal endpoints.

## Other knobs

| Variable | Default | Role |
| --- | --- | --- |
| `SESSION_EMBEDDING_BATCH_SIZE` | 32 | Chunks per provider call and usage row |
| `SESSION_EMBEDDING_QUEUE_MAX_PENDING` | 128 | Accounts waiting before a submit is dropped |
| `SESSION_EMBEDDING_QUEUE_WORKER_ENABLED` | true | Background thread; `TESTING=true` disables it |
| `SESSION_EMBEDDING_MAX_ATTEMPTS` | 3 | Retries before a chunk is retired as failed |
| `SESSION_EMBEDDING_TIMEOUT_SECONDS` | 30 | One embeddings HTTP call |

Helm documents the same names next to the gateway search-index queue comments
in `helm/preloop/values.yaml`.
