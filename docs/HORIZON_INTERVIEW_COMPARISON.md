# Granite vs K2 Horizon interview inspection — 2026-10-07

## Setup

Ten scenarios per interviewer, using the current service/client `Interview`,
unchanged interview prompts, a fixed Granite LLM player, and a 12-assistant-turn
limit. No TTS, playback, or microphone. llama.cpp b11471; official Q4_K_M
quantizations. Raw evidence is in the ignored experiment output directory
`experiments/llm-runtimes/introduction-test-tts-sst/out/paired_interviews_roles_fixed_20261007/`.

## Observed results

| Measure | Granite 4.2 8B | K2 Horizon 7B |
|---|---:|---:|
| Required config fields populated | 9/10 | 1/10 |
| All strict scenario checks passed | 0/10 | 0/10 |
| Expected player name recorded | 1/10 | 0/10 |
| Expected companion name recorded | 1/10 | 0/10 |
| All expected style keywords preserved | 7/10 | 1/10 |
| Expected story/absence preserved | 4/10 | 5/10 |
| Tool calls across the ten runs | 39 | 4 |
| Exact repeated assistant replies beyond their first occurrence | 5 | 66 |
| Mean assistant turns | 11.9 | 11.5 |
| Runs with a recorded error | 1 | 1 |

Both errors were `JSONDecodeError: Unterminated string` in the LLM player's
schema-constrained response, not interviewer host crashes. The player has a
160-token output cap; truncation is a likely cause, not verified from these
saved records. Granite scenario 3 and Horizon scenario 2 ended early.

## Important validity problem

The player simulation failed despite the added role clarification. It regularly
answered with the companion name as its own name, adopted the companion role,
accepted invented names, and drifted into gameplay instead of declining a story.
For example, the Ada/Bob scenario began with the player answering `Bob`, and
the Max/Peter scenario answered `I am Peter`. Horizon's Iris/Ember scenario
received `I'm Ember, the AI companion.`

Consequently, wrong-name and story scores cannot fairly establish interviewer
accuracy: both interviewers were often supplied incorrect facts. Required-field
completion also means only structurally complete, not correct. The earlier
9/10 versus 1/10 figures must not be read as accurate successful interviews.
These are ten executed attempts per model, not twenty valid controlled trials.

## Qualitative judgment

**Granite did the better practical job in these observed conversations.** It
used tools much more often, usually advanced the conversation, and repeated
itself much less. It still compressed style details, sometimes invented a
companion name, and moved into gameplay before the benchmark's story condition
was satisfied. Its parenthetical instructions and confirmations could feel
mechanical.

**Horizon had the more colorful openings, but was not nicer overall.**
`where the walls have opinions and the coffee is always wrong` is a lively
opening. However, `a name as limp as a noodle left to dry` and repeated objections
to ordinary names felt more dismissive than playful. Repeating an acknowledged
question across most remaining turns made it frustrating. Most final configs
were empty despite conversational acknowledgements; schema-valid tools were
rarely invoked. In the Sofia/Pearl case it said it would `record it at once`
but did not call a tool, also exposing private bookkeeping language.

## Recommendation

Keep Granite as the default and Horizon as an explicit experimental option.
The results support that conservative deployment choice, but not a clean
general ranking of the models. Before publishing a reliable accuracy comparison,
repair and validate the player agent independently, use unambiguous human and
companion fact labels, preserve raw player responses/finish reasons, and reject
or flag persona violations rather than silently counting them against the
interviewer. A wall-clock case limit should supplement the existing turn limit
for future runs.