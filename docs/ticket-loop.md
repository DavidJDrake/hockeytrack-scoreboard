# The ticket loop

`.claude/workflows/sco-tickets.js` works the SCO board with a small fleet of
agents. It is run from Claude Code, in a session whose working directory is
this repository:

```
Workflow({ name: "sco-tickets", args: { max: 3, today: "2026-09-22" } })
```

From another working directory the tool refuses the path; pass the file's
contents as `script` instead (the first run, on 2026-09-22, was done that
way from the HockeyTrack checkout).

`args`:

| key | meaning | default |
|---|---|---|
| `max` | how many tickets to build this run | 3 |
| `only` | build only these keys | all buildable |
| `skip` | never build these keys | none |
| `today` | the date, for comments (scripts cannot read the clock) | none |

## What one run does

1. **Triage** (one agent, medium effort). Reads every open SCO ticket and the
   PR list, and sorts each into: *reconcile* (has a PR), *build* (an agent can
   finish it on a branch), *owner* (needs a decision, an account or a purchase),
   *hardware* (needs a panel), *blocked*, or *epic*. Writes a brief for each
   build ticket: the change, the files, the tests, the acceptance criteria.
   The brief is all a builder gets, which is what keeps token use down.
2. **Reconcile** (one cheap agent per ticket, in parallel with building). A
   ticket whose PR merged goes to Done with a comment. One whose PR body still
   owes a deploy is commented but left open.
3. **Build** (one agent per ticket, each in its own git worktree, so they run
   in parallel without touching each other). Implements with tests, runs the
   affected suites, makes one commit on `sco-<n>`. Effort is set by triage:
   low for docs, medium for code, high for security-review tickets.
4. **Review** (a second agent per branch). Ordinary tickets get a correctness
   review; security-review tickets get an adversarial one whose job is to
   refute the change. Blocking findings go to a fix agent and a re-review.
5. **Record**. The ticket gets a comment (branch, commit, tests, deploy owed,
   verdict, open questions) and moves to In Progress.

Reconcile and build run side by side; within build, each ticket flows
through its stages independently, so a quick docs ticket is recorded while a
big one is still being reviewed.

## What it never does

Agents never push, tag, open or merge PRs, apply Terraform, publish the site,
build an image, or change anything in AWS or GitHub. Those are the main
session's and the owner's. A run ends with **branches to push**; the main
session pushes them and opens the PRs, the owner merges and deploys, and the
next run closes the tickets. Tickets that need the owner are listed, not
guessed at.

## Why it is shaped this way

- **Minimal context per agent.** A builder gets a brief and file names, not
  the conversation. A reviewer gets a diff. Triage is the only agent that
  reads the whole board.
- **Two agents per change.** The builder's own account of a change is not
  evidence; the reviewer reads the diff and names the test that proves each
  behavior. Security-review tickets are reviewed to be refuted.
- **Worktrees.** Parallel builders cannot collide, and a builder that gives
  up leaves a committed branch, never a dirty tree.
- **The board is the queue.** State lives in Jira and in git, not in the
  workflow, so a run can be repeated, resumed, or abandoned without loss.
