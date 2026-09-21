# Layout mock-ups for the real panel

The panel is 400x1280: **3.2:1**. The scoreboard frame is drawn at 1920x480,
which is 4:1, so turned and scaled it lands as 1280x320 with **40 px of unused
glass along each long edge** (measured 2026-09-19, `docs/hardware-checks.md`).

These are drawn at 1920x600, the panel's own shape, with the project's real
fonts and a real game state. They are pictures to choose from, **not
production code**: `render_mockups.py` draws them and nothing else uses it.

| | |
|---|---|
| **A** as today | The 4:1 frame with the unused bands marked. The baseline. |
| **B** taller | The same layout grown into the height: digits and names about a quarter bigger. Reads from further away. Two penalty rows a side. |
| **C** info strip | Today's layout untouched, and a strip underneath: last goal, another game, the next game. The strip's content needs data the panel does not get yet. |
| **D** penalties | Today's sizes, with bigger penalty rows and room for a third a side. The smallest text on the panel is the thing that grows. |

![A](a-as-today.png)
![B](b-taller.png)
![C](c-info-strip.png)
![D](d-three-penalty-rows.png)

Whichever is chosen, two things come with it: the frame's height stops being a
constant (a panel that is 4:1 must still work), and the burn-in shift and the
stale-feed band are placed against the new layout and re-tested.
