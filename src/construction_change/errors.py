"""施工变更影响审批服务向 API 和 CLI 暴露的稳定错误。"""


class ChangeError(RuntimeError):
    code = "change_error"
    status = 400


class NotFound(ChangeError):
    code = "not_found"
    status = 404


class Conflict(ChangeError):
    code = "conflict"
    status = 409


class Forbidden(ChangeError):
    code = "forbidden"
    status = 403


class InvalidState(ChangeError):
    code = "invalid_state"
    status = 409


class ValidationFailed(ChangeError):
    code = "validation_failed"
    status = 422
