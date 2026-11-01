Probe results from real Sage 100 driver builds, one file per driver version and process bitness
(`<SQL_DRIVER_VER>-x64.json`, `-x86.json`). The Windows runner writes a file the first time it meets
a driver build and fails until the file is reviewed and committed; after that, a change in the
probe's results fails the build (SPEC.md §15.3). No file exists yet: the runner has not run (M2 gate).
