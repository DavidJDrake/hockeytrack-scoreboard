# HockeyTrack Scoreboard

A physical NHL scoreboard: a Raspberry Pi Zero 2 W driving a 4:1 HDMI bar
display that follows one live game — clock, score, shots, penalties and the
players serving them — pushed from
[HockeyTrack](https://github.com/DavidJDrake/hockeytrack)'s EventBridge bus
through AWS IoT Core within about a second of the play.

It exists as much to be a worked example as a gadget. HockeyTrack publishes
every game event to an EventBridge bus, and the point of this project is to
show what consuming that looks like from the outside: one rule on someone
else's bus, and nothing in the pipeline changes to accommodate you.

**Status: work in progress.** The repository scaffold exists (Go module,
Python package, Makefile) but there is no reducer or device logic yet. See
the design and the implementation plan:

- [`docs/superpowers/specs/2026-09-06-scoreboard-design.md`](docs/superpowers/specs/2026-09-06-scoreboard-design.md)
  — the architecture, the hardware, the state document, the screen layout, and
  the open questions.
- [`docs/superpowers/plans/2026-09-06-scoreboard.md`](docs/superpowers/plans/2026-09-06-scoreboard.md)
  — fourteen tasks from an empty repository to a device on a bench.
- [`docs/pdf/`](docs/pdf/) — the spec as a PDF.

## How it is meant to work

A Lambda subscribes to five HockeyTrack detail-types and folds them into one
small JSON state document per game, which it republishes to an AWS IoT Core
topic as a *retained* message. The device subscribes to one topic and renders
whatever it last received, running the game clock locally between updates so
the display ticks every second even though the feed is sampled every five.
Retained messages are what make a power cut a non-event: a device that
reconnects is handed the current state immediately, with no history to replay.

## Developing it out of season

The NHL season is six months long, which leaves six months where the bus is
silent and a device author has nothing to build against. HockeyTrack solves
that on its side: `make livefire GAME=<id>` reconstructs any of the 72,921
archived games and republishes it to the real bus at whatever speed you ask
for, including real time. Synthetic runs carry the event source
`hockeytrack.synthetic`, so a consumer here needs a rule matching that source
(or matching both, so one rule serves drills and real games alike).

## License

MIT. NHL data belongs to the NHL; see the open question in the spec about team
logos before redistributing any.
