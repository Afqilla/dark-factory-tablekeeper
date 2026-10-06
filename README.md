# Dark Factory: tablekeeper

Team: Tirexx (Afqilla Regina)
Track: tablekeeper (restaurant reservations)
Stages completed: 1
Factory: three coding-agent seats in BAND Desktop (Lead, Builder, reviewer03),
runtime Codex, model gpt-6-luna (the id shown by BAND Desktop).

## How to read this repository

| Path | What it is |
|---|---|
| `mandates/` | One generic mandate per seat. Each starts with its harness and model |
| `FACTORY.md` | How the factory is set up, why, what it cost, how it handles bad work |
| `room.json` | Full download of the BAND Desktop room that produced the code, unmodified |
| `stage-1/` | The service built by the band (Dockerfile, RUN.md, source, tests) |

## Run the service

See `stage-1/RUN.md`. Short version:

    docker build -t tablekeeper-stage1 stage-1
    docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-stage1
    curl http://localhost:8080/health

The health endpoint answers `{"status":"ok"}`.

## Notes

- The service source, its tests and the first Dockerfile and RUN.md were written and
  committed by the Builder seat. The Lead seat committed the final packaging commit
  (`c1deafd`, Dockerfile and RUN.md). Author lines are in the room's final report.
- The human committed the mandates and `.gitignore` before the run, and the documents
  at the repository root (`README.md`, `FACTORY.md`, `room.json`) after it. The human
  sent exactly one room message, the dispatch, and nothing after it.
- This repository comes from a rehearsal-style room (22:04 to 23:19 WIB, 5 Oct 2026)
  that is being submitted as the run. Its seats use Codex, not OpenCode. See
  `FACTORY.md`, section 7.
- Result of the shipped checks (isolated mode, run after the room ended, on the same
  revision as the room's final report):

      stage 1: pass
      stage 2: fail
      claimed stage: 1 on the shipped checks

  The harness itself notes that this run covers only part of the full tests.
