"""Start the clean A.I.O desktop without any legacy converter engine."""
if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == '--aio-guard-preflight':
        from app_backend.converter_guard.preflight_native import main
        main(sys.argv[2])
    from app_backend.desktop import launch_desktop
    raise SystemExit(launch_desktop())
