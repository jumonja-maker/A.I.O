"""Cooperative shutdown of recovery reads; never a corruption verdict."""


class RecoveryStopped(Exception):
    """Ownership must be released without salvaging or deleting on shutdown."""


def check_recovery_stop(cancel):
    if cancel is not None and cancel.is_set():
        raise RecoveryStopped()
