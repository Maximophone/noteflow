from typing import Any

SLOW_REPEAT_INTERVAL = 30

class Config:
    def __init__(self):
        self.slow_repeat_interval = 10

    def get(self, var_name: str) -> Any:
        return self.__dict__[var_name]

CONFIG = Config()

