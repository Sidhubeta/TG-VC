try:
    import httpx  # noqa: F401
except ImportError:  # local sandbox fallback
    import httpx2 as httpx  # type: ignore # noqa: F401
