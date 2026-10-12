
## Build ledger (mandatory)
Every session logs what it built. The ledger is the `ai-command-center` folder (CLI `tracker`, app `tracker open`); the protocol is the `log-build` skill. Use it whenever you create or change a durable file, or when the Stop gate fires.
Before building, read the CLAUDE TRACKER context the SessionStart hook injects and check `tracker recent --cwd .` or `tracker search "..."`. If the thing already exists, extend it and log `--kind enhancement` instead of rebuilding it.
Log each distinct thing as you finish it: `tracker log --title "<name>" --summary "<what it is and why, 60+ chars>" --usage "<how to run it>" --file <path>`. `tracker waive --reason "..."` is only for sessions that made nothing durable.
