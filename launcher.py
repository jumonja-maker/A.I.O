"""Start the clean A.I.O desktop without any legacy converter engine."""
if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == '--aio-guard-preflight':
        from app_backend.converter_guard.preflight_native import main
        main(sys.argv[2])
        raise SystemExit(0)
    if len(sys.argv) == 3 and sys.argv[1] == '--aio-guard-quality':
        from app_backend.converter_guard.quality_worker import main
        main(sys.argv[2])
        raise SystemExit(0)
    # Removed/private unknown worker switches must not fall through to GUI.
    if len(sys.argv)>1 and sys.argv[1].startswith('--aio-guard-'):
        raise SystemExit(2)
    from app_backend.desktop import launch_desktop
    raise SystemExit(launch_desktop())
