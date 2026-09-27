import threading
import time

_lock = threading.Lock()
_store = {}

TTL = {
    "battery": 60,
    "wifi": 60,
    "disk": 300,
    "pkg": 3600,
    "context": 120,
    "graph": 600,
    "project": 600,
    "system": 30,
}


def get(topic: str, key: str = ""):
    with _lock:
        item = _store.get((topic, key))
        if not item:
            return None
        value, stamp = item
        if time.time() - stamp > TTL.get(topic, 60):
            _store.pop((topic, key), None)
            return None
        return value


def put(topic: str, value, key: str = ""):
    with _lock:
        _store[(topic, key)] = (value, time.time())
    return value


def invalidate(*topics: str) -> None:
    with _lock:
        for k in list(_store):
            if k[0] in topics:
                _store.pop(k, None)


def clear() -> None:
    with _lock:
        _store.clear()
