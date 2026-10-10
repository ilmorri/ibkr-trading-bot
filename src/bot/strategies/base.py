from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    def __init__(self, symbol: str, params: dict | None = None):
        self.symbol = symbol
        self.params = params or {}

    @abstractmethod
    def generate_signal(self, market_data):
        raise NotImplementedError
