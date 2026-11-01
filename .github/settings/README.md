# Repository settings

These files hold the repository's GitHub settings (docs/decisions.md D75). There are two ways to apply them.

**The script.** It applies all four parts, and needs the GitHub CLI logged in as a repository admin:

```bash
python3 scripts/github_settings.py            # or --parts rulesets,actions,environments,security
python3 scripts/github_settings.py --check    # every line should read ok
```

**By hand.** Only the two ruleset files can be imported into GitHub, from Settings > Rules > Rulesets >
New ruleset > Import a ruleset:

- `ruleset-main.json`
- `ruleset-release-tags.json`

`actions.json` and `environments.json` are not rulesets. GitHub refuses them with "The ruleset you are
importing is not supported on this page". Set them in Settings instead.

**Settings > Actions > General** (`actions.json`):

- **Actions permissions.** Choose "Allow ALIRAZA47, and select non-ALIRAZA47, actions and reusable
  workflows".
  - Tick "Allow actions created by GitHub", and leave "Allow actions by Marketplace verified creators" off.
  - Under "Allow specified actions and reusable workflows", enter
    `astral-sh/setup-uv@*, pypa/gh-action-pypi-publish@*`.
- **Approval for running fork pull request workflows from contributors.** Choose "Require approval for all
  external contributors".
- **Workflow permissions.** Choose "Read repository contents and packages permissions". Leave "Allow GitHub
  Actions to create and approve pull requests" off.

**Settings > Environments** (`environments.json`). Create these four environments:

| Environment | Deployment branches and tags |
| --- | --- |
| `release` | branch `main`, tag `v*` |
| `pypi` | tag `v*` |
| `registry` | tag `v*` |
| `sage-runner` | branch `main` |

For each one, do two things:
- Under "Required reviewers", add ALIRAZA47. Leave "Prevent self-review" off, so you can approve your own
  runs.
- Under "Deployment branches and tags", choose "Selected branches and tags", and add the rules from the
  table.

The secrets belong in these environments, not in the repository:
- `release`: `WINDOWS_SIGNING_CERT_PFX_BASE64` and `WINDOWS_SIGNING_CERT_PASSWORD`.
- `registry`: `MCP_REGISTRY_PRIVATE_KEY`.
- `sage-runner`: `SAGE_TEST_USER` and `SAGE_TEST_PASSWORD`.

**Settings > Code security.** Turn on "Private vulnerability reporting" (SECURITY.md relies on it).
