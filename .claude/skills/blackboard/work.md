# `/blackboard work <project>`

This is one work cycle: take one intent, do it, record one fact. Use your worker label from SKILL.md, made once in this
conversation, for every claim call.

1. **Read the goal and rules.**
   - Call `graph` with `tags_all ["board:<project>", "kind:goal"]` and `detail "full"`, then the same with `kind:rules`.
   - If there is no goal, or its state is not `state:running`, say the board is not running and **end the loop**.
   - Note the goal's scope and its `Claim minutes`.
2. **Take an intent,** in a separate call made after you have read step 1's result and seen `state:running`. Call
   `claim_next` with:
   - `tags_all ["board:<project>", "kind:intent", "state:open"]`;
   - `minutes`: the goal's claim minutes;
   - `session_label`: your label;
   - `description`: `Working on this for board <project>`.
3. **If nothing is free** (`claimed: false`), say `No free intent (empty cycle <k> of 3, label: <your label>)`, where
   `<k>` counts empty cycles in a row in this conversation. If `<k>` is 3, **end the loop**. Otherwise end this cycle.
4. **Check it is not already done.** Call `neighbours` on the intent with `direction "in"`, `types ["derived-from"]` and
   `detail "short"`. If any neighbour has the tag `kind:fact`, a previous worker already recorded its result before
   stopping. Skip to step 8 and write no fact.
5. **Do the intent under the rules.**
   - Read every fact and hint the intent cites, listed in its `links`, first.
   - Work only inside what the rules allow.
   - Ideas or problems outside the intent become hints: store a hint, don't do the extra work.
   - If the work may outlast the claim, renew it during the work, as in the next step.
6. **Confirm the intent is still yours.**
   - Call `claim_memory` on the intent with your `session_label`, the goal's claim minutes and the description
     `Working on this for board <project>`. From your own session this renews the claim.
   - If it is refused, your claim lapsed and another session holds the intent now. Store your result as a hint,
     `Partial result for intent <id>: <what you found>`, tagged `board:<project>`, `kind:hint` and `evergreen`, and
     linked `relates-to` the intent. Write no fact, change no tags, and end the cycle, saying so and ending with
     `(label: <your label>)`.
   - If it succeeds, repeat step 4's `neighbours` check. If a fact appeared meanwhile, go to step 8 and write no fact.
7. **Store exactly one fact** with the template below. Tag it `board:<project>`, `kind:fact`, `evergreen`, and one
   `verdict:` tag. Then link it `derived-from` the intent.
   - `verdict:confirmed`: the intent's question has an answer you checked.
   - `verdict:refuted`: the idea was tried and does not hold.
   - `verdict:inconclusive`: it could not be finished; say why in `Found:`.

   Keep the fact under about 1,200 characters. It holds only what this intent established. Long output goes where the
   rules say, and `Evidence:` cites it.
8. **Close the intent.**
   - `update` the intent's tags: the tags `claim_next` returned, with `state:open` swapped for `state:done`, and every
     other tag kept.
   - Call `release_claim` on the intent with your `session_label`.
   - Say in one line what the fact recorded, ending with `(label: <your label>)`. That ends the cycle.

## Fact template

```text
Fact for intent <intent id>: <the result in one line>
Did: <what you did, one to three lines>
Found: <only the new result>
Evidence: <what lets someone check it: the command and its key output, a file path, a commit>
```
