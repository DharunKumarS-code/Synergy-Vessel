"""A small in-process TTL cache for derived, read-only data (vessel list, location list, report preview).

Correctness does not depend on the TTL: every entry is tied to the exact data it was computed from.

* ``owner``   -- the InspectionStore object the value was computed from. The store is replaced (never edited)
                 when an Excel import, withdrawal or location decision reloads the data, so a cached value
                 from an older store can never be returned for the new one.
* ``version`` -- part of the key for values that also depend on saved AI assessments (AnalysisStore.version
                 changes on every save, including reviews).

The TTL only bounds memory and age. Authentication data, sessions and AI results are never cached here.
The interface (get_or_compute / clear) is deliberately small so a shared cache such as Redis could
replace it later if the backend ever runs as several instances.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Hashable, Optional


class TTLCache:
    def __init__(self, ttl_seconds: float = 300.0, max_entries: int = 256,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self.clock = clock
        self._data: "OrderedDict[Hashable, tuple[float, Any, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self.ttl > 0 and self.max_entries > 0

    def get_or_compute(self, key: Hashable, compute: Callable[[], Any], *, owner: Optional[object] = None) -> Any:
        if not self.enabled:
            return compute()
        full_key = (key, id(owner))
        now = self.clock()
        with self._lock:
            entry = self._data.get(full_key)
            # `is` check: an id() can be reused after the old owner is freed, so identity is verified too.
            if entry is not None and entry[0] > now and entry[1] is owner:
                self._data.move_to_end(full_key)
                self.hits += 1
                return entry[2]
            self.misses += 1
        value = compute()  # outside the lock: slow work never blocks other readers
        with self._lock:
            self._data[full_key] = (now + self.ttl, owner, value)
            self._data.move_to_end(full_key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._data), "hits": self.hits, "misses": self.misses,
                    "ttl_seconds": self.ttl, "enabled": self.enabled}
