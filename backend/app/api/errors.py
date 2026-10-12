"""HTTP 层的失败映射: 把 AppError 翻成合适的状态码

路由层原来的写法是 `except Exception -> 500`, 于是三类完全不同的东西被压成同一个
状态码: 上游抖动(该让前端重试)、能力未开(该让前端收起这个入口)、以及**我们自己的
代码 bug**(该有人去看日志)。前端拿到 500 只能一律显示"服务异常"。

这里只映射**分类过的**失败; 其余异常一概不捕, 让它冒给 FastAPI(裸 500 + 完整栈到
日志)。看着更"丑", 但这正是本意: 程序缺陷要有人看见, 而不是被压成一句好听的文案。
"""

from fastapi import HTTPException

from ..graph.errors import (
    AppError, CapabilityDisabled, ConfigError, PermanentError, TransientError,
)

# 失败类型 -> HTTP 状态码
#   503 上游暂时不可用(可重试) / 502 上游给了坏响应(重试无意义)
#   501 该能力本服务未启用 / 500 服务自身配置错
_STATUS_BY_TYPE = (
    (CapabilityDisabled, 501),
    (ConfigError, 500),
    (TransientError, 503),
    (PermanentError, 502),
    (AppError, 502),
)


def to_http_error(exc: AppError, what: str) -> HTTPException:
    """AppError -> HTTPException(带 Retry-After 提示可重试的失败)

    Args:
        exc:  已分类的失败
        what: 面向用户的动作名(如 "POI搜索"), 拼进 detail
    """
    status = 502
    for err_type, code in _STATUS_BY_TYPE:
        if isinstance(exc, err_type):
            status = code
            break

    headers = {"Retry-After": "5"} if status == 503 else None
    print(f"[api] {what}失败({exc.code}): {str(exc)[:200]}")
    return HTTPException(status_code=status, detail=f"{what}失败: {exc.user_message}",
                         headers=headers)