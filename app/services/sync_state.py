"""Thread-safe sync-state service for the BigQuery cache refresh.

The previous implementation used two module-level dicts (``sync_state`` and
``actual_totals``) that were mutated from the request thread *and* the
background refresh thread. This module exposes a single
:class:`SyncStateService` instance (``sync_state_service``) that owns the
state behind a lock, plus thin ``sync_state`` / ``actual_totals`` mappings
that proxy attribute access for the handful of legacy callers that still
read them directly.
"""
from __future__ import annotations

import threading
from collections.abc import MutableMapping
from typing import Any


class SyncStateService:
    """Coordinator for the background BigQuery cache refresh."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._is_running = False
        self._should_stop = False
        self._totals: dict[str, int] = {
            "summary_total": 0,
            "items_total": 0,
            "comparison_total": 0,
        }

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    @property
    def should_stop(self) -> bool:
        with self._lock:
            return self._should_stop

    @property
    def totals(self) -> dict[str, int]:
        with self._lock:
            return dict(self._totals)

    def start(self) -> bool:
        """Mark a refresh as started. Returns ``False`` if one is already running."""
        with self._lock:
            if self._is_running:
                return False
            self._is_running = True
            self._should_stop = False
            return True

    def request_stop(self) -> None:
        with self._lock:
            self._should_stop = True

    def finish(self) -> None:
        with self._lock:
            self._is_running = False
            self._should_stop = False

    def set_totals(self, *, summary: int, items: int, comparison: int) -> None:
        with self._lock:
            self._totals = {
                "summary_total": int(summary or 0),
                "items_total": int(items or 0),
                "comparison_total": int(comparison or 0),
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "is_running": self._is_running,
                "should_stop": self._should_stop,
                **self._totals,
            }


sync_state_service = SyncStateService()


class _SyncStateProxy(MutableMapping[str, bool]):
    """Compatibility shim so legacy ``sync_state['is_running']`` keeps working."""

    _attr_map = {"is_running": "is_running", "should_stop": "should_stop"}

    def __getitem__(self, key: str) -> bool:
        if key not in self._attr_map:
            raise KeyError(key)
        return getattr(sync_state_service, self._attr_map[key])

    def __setitem__(self, key: str, value: bool) -> None:
        if key == "is_running":
            if value:
                sync_state_service.start()
            else:
                sync_state_service.finish()
        elif key == "should_stop":
            if value:
                sync_state_service.request_stop()
            else:
                with sync_state_service._lock:  # noqa: SLF001 - intentional for shim
                    sync_state_service._should_stop = False  # noqa: SLF001
        else:
            raise KeyError(key)

    def __delitem__(self, key: str) -> None:  # pragma: no cover - not used
        raise NotImplementedError

    def __iter__(self):
        return iter(self._attr_map)

    def __len__(self) -> int:
        return len(self._attr_map)


class _ActualTotalsProxy(MutableMapping[str, int]):
    """Compatibility shim for legacy ``actual_totals['summary_total']`` access."""

    _keys = ("summary_total", "items_total", "comparison_total")

    def __getitem__(self, key: str) -> int:
        if key not in self._keys:
            raise KeyError(key)
        return sync_state_service.totals[key]

    def __setitem__(self, key: str, value: int) -> None:
        if key not in self._keys:
            raise KeyError(key)
        totals = sync_state_service.totals
        totals[key] = int(value or 0)
        sync_state_service.set_totals(
            summary=totals["summary_total"],
            items=totals["items_total"],
            comparison=totals["comparison_total"],
        )

    def __delitem__(self, key: str) -> None:  # pragma: no cover - not used
        raise NotImplementedError

    def __iter__(self):
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


sync_state: MutableMapping[str, bool] = _SyncStateProxy()
actual_totals: MutableMapping[str, int] = _ActualTotalsProxy()
