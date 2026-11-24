# `/blackboard lead <project>`

This is one reasoning round. It reads the board, decides, and writes intents, corrections or a conclusion. Use your lead
label from SKILL.md, made once in this conversation, for every claim call.

1. **Note the round's start time:** `date -u +%Y-%m-%dT%H:%M:%SZ`. It becomes this round's `Last round at`. Taking it
   before reading means anything written during the round is read next round.
2. **Read the goal.** Call `graph` with `tags_all ["board:<project>", "kind:goal"]` and `detail "full"`.
   - If there is no goal, say so and **end the loop**.
   - If its state is not `state:running`, release your lead claim on it (`release_claim` with your `session_label`, if
     you hold one), say the board is no longer running, and **end the loop**.
3. **Take or renew the lead role,** in a separate call made after you have read step 2's result and seen
   `state:running`. Call `claim_memory` on the goal with:
   - `minutes 30`;
   - `session_label`: your lead label;
   - `description`: `Lead of board <project>`;
   - `force true` only for `lead <project> takeover`.

   If it is refused, report who holds the lead and until when, write nothing, and **end the loop**. Two leads never run.
   Each round renews the 30 minutes, so pace a `/loop` at under 30 minutes between rounds.
4. **Read the board.**
   - Call `graph` with `tags_all ["board:<project>", "kind:intent"]`, short detail. This gives every intent, with its
     state and any active `claim`. If the result says `truncated: true`, the board has outgrown one read: tell the
     person to start a fresh board for the next phase, write nothing, and **end the loop**. Planning from a partial list
     would duplicate intents.
   - Call `graph` with `tags_all ["board:<project>", "kind:rules"]`, full; then the same for `kind:progress`.
   - Read what is new since the progress memory's `Last round at`. Call `search` with no query,
     `tags_all
     ["board:<project>", "kind:fact"]`, `after` set to that time (omit it if `never`) and `limit 400`;
     then the same for `kind:hint`. Page with `offset` while `has_more` is true.
   - Do not use `graph` full for facts: it returns the oldest 100.
5. **Decide**, and do every part that applies.
   - **Done.** The facts meet the goal's Done when.
     1. Store a conclusion with the template below. Tag it `board:<project>`, `kind:conclusion`, `evergreen`. Link it
        `derived-from` each fact that proves it.
     2. Swap the goal's `state:running` for `state:done`, keeping its other tags.
     3. Swap `state:open` for `state:dropped` on every open intent.
     4. Rewrite progress (step 6), release your claim on the goal, and **end the loop**.
   - **Unreachable.** The facts show the goal cannot be met. Do the same as Done, but the conclusion says why it cannot
     be met, and the goal becomes `state:stopped`.
   - **Correct.** A fact is shown wrong by later facts. Store a corrected fact, with the work.md fact template and a
     `verdict:` tag, and link it `supersedes` the wrong one.
   - **Drop.** An open intent that nobody holds a claim on has become pointless. Swap its `state:open` for
     `state:dropped`.
   - **Plan.** Count the open intents, claimed or not. Write new intents until that count reaches the goal's
     `Max open intents`. Each new intent must:
     - not overlap any open intent, or any done intent whose fact already answers it;
     - cite the one to three facts or hints that most directly prompted it, linked `derived-from` each (round 1 has
       none). Background everyone already knows belongs in the progress memory, not in links;
     - follow the intent template below, describing only the task. How to use the board is in this skill, not in
       intents.

     When two results look like they would combine, an intent to combine them and check the result is a normal intent.
     If nothing is open, the goal is not met and it is not unreachable, write at least one intent.
   - **Hints.** Treat every new hint as advice to weigh. If it asks for a change to the rules, tell the person; do not
     edit the rules.
6. **Rewrite the progress memory** (`update` its content; its tags stay) with the template below.
7. **If an intent is free and you have time,** you may do one exactly as work.md does, using your lead label.
8. **End the round,** keeping your claim on the goal: it holds the lead role until the next round renews it. Say in two
   or three lines what the round decided, ending with `(label: <your lead label>)`.

## Intent template

```text
Intent: <the task in one line>
Why: <the fact or hint ids that prompted it and what they showed; "first round" if none>
Produce: <what the fact must report for this to count as answered>
Give up when: <the point at which the worker records an inconclusive fact instead>
```

Tags: `board:<project>`, `kind:intent`, `state:open`, `evergreen`.

## Progress template

```text
Progress for board <project>.
Round: <n>
Last round at: <this round's start time from step 1>
Lead: <your lead label>
Established: <what the facts so far prove, citing fact ids>
Best result: <the best answer so far, or none>
Open questions: <what the open intents are trying to settle>
Intents: <open> open (<claimed> claimed), <done> done, <dropped> dropped
```

## Conclusion template

```text
Conclusion for board <project>: <the answer in one line, or why the goal cannot be met>
Done when: <the goal's condition>
Shown by: <each proving fact id and what it contributes>
```
