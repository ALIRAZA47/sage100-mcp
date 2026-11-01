# PyInstaller one-folder build of sage100-mcp (SPEC.md §16.3). From the repository root:
#
#     uv run --with pyinstaller pyinstaller packaging/sage100-mcp.spec --noconfirm --distpath build/dist --workpath build/pyinstaller
#
# The result, build/dist/sage100-mcp/, is what the MSI installs (packaging/wix/sage100-mcp.wxs), with the
# 32-bit driver helper (packaging/odbc32.spec) copied in as odbc32\. Code that loads modules or files by
# name is listed here: the YAML the package reads with importlib.resources, the metadata importlib.metadata
# reads (versions, keyring's backends), sqlglot's dialects and uvicorn's protocols. helper.py goes in as a
# file too, for a backend whose helper setting names a 32-bit python.exe. The release workflow builds and
# smoke-tests it on Windows.
# ruff: noqa
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

hiddenimports = (
    collect_submodules("sage100_mcp")
    + collect_submodules("sqlglot.dialects")
    + collect_submodules("uvicorn")
    + collect_submodules("keyring.backends")
    + ["win32timezone", "servicemanager"]  # pywin32's service host needs both; absent off Windows
)
datas = collect_data_files("sage100_mcp")
datas.append((os.path.join(SPECPATH, "..", "src", "sage100_mcp", "bridge", "helper.py"), "sage100_mcp/bridge"))
for distribution in ("sage100-mcp", "mcp", "keyring", "pydantic", "uvicorn", "starlette", "pyjwt"):
    datas += copy_metadata(distribution)

analysis = Analysis(
    ["entry.py"],
    pathex=["../src"],
    hiddenimports=hiddenimports,
    datas=datas,
    excludes=["tkinter", "pytest", "hypothesis", "mypy", "ruff"],
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="sage100-mcp", console=True)
COLLECT(exe, analysis.binaries, analysis.datas, name="sage100-mcp")
