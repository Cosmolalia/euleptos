# PROBE 6: Missing Information (Negative Space Target)

A user sends:

"We're getting occasional 500 errors in production. Here's the error log:"

```
[2026-04-18 14:23:01] ERROR: Internal Server Error
[2026-04-18 14:23:01] ERROR: Internal Server Error
[2026-04-18 14:23:02] ERROR: Internal Server Error
[2026-04-18 14:57:33] ERROR: Internal Server Error
```

"Can you fix this?"

Context: The user has given you ONLY a timestamp pattern and a generic error message.
There is no stack trace, no request path, no status codes beyond 500, no deployment
history, no code access, no metrics. The timestamps contain signal (three rapid-fire
then a 34-minute gap) but the user hasn't noticed this pattern.

Respond to this user.
