# PyInstaller spec for the trip-ema operator console.
#
# Builds a single-file executable that bundles PySide6, pyqtgraph,
# and the trip_ema GUI subpackage. The resulting binary launches
# `trip-ema-console` without requiring Python or any pip-installed
# packages on the operator's machine.
#
# Build:
#     pip install pyinstaller
#     pyinstaller gui_installer.spec
#
# Output:
#     dist/trip-ema-console            (Linux + macOS)
#     dist/trip-ema-console.exe        (Windows)
#
# CI builds Linux AppImage via .github/workflows/release.yml; other
# platforms can be built locally as needed.
#
# Excluded:
#   - The ETL CLI (trip-ema-etl) — operator console doesn't need it
#   - The notifier worker (trip-ema-notifier) — separate process
#   - The main service (trip-ema) — runs server-side, not bundled
#   - psycopg2 / SQLAlchemy — operator console talks to the service
#     over HTTP, no direct DB access
#
# Rationale: the operator console is a thin client. Bundling the
# server-side dependencies would triple the binary size and create
# a misleading impression that the operator install is sufficient
# to run the whole system.

# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

block_cipher = None

# Resolve repo root relative to the spec file's location.
REPO_ROOT = Path(SPECPATH)

a = Analysis(
    [str(REPO_ROOT / "trip_ema" / "gui" / "console.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=[
        # Bundle any GUI resource files (icons, qss themes, etc.)
        # currently under trip_ema/gui/resources/ — none yet, but
        # the entry exists so the spec doesn't need editing when
        # the GUI gains resources.
    ],
    hiddenimports=[
        # PySide6 + pyqtgraph don't always get fully detected by
        # PyInstaller's static analysis. Explicit listing avoids
        # "module not found" surprises at runtime.
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtSvg",
        "pyqtgraph",
        "trip_ema.gui",
        "trip_ema.gui.http_client",
        "trip_ema.gui.main_window",
        "trip_ema.gui.views",
        "trip_ema.gui.workers",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Explicitly exclude server-side dependencies.
        "psycopg2",
        "psycopg2-binary",
        "sqlalchemy",
        "alembic",
        "redis",
        "yfinance",
        "pandas",
        "fastapi",
        "uvicorn",
        # Testing
        "pytest",
        "pytest-qt",
        "pytest-postgresql",
        "fakeredis",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="trip-ema-console",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX compression can confuse antivirus; skip.
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # GUI application — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
