"""The frozen program's entry point (packaging/sage100-mcp.spec): the same CLI as the console script."""

from sage100_mcp.cli import main

raise SystemExit(main())
