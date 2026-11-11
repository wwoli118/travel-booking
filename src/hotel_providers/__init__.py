"""호텔 공급자(API)별 어댑터. 공통 인터페이스는 base.py."""

from .base import HotelProvider, HotelProviderError, ProviderNotAvailable, get_provider

__all__ = ["HotelProvider", "HotelProviderError", "ProviderNotAvailable", "get_provider"]
