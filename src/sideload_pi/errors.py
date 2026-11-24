"""Errors the owner can act on. Messages must not contain secrets."""


class SideloadError(Exception):
    """Base error. The message is safe to show on the e-ink panel and in logs."""


class ConfigError(SideloadError):
    pass


class LimitError(SideloadError):
    """Apple's free-account limits, or our pre-check before calling Apple."""


class AnisetteError(SideloadError):
    pass


class AppleAuthError(SideloadError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class AppleAPIError(SideloadError):
    def __init__(self, message: str, result_code: int | None = None):
        super().__init__(message)
        self.result_code = result_code


class DeviceError(SideloadError):
    pass


class SignError(SideloadError):
    pass


class InstallError(SideloadError):
    pass


class TwoFactorTimeout(SideloadError):
    pass
