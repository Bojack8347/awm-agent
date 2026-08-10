# Wolfram|Alpha fallback

This optional tool is the Financial Planning agent's last-resort route for a
de-identified pure-math problem that the local `awm.financial_math.v2` plan
cannot represent. The agent chooses the route; the server only enforces the
query, privacy, transport, scalar-result, and unit contracts.

It is disabled by default. Enable it with:

```text
AWM_WOLFRAM_ALPHA_MODE=live
WOLFRAM_ALPHA_APP_ID=<secret AppID>
AWM_WOLFRAM_ALPHA_TIMEOUT_SECONDS=8
```

Keep the AppID in the deployment secret manager. Never put Client File facts,
analysis identifiers, account data, financial amounts, tax or benefit rules,
or other personalized context in a Wolfram|Alpha query. The adapter makes one
fixed-host request, accepts one unambiguous finite scalar with the declared
unit, and marks the result as reporting-only.
