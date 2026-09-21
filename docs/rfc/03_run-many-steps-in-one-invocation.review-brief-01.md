<!--
The brief sent to the reviewer, verbatim, committed so the review in
03_run-many-steps-in-one-invocation.review-01.md is auditable against what it was
asked to do. It was written against RFC-03 version 2 and refers to the drafting
session's git-ignored task directory under .scratch/rfc-03-run/; the evidence it
points at is now in 03_run-many-steps-in-one-invocation.measurements.md.
-->

# Brief: adversarially review RFC-03

You are reviewing a specification. Attack it. Do not improve it, do not rewrite it,
and do not be agreeable.

The RFC was drafted by a Claude model. You are a different family on purpose. Defer
to nothing in it because it sounds confident or because it cites a line number. Check
the line numbers.

Repository root: `/Users/kevin/dev/browser-tools`. You have read access. Read the
files yourself; nothing is pasted into this brief.

## The artifact under review

`docs/rfc/03_run-many-steps-in-one-invocation.rfc.md` (version 2, status Draft).

It specifies one new CLI verb, `run`, that executes an ordered list of verb phrases
against one CDP session in one invocation.

## Read these before reviewing

1. `CONTEXT.md` - the project's domain glossary and naming authority. The nouns
   **One-Shot Session**, **CDPRuntime**, **UID**, **Bounded Capture** and
   **The Manual** all bear on this design.
2. `docs/rfc/01_merge-chrome-agent-core-into-browser-tools.rfc.md` - the accepted
   architecture RFC-03 lands in. Its scope boundary is at line 36, its declined
   long-lived recorder at line 43, its CLI surface at lines 148 to 210, its exit
   code rules at lines 200 to 207, its packaging at 339.
3. `src/browser_tools/GUIDE.txt` - the shipped CLI manual. 346 lines. The verb
   surface RFC-03 derives its step surface from.
4. `.scratch/rfc-03-run/measurements.md` - the evidence behind RFC-03's Motivation,
   plus the harnesses that produced it (`measure.sh`, `flow.sh`, `floor_probe.py`)
   and the raw per-run numbers (`raw*.tsv`).
5. Source the RFC makes claims about: `src/browser_tools/one_shot.py`,
   `src/browser_tools/curated.py`, `src/browser_tools/frame_manager.py`,
   `src/browser_tools/cli.py`, `src/browser_tools/tool_registry.py`,
   `src/browser_tools/extras.py`.

## Attack these specifically

Answer every one of the nine. A question you cannot settle from the repository gets
"unsettled" plus what evidence would settle it. Do not skip one.

1. **Does the measurement support the Motivation?** RFC-03 claims a ten-step flow
   drops from 1.70 s to 0.85 s, 50%. Check `floor_probe.py` against `flow.sh`: are
   they doing the same work? `floor_probe.py` calls internal functions directly.
   Is that a fair floor, or does it skip work the real verb would have to do?
   `measurements.md` states most of the remaining 0.85 s is two quiet windows. If
   that is right, what is the saving on a flow with no waits in it?

2. **Is "one CDPRuntime for the run" achievable?** Design says every step MUST run
   over one CDPRuntime. But `passthrough`, `wait`, `console-list`, `network-list`
   and `screenshot` open their own One-Shot Session today (`one_shot.py`,
   `curated.py:550` onward). Does routing them through `CDPHandler` change their
   observable behavior? `wait` and `console-list` subscribe to CDP events. Over a
   shared long-lived session, can two steps' subscriptions collide, or can a step
   see events a previous step's subscription buffered? RFC-01 requires `attach`
   subscriptions be isolated per session; does a Step Run violate the spirit of
   that?

3. **Can validation really run before the first step?** Design claims every
   malformed step is caught before step 1 executes, preserving the exit-2 contract.
   Is that true for a raw `Domain.method` passthrough step, where whether the
   browser supports the method is unknown until it is sent? Does the RFC overclaim?
   Where exactly is the line between a usage error and a runtime failure, and is it
   drawn consistently in the Error Handling table?

4. **Is the amendment to the exit-1 stdout rule safe?** RFC-03 makes `run` the one
   verb that prints on stdout while exiting 1. Find the hazard. What breaks in a
   caller that was written against the old rule? Is `run.status` a sufficient guard?
   Does this conflict with RFC-01's "a failed operation MUST NOT report a success
   field"?

5. **Is the no-data-flow decision defensible, or does it make the verb pointless?**
   With no variables and no data flow between steps, a run cannot use a UID a step
   in the same run discovered. Count the flows that remain. Are frame-scoped reads
   and fixed sequences a real enough case to justify a new verb, a new noun in
   `CONTEXT.md`, and an amendment to the output contract? Argue the strongest case
   that this verb should not exist at all.

