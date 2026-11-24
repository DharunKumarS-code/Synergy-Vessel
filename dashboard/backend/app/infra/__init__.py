"""Infrastructure concerns kept apart from business logic (2026-11-23).

* ``rate_limit``      -- per-client request limits (HTTP 429 + Retry-After)
* ``cache``           -- small in-process TTL cache for derived read-only data
* ``circuit_breaker`` -- stops calling an external AI provider that keeps failing

All three hold their state in this process only. That matches the current single-process deployment;
see dashboard/INFRASTRUCTURE.md for what would change with several backend instances.
"""
