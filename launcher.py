"""Start the clean A.I.O desktop without any legacy converter engine."""
from app_backend.desktop import launch_desktop

if __name__ == '__main__':
    raise SystemExit(launch_desktop())
