# `/blackboard start <project>`

This sets up a new board with the person. It writes the goal, rules and progress memories and no intents: the lead's
first round writes those.

1. **Check the name is free.** Call `graph` with `tags_all ["board:<project>"]`. If anything comes back, say a board
   with that name already exists, offer `/blackboard status <project>`, and stop.
2. **Ask the person**, one question at a time, unless they have already said:
   - **Goal:** what the board is trying to achieve.
   - **Done when:** a condition a lead can check against facts, for example "a fact shows X, and facts cover Y". Vague
     answers like "when it's better" need a measurable form.
   - **Scope:** which shared-memory scope holds the board. Call `list_scopes`. It must be one they can write. Mention
     whether that scope is public-safe.
   - **Max open intents** (default 4) and **claim minutes**, which is how long a worker holds an intent before it is
     treated as abandoned (default 60).
3. **Draft the rules.** Draft them from their answers and from the workspace in front of you: its README, test and build
   commands, and any scripts that measure what the goal cares about. Use the rules template below, then show the draft
   to the person and ask for changes or approval. Do not write rules the person has not approved.
4. **Write the memories:**
   - the **goal**: the template below, tags `board:<project>`, `kind:goal`, `evergreen`, `state:running`;
   - the **rules**: the approved text, tags `board:<project>`, `kind:rules`, `evergreen`;
   - the **progress**: the template below, tags `board:<project>`, `kind:progress`, `evergreen`.

   Then link the rules and the progress each `depends-on` the goal.
5. **Tell the person how to run it:**
   - `/loop /blackboard lead <project>` in one session;
   - `/loop /blackboard work <project>` in as many others as they like, on any model;
   - `/blackboard status <project>`, `hint` and `stop` from anywhere.

## Goal template

```text
Goal: <one paragraph: what and why>
Done when: <the checkable condition>
Max open intents: <n>
Claim minutes: <n>
Started: <ISO date> by <the person>
```

## Rules template

```text
Rules for board <project>.
How to work: <where and how a worker does an intent, e.g. its own checkout or directory>
How to verify: <the commands or checks that make a result trustworthy>
Evidence: <what a fact must cite so others can check it, e.g. a command and its key output, a file, a commit>
Long output: <where it goes, e.g. a file under a given directory; facts cite the path>
Allowed beyond the base safety rules: <anything explicitly allowed, or "nothing">
Never: <anything forbidden on top of the base safety rules>
```

## Progress template (round 0)

```text
Progress for board <project>.
Round: 0
Last round at: never
Lead: none yet
Established: nothing yet.
Best result: none.
Open questions: none.
```
