"""业务错误类型，由统一异常处理器映射为 HTTP 响应。"""

from typing import Any


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
