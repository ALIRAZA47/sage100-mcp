# PyInstaller one-folder build of the 32-bit driver helper (docs/decisions.md D67). On Windows, with a
# 32-bit CPython 3.12 that has pyodbc and PyInstaller, from the repository root:
#
#     python -m PyInstaller packaging/odbc32.spec --noconfirm --distpath build/odbc32 --workpath build/pyinstaller32
#
# The result, build/odbc32/odbc32/, is copied into the server's folder as odbc32\ before the MSI is built,
# which is where the server looks for odbc32\sage100-mcp-odbc32.exe. It holds helper.py, the standard
# library and pyodbc: none of the server's other code or dependencies.
# ruff: noqa
import os

analysis = Analysis(
    [os.path.join(SPECPATH, "..", "src", "sage100_mcp", "bridge", "helper.py")],
    hiddenimports=["pyodbc"],
    excludes=["tkinter", "unittest", "pydoc", "sqlite3", "asyncio", "multiprocessing"],
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="sage100-mcp-odbc32", console=True)
COLLECT(exe, analysis.binaries, analysis.datas, name="odbc32")
