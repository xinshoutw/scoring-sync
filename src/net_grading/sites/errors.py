class SiteError(Exception):
    """所有站台錯誤的基底."""


class SiteLoginError(SiteError):
    """登入失敗（憑證錯誤、學號不存在等）."""


class SiteUnsupportedRole(SiteLoginError):
    """Site1 認到 teacher/admin，但本站只服務 student."""


class SiteTokenExpired(SiteError):
    """Session / idToken 過期，需要重新登入或刷新."""


class SiteRateLimited(SiteError):
    """被 429."""


class SiteNotSupported(SiteError):
    """站台不支援此操作（例：Site3 無讀取端點）."""


class SiteTransportError(SiteError):
    """網路層錯誤：timeout、DNS、5xx."""