6. **Is the frame-selection claim correct?** RFC-03 states that across a navigation
   the selection re-resolves against its stored URL pattern rather than clearing,
   citing `frame_manager.py:349-353`, and that it clears on detach, citing
   `frame_manager.py:313-314`. Read that code. Is the description accurate? Look
   specifically at what happens when re-resolution finds no match.

7. **Does `screencast` work as a step?** RFC-03 includes it because it is a Bounded
   Capture. Check `src/browser_tools/screencast.py` and `curated.py`. Does it hold
   state or a recorder that would conflict with a following step over the same
   runtime?

8. **Verify every citation.** The RFC cites specific line numbers in `GUIDE.txt`,
   `frame_manager.py`, `curated.py`, `cli.py`, and RFC-01. Check each one. Report
   every citation that does not say what the RFC claims it says.

9. **Is the step surface derived correctly?** RFC-03 lists 13 verbs plus the
   passthrough and excludes the rest. Compare against `_KNOWN_VERBS` in `cli.py`
   and `REGISTRY_VERBS`. Is anything wrongly included or wrongly excluded? Note
   that `window-border` appears in RFC-01's no-endpoint list but not in the
   shipped `REGISTRY_VERBS`; does that drift affect RFC-03's derivation?

## Skills to load

This route has no skills capability, so the skill files cannot be loaded as skills.
The three that govern this work are named here with their paths, and the rules that
bear on your output are written out inline below. Read the files if your tooling can
reach them; otherwise follow the inlined rules, which are sufficient.

1. **research** (`~/.agents/skills/research/SKILL.md`) - governs how you evidence
   findings. The rules that apply: primary sources only, meaning the code and the
   documents in this repository, never your recollection of how a tool usually
   behaves. Every claim carries a citation. A claim you cannot cite is reported as
   unsettled rather than asserted. State the standing of each claim: verified from
   source, or inferred.

2. **technical-writing** (`~/.agents/skills/technical-writing/SKILL.md`) - governs
   the review prose. The rules that apply: open with the finding and why it matters.
   Put consequences and uncertainty beside the claim they qualify. Use the project's
   canonical names, which are in `CONTEXT.md`. A skimmable summary must not imply
   more confidence than the detailed findings support.

3. **talk-normal** (`~/.agents/skills/talk-normal/SKILL.md`) - governs the voice.
   The rules that apply: short sentences, one idea per sentence, plain verbs,
   present tense. One name per thing, taken from `CONTEXT.md`. No analogies and no
   metaphor. **No em dashes; use a plain hyphen.** No praise, no reassurance, no
   commentary on how good or bad the RFC is as writing. State the defect and move
   on.

## Ground rules

- Cite `file:line` for every claim you make about the code. A claim without a
  citation will be discarded.
- Separate three things and label each finding as one: a **spec defect** (the RFC
  says something false or self-contradictory), an **evidence gap** (a claim the
  measurements do not support), or a **design disagreement** (the RFC is coherent
  and you would decide differently).
- Rank findings by severity: blocking, major, minor.
- Do not propose replacement prose. Say what is wrong and what would have to change.
- If a section is sound, say so in one line. Do not pad.
- Four questions were already decided by the repository owner on 2026-09-21 and are
  recorded in the RFC's Decisions section: the restricted API execution model, the
  verb name `run`, no default `--timeout`, and the provisional Run Document shape.
  You MAY argue a decision is wrong, but label it a design disagreement and note
  that it is already decided.

## Output

You have read-only access to this repository on purpose: you are reviewing an
artifact, not editing one. Do not attempt to write, edit or create any file.

Return the complete review as your response, in Markdown. The caller writes it
to disk.

Structure it as:

1. **Verdict**: accept, accept with changes, or reject. One paragraph of reasoning.
2. **Blocking findings**, then **major**, then **minor**. Each with: the label
   (spec defect / evidence gap / design disagreement), the location in the RFC, the
   citation that proves it, and what would have to change.
3. **The nine questions**, answered in order, each in a few sentences.
4. **Citation audit**: a table of every RFC citation you checked and whether it
   holds.
5. **What is sound**: one line per section you found no fault with.

## requirements

- The complete review is returned as the response, in Markdown, and no file is written.
- Every finding carries a `file:line` citation.
- Every finding is labelled spec defect, evidence gap, or design disagreement.
- Every finding is ranked blocking, major, or minor.
- All nine attack questions are answered, or explicitly marked unsettled with the
  evidence that would settle them.
- A citation audit table is present covering the RFC's line-number citations.
- An overall verdict of accept, accept with changes, or reject is stated.
