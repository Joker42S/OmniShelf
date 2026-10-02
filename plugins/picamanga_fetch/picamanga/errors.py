"""异常类型。"""

from __future__ import annotations


class PicamangaError(Exception):
    """本工具所有异常的基类。"""


class ConfigError(PicamangaError, ValueError):
    """配置非法，例如 mode 取值不对、host 为空。"""


class AuthError(PicamangaError):
    """没有凭据，或凭据被拒绝（HTTP 401 与哔咔的认证错误码）。"""


class ApiError(PicamangaError):
    """接口返回异常：网络错误、非 JSON 响应、业务错误码。"""
