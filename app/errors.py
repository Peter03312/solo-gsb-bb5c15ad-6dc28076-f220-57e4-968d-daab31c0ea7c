class AppError(Exception):
    """业务错误基类，由 main 中的异常处理器统一转成 JSON。"""

    def __init__(self, message: str, status_code: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class BadRequest(AppError):
    def __init__(self, message: str):
        super().__init__(message, 400, "bad_request")


class PermissionDenied(AppError):
    def __init__(self, message: str):
        super().__init__(message, 403, "forbidden")


class NotFound(AppError):
    def __init__(self, message: str = "资源不存在"):
        super().__init__(message, 404, "not_found")


class Conflict(AppError):
    """状态冲突 / 版本过期 / 被规则拒绝，统一 409 并携带明确中文提示。"""

    def __init__(self, message: str, code: str = "conflict"):
        super().__init__(message, 409, code)
