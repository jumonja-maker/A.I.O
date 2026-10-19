"""Machine-readable failures; UI text never contains a private file path."""


class GuardError(Exception):
    def __init__(self, code: str, message: str, *, stage: str = 'guard',
                 status: int = 400):
        super().__init__(message)
        self.code, self.message, self.stage, self.status = code, message, stage, status

    def as_dict(self):
        return {'code': self.code, 'error': self.message, 'stage': self.stage,
                'libreOfficeStarted': False}
