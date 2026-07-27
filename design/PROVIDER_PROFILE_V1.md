# Provider Profile V1

`pipeline/configs/literary_provider_profile_v1.json` is the non-secret runtime
profile for the literary pipeline. It centralizes role-to-model and
role-to-physical-quota-bucket selection for B0, the Local Conflict Auditor, and
the Stable-Claim Auditor.

The profile stores only relative credential file names and non-empty row
numbers. It never stores API-key text or an absolute credential-root path. The
runner receives the credential root through `--credential-root` or
`THESIS_CREDENTIAL_ROOT`; a future Console settings layer can provide the same
value without changing the pipeline contract.

Each credential may also pin a non-secret compatible API base URL. The active
CKEY credential uses `https://api.xah.io`; the retained ShopAPI credential uses
`https://api.shopaikey.com`; native Google credentials leave this field null.
This prevents a proxy `sk-...` credential from ever being sent to Google's
official endpoint.

Each physical bucket also pins `request_timeout_ms`. CKEY and native providers
retain a bounded 120-second timeout; the retained ShopAPI proxy uses 480 seconds
because observed successful requests can take nearly six minutes. A timeout remains fail-closed
and is never an instruction to retry automatically: the upstream provider may
finish and bill a request after the client disconnects.

Every physical key/account has one distinct `quota_bucket_id`. Replacing a key
with another account requires a new bucket/revision instead of reusing an old
ledger identity. Disabled credentials remain visible for audit but cannot be
selected by a role.

The role's first bucket is the default. A run may select another enabled bucket
explicitly, but model id and role provider remain pinned by the sealed profile.
Marketplace routes use the source-qualified model id returned by the provider,
while the runner separately verifies that its final path segment is the sealed
semantic model (`gemini-3.5-flash`). The quota bucket continues to identify the
physical CKEY account, not an individual marketplace seller.
