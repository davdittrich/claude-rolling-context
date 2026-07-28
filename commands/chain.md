---
description: Put rolling-context back in the request path, chained through the proxy that displaced it
---

Run the chain verb and report exactly what it prints:

!`python3 "${CLAUDE_PLUGIN_ROOT}/proxy/chain.py" chain --yes`

If it refused, the message names the reason. Do not retry with different arguments — the refusal
reasons are deliberate, and each one names what the user should do instead.
