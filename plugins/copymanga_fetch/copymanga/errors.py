"""异常类型。"""

from __future__ import annotations


class CopymangaError(Exception):
    """本工具所有异常的基类。"""


class ConfigError(CopymangaError, ValueError):
    """配置非法，例如 mode 取值不对、host 为空。"""


class AuthError(CopymangaError):
    """没有凭据，或凭据被拒绝（HTTP 401/403）。"""


class ApiError(CopymangaError):
    """接口返回异常：网络错误、非 JSON 响应、业务错误码。"""
