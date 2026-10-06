# FACTORY

All times are WIB (UTC+7) on 5 Oct 2026. The numbers come from `room.json`, the Git
history and the harness output. Anything not measured is marked "not measured".

## 1. Seats and ownership

| Seat | Role in this room | Commits made |
|---|---|---|
| Lead | received the dispatch, handed the specification to the Builder and the Reviewer, ran the harness attempts, wrote the final report | `c1deafd` (Dockerfile and RUN.md packaging) |
| Builder | wrote the service and its tests, fixed the rejected item | `6f9d02e`, `9822f78` |
| reviewer03 | independent verification against the specification, rejected once and then accepted | none |

The exact rules of each seat are in `mandates/`; the table above only records what the
room shows.

The mandates are in `mandates/`. They describe how a seat works, not what is being
built. The task (the specification) is pasted into the room by the human in one
dispatch message. The Reviewer seat is named `reviewer03` because the name `Reviewer`
was already taken in the BAND account; its mandate was edited to use that name.

## 2. Setting it up from scratch

1. Create a repository containing only `mandates/` and `.gitignore`, and commit it.
2. In BAND Desktop create three seats with runtime Codex, model `gpt-6-luna`, working
   directory set to the absolute path of the repository, and the Role set to each
   mandate file. The Role is stored in the seat, so after editing a mandate the Role
   must be set again.
3. Codex is signed in with a ChatGPT account on the same computer that runs BAND
   Desktop. Seats run on that computer, not on a remote server.
4. Check each seat with the runtime check in the seat form, then in a new room ask each
   seat for a one-word reply, and ask the Builder to run `git status`.
5. Send one dispatch message to the Lead. Send nothing after it.

Hardware and software used: a Windows laptop with BAND Desktop, Docker Desktop and WSL
(2 CPUs visible in WSL). The harness check command named in the dispatch was a small
PowerShell script that copied the repository to a Linux server and ran the harness
there. That server has 1 CPU, which broke the harness (section 5). Codex version and
approval policy: not recorded.

## 3. Design choices and why

- Three seats, reviewer separate. The Builder reported its work as passing 6 of its own
  tests at 22:22. The separate reviewer then rebuilt the image and found a failure
  the Builder's tests did not cover (section 4). A builder that reviews itself would
  have shipped that bug.
- Verbatim handoffs in numbered parts. The Lead first pointed the Builder at a room
  message. The Builder could not read it (its runtime has no room history lookup) and
  wrote twice that it was blocked. The Lead then pasted the specification in two
  numbered parts (22:10 and 22:11). The dispatch asks for exactly this.
- The reviewer works from a per-section checklist and must cite evidence for each
  item. This is why the rejection at 22:31 named the failing section, the reproduction
  and the line range.
- Packaging by the Lead. The Lead committed the final Dockerfile and RUN.md
  (`c1deafd`, 22:47) and then asked the reviewer to confirm acceptance for that new
  HEAD, which the reviewer did.

## 4. How the factory catches and recovers from bad work

Mechanism: the reviewer lists every requirement section as a checklist with evidence,
runs the tests itself, builds the Docker image from the committed tree, reproduces
failures against the built image, and rejects any item without evidence.

Real example from this room:
- 22:22 the Builder committed `6f9d02e` and reported its tests passing.
- 22:31 reviewer03 rejected `6f9d02e`. Sections 1 to 9 and 11 passed. Section 10 failed:
  importing a restaurant with missing required fields returned 204, and a following
  `GET /restaurants/r` returned 500. The verdict named the validation gap in
  `service.py` and asked for full validation before the state is replaced.
- 22:36 the Builder committed `9822f78` ("Validate imported Tablekeeper state
  atomically") and added a test (6 tests became 7).
- 22:40 reviewer03 accepted `9822f78` after reproducing the case against the built
  image: the import returned 422 `validation_failed` and the original restaurant stayed
  intact.
- 22:53 and again at 23:17 the reviewer confirmed acceptance for the final HEAD
  `c1deafd` after the Lead's packaging commit.

## 5. What failed and what we changed

| Problem seen | Where | Change made |
|---|---|---|
| Codex session revoked when the seat was created (login also used on another machine) | seat runtime check, before the run | logged in again on the laptop |
| Lead referenced a room message, Builder could not read it | 22:05 to 22:11 | Lead pasted the specification in two numbered parts; about 4 minutes lost |
| Harness cannot start: it asks Docker for 2 CPUs, the Linux server has 1 | 22:25 onward (isolated and host runs) | not fixed inside the room; two parallel isolated attempts each waited about 12 minutes before failing, and later attempts failed the same way; no `stage-1.log` was produced |
| Reviewer's detailed verdict was not visible to the Lead | 23:02 | the Lead asked twice for a repost; the reviewer reposted; about 5 minutes lost |
| Harness result missing from the room | end of room | after the room ended the harness was run on the laptop (WSL, 2 CPUs) on the same revision and passed stage 1 |

## 6. Measured cost and time

| Measure | Value |
|---|---|
| Dispatch to final report | 72.9 minutes (22:04:09 to 23:17:05); last agent message at 23:19 |
| Dispatch to first commit | 18.1 minutes (`6f9d02e`) |
| Dispatch to first review verdict | 27.7 minutes (rejection) |
| Review rounds / rejections | 2 / 1 |
| Chat messages (text) per seat: Lead / Builder / reviewer03 | 22 / 9 / 11 |
| Tool calls per seat: Lead / Builder / reviewer03 | 66 / 51 / 42 |
| Human messages in the room | 1 (the dispatch) |
| Model credit used | not measured (Codex through a ChatGPT account, no per-token record) |
| Isolated harness build and run | not measured |
| Shipped-check result | stage 1: pass; stage 2: fail; claimed stage: 1 on the shipped checks |

## 7. Known limits and unproven requirements

- The harness result is not inside the room. Every harness attempt inside the room
  failed because of the 1-CPU server (state=error, no claimed stage). The passing run
  was made on the same revision after the room ended, so `room.json` does not contain it.
  The harness says its run covers only part of the full tests.
- This room was a rehearsal-style run, not a fresh run built for the submission. Its
  seats use Codex (model `gpt-6-luna`) instead of OpenCode, the mandates were edited to
  name Codex, the Reviewer seat has a different name (`reviewer03`), and the check
  command was a custom script.
- Only 7 tests were written for the whole specification. Coverage beyond them rests
  on the reviewer's own checks, of which the room shows the import reproduction and
  the build and health check.
- State is kept in memory and is lost on restart, which the specification allows.
- Only stage 1 was attempted. Stages 2 to 4 were not attempted.
- Cost and credit use were not measured.
