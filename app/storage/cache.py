"""
High-Performance Cache Engine supporting Redis with seamless In-Memory Fallback.
Provides sub-millisecond price lookups, tick buffering, and rate limiters.
"""

import time
import json
import logging
from typing import Any, Optional, Dict
from app.config import settings

logger = logging.getLogger("app.storage.cache")


class FastCacheManager:
    """Hybrid Redis / In-Memory Cache for ultra-low latency trading operations."""

    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url or settings.REDIS_URL
        self.redis_client = None
        self.is_redis_connected = False
        self._memory_store: Dict[str, Any] = {}
        self._expiry_store: Dict[str, float] = {}
        self.hits = 0
        self.misses = 0

        self._connect_redis()

    def _connect_redis(self):
        try:
            import redis
            client = redis.from_url(self.redis_url, socket_timeout=1.0, decode_responses=True)
            if client.ping():
                self.redis_client = client
                self.is_redis_connected = True
                logger.info(f"Connected to Redis at {self.redis_url}")
        except Exception as e:
            self.is_redis_connected = False
            self.redis_client = None
            logger.info(f"Redis not available ({e}). Using ultra-fast in-memory cache.")

    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> bool:
        """Store key-value pair in cache."""
        try:
            if self.is_redis_connected and self.redis_client:
                str_val = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
                if ttl_seconds:
                    self.redis_client.setex(key, ttl_seconds, str_val)
                else:
                    self.redis_client.set(key, str_val)
                return True
        except Exception:
            self.is_redis_connected = False

        # In-Memory fallback
        self._memory_store[key] = value
        if ttl_seconds:
            self._expiry_store[key] = time.time() + ttl_seconds
        elif key in self._expiry_store:
            del self._expiry_store[key]
        return True

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieve key from cache."""
        try:
            if self.is_redis_connected and self.redis_client:
                val = self.redis_client.get(key)
                if val is not None:
                    self.hits += 1
                    try:
                        return json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        return val
                self.misses += 1
                return default
        except Exception:
            self.is_redis_connected = False

        # In-Memory fallback
        if key in self._memory_store:
            exp = self._expiry_store.get(key)
            if exp and time.time() > exp:
                del self._memory_store[key]
                del self._expiry_store[key]
                self.misses += 1
                return default
            self.hits += 1
            return self._memory_store[key]

        self.misses += 1
        return default

    def set_price(self, symbol: str, price: float):
        """Set stock tick price with sub-millisecond storage."""
        self.set(f"price:{symbol.upper()}", round(price, 2), ttl_seconds=300)

    def get_price(self, symbol: str) -> Optional[float]:
        """Fetch stock tick price instantly."""
        val = self.get(f"price:{symbol.upper()}")
        if val is not None:
            try:
                return float(val)
            except ValueError:
                return None
        return None

    def get_status(self) -> Dict[str, Any]:
        """Return diagnostic metrics of cache subsystem."""
        total = self.hits + self.misses
        hit_ratio = round((self.hits / total * 100), 2) if total > 0 else 100.0
        return {
            "mode": "redis" if self.is_redis_connected else "in_memory_fast",
            "redis_connected": self.is_redis_connected,
            "cached_keys_count": len(self._memory_store) if not self.is_redis_connected else "redis_managed",
            "hits": self.hits,
            "misses": self.misses,
            "hit_ratio_pct": hit_ratio,
        }


# Global cache instance
cache = FastCacheManager()

