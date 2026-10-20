"""Stable Guard failures. Public messages never embed a private source path."""


class GuardError(Exception):
    def __init__(self, code, message, *, stage='guard', status=400, diagnostic=''):
        super().__init__(message)
        self.code, self.message, self.stage, self.status = code, message, stage, status
        self.diagnostic = diagnostic[-8192:]

    def as_dict(self):
        return {'code': self.code, 'error': self.message, 'stage': self.stage,
                'libreOfficeStarted': False}
