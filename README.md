# Claude agent runner

A job queue that lets a Claude session drive local GPU services it cannot
reach directly.

## Why it exists

A Claude session can write into a shared folder, but cannot reach this
machine's network or type into a terminal. So instead of trying to bridge
that gap directly, this script watches a folder that Claude *can* write to:

```
  Claude writes  jobs/*.json
                      │
                      ▼
        agent_runner.py  ──►  ComfyUI (:8188)
                         ──►  edge-tts
                      │
                      ▼
  Claude reads   results/*.json
```

Claude drops a job file in, the runner executes it against the local
services, and the result is written back into the same folder for Claude to
collect.

## Running it

Double-click `RUN-AGENT.bat` once and leave the window open. Closing it
stops the runner. It touches nothing outside its own folder.

## Layout

```
agent_runner.py   the loop
RUN-AGENT.bat     start it
jobs/             inbox   — job files land here
results/          outbox  — completed work  (git-ignored)
deliver/          finished artefacts        (git-ignored)
done/             processed jobs            (git-ignored)
refs/             reference audio for voice work
```

`results/`, `deliver/` and `done/` are generated output and are excluded
from version control — together they run to several gigabytes.

## Requirements

- ComfyUI listening on `127.0.0.1:8188`
- `edge-tts` available on the path

See [`gf-forge`](https://github.com/dancockrell/gf-forge) for the ComfyUI side, which covers the same
ground with a richer job format and is the better starting point for new
work.
