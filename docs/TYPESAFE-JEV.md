# TypeSafe Jev provider

`JevDecisionSelector` adapts the TypeSafe SystemOne endpoint at
`https://api.typesafe.ai/v1/systemone` to the shared decision contracts. The
request uses the pinned `jev-1.13.0` model and sends one `state` plus one or
more named questions. TypeSafe's official OpenAPI document is available at
[`https://api.typesafe.ai/openapi.json`](https://api.typesafe.ai/openapi.json).
It defines Choice questions as a criteria map, Noul questions with `true` and
`false` criteria, and Score questions as an ordered criteria list. Responses
include the selected answers, the model name, and input/output token usage.

The adapter records the exact request object and raw response in `Selection`.
Credentials are read at construction time from `TYPESAFE_API_KEY` or supplied
by the runtime. They are never included in model input or evidence. The live
HTTP transport uses `httpx` with redirects and retries disabled. Tests inject a
transport and make no paid requests.

Hard charge reservation is fail closed. `maximum_charge_micros()` returns a
value only when the caller supplies a verified token bound and records its
source. A byte heuristic is never presented as a provider-enforced limit.
Transport is attempted once. A runtime may retry only after a
`ProviderFailure` proves that submission did not occur and the request was
uncharged.
