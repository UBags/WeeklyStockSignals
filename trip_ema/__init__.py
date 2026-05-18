"""trip_ema — Triple-EMA + ATR weekly indicator signals and notifications."""

# Version: prefer package metadata (when installed via pip / pyproject.toml).
# Fall back to a hardcoded string for unpackaged dev usage (running directly
# from a clone). The fallback should match pyproject.toml's `version` field;
# they're kept in sync by hand until the package is published.
try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError
    try:
        __version__ = _pkg_version("trip-ema")
    except PackageNotFoundError:
        __version__ = "0.11.0"
except ImportError:
    __version__ = "0.11.0"
