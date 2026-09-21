# Brief: adversarially review RFC-03 version 3, focusing on the fixes

You are reviewing a specification. Attack it. Do not improve it, do not rewrite it,
and do not be agreeable.

A Claude model drafted this RFC. You are a different family on purpose. Defer to
nothing in it because it sounds confident or because it cites a line number. Check
the line numbers.

Repository root for this review: `/Users/kevin/dev/browser-tools/.scratch/rfc-03-run/wt-rfc-v3`.
It is a git worktree with the RFC branch checked out. You have read access. Read
the files yourself; nothing is pasted into this brief.

## What is different about this review

**Version 2 of this RFC was already reviewed. You are reviewing version 3, which
was written in response to that review.** 440 lines changed between them and the
document grew from 789 to 1073 lines. The earlier review returned "accept with
changes" with two blocking findings, and nobody has yet checked whether the fixes
work.

So your job is narrower and harder than a fresh read. **The question is whether the
fixes actually answer the findings, and whether 440 lines of new text introduced
new defects.**

## Read first

1. `docs/rfc/03_run-many-steps-in-one-invocation.review-01.md` - the version 2
   review, committed. It carries the original verdict, every finding, and a table
   at the end claiming what version 3 did with each one. **Treat that table as a
   claim to verify, not as fact.**
2. `docs/rfc/03_run-many-steps-in-one-invocation.rfc.md` - version 3, the artifact
   under review.
3. `docs/rfc/03_run-many-steps-in-one-invocation.measurements.md` - all evidence,
   with the harnesses inlined and raw per-run data in its appendix.
4. `CONTEXT.md` - the naming authority.
5. `docs/rfc/01_merge-chrome-agent-core-into-browser-tools.rfc.md` - the accepted
   architecture. RFC-03 now claims to amend it.
6. `src/browser_tools/GUIDE.txt` - the shipped CLI manual, 346 lines.
7. Source RFC-03 makes claims about: `src/browser_tools/cdp_handler.py`,
   `src/browser_tools/one_shot.py`, `src/browser_tools/core/cdp_client.py`,
   `src/browser_tools/events.py`, `src/browser_tools/frame_manager.py`,
   `src/browser_tools/curated.py`, `src/browser_tools/cli.py`.

## Attack these specifically

Answer every one of the eight. A question you cannot settle from the repository
gets "unsettled" plus what evidence would settle it. Do not skip one.

1. **Does the B1 fix actually work?** Design, "One CDPRuntime for the run", now
   requires the One-Shot Session's browser-level attach, adds a domain enable and
   disable rule, and requires per-step handler removal. Read
   `src/browser_tools/cdp_handler.py:404-413`, `src/browser_tools/one_shot.py`,
   and `src/browser_tools/core/cdp_client.py:155-165`. Is the specified model
   buildable? The rule exempts `Page` and `Runtime` from the per-step disable
   because the frame manager needs them. Is that exemption sufficient, or are there
   other domains the runtime depends on? What breaks if a step enables a domain
   that a later step also needs?

2. **Does the B2 fix actually work?** The RFC now carries two amendments, one to
   The Manual and one to RFC-01:204. Read RFC-01:200-207. Is the amendment's
   wording correct and narrow enough? Does the ordered caller contract, exit code
   then `run.status` then steps, genuinely contain the hazard, or does it just
   describe it?

3. **Is the frame no-match rule right?** The RFC now requires a run to clear the
   selected frame id when re-resolution after a navigation finds no match. Read
   `src/browser_tools/frame_manager.py:200-235`, `:310-316`, `:345-355`. Is the
   RFC's description of current behavior accurate now? Is the new requirement
   consistent with `select_frame_by_url`, `get_selected_frame` and `reset_frame`?

4. **Does the argparse-reuse requirement hold up?** Validation now requires the step
   parser to reuse the existing `argparse` parsers rather than mirror them, and the
   RFC claims this removes the possibility of a usage error found mid-run. Read
   `src/browser_tools/cli.py`, especially `build_parser` and
   `_split_leading_instance` at `:828`. Can the existing parsers actually be reused
   for a step, given they are built for a full command line? What does argparse do
   on a parse failure that a run would have to intercept?

5. **Is the withdrawn motivation handled honestly?** Version 3 withdraws
   frame-selection continuity as the lead motivation, on the ground that
   `get_frame_storage` is its only consumer. Verify that count yourself: grep for
   `get_selected_frame` and `get_selected_execution_context_id` across `src/`. Is
   the count right? Does the RFC now overcorrect, claiming less than the truth?

6. **Do the two measured flows support what Motivation claims?** Flow A is 1696.3
   to 849.1 ms, 50%. Flow B is 787.8 to 82.7 ms, 90%. The RFC says the difference
   is entirely the intrinsic waits and that the honest headline is the per-step
   number. Check the harnesses and the raw appendix in the measurements file. Is
   flow B a fair comparison? Its floor probe calls `one_shot_page_session`
   directly. Does that skip work the real verb would do? Flow B's floor has wide
   spread, 59.1 to 83.8 ms over 5 runs. Is 5 runs enough to claim 90%?

7. **Did 440 lines of new text introduce contradictions?** Read version 3 as a
   whole. The earlier review found the RFC contradicting itself in two places.
   Look for new ones, especially between Design sections and the normative
   `GUIDE.txt` entry, and between the Decisions section and the body.

8. **Verify every citation added or changed in version 3.** Particularly the new
   ones: `cdp_handler.py:404-413`, `:411-413`, `:816`, `:856`, `:863`;
   `cdp_client.py:158-161`; `events.py:344`, `:350`, `:323-330`, `:367-368`;
   `frame_manager.py:207-208`, `:223-224`, `:229-230`, `:313-314`, `:349-353`;
   `list_verbs.py:114-116`; `cli.py:76`, `:848-854`; RFC-01:182-184, `:194`,
   `:200-207`, `:204`. Report every citation that does not say what the RFC claims.

## Skills to load

This route has no skills capability, so the skill files cannot be loaded as skills.
The three that govern are named here with their paths, and the rules that bear on
your output are written out inline. Read the files if your tooling can reach them;
otherwise follow the inlined rules, which are sufficient.

1. **research** (`~/.agents/skills/research/SKILL.md`) - primary sources only,
   meaning the code and documents in this repository, never your recollection of
   how a tool usually behaves. Every claim carries a citation. A claim you cannot
   cite is reported as unsettled rather than asserted.

2. **technical-writing** (`~/.agents/skills/technical-writing/SKILL.md`) - open
   with the finding and why it matters. Put consequences and uncertainty beside the
   claim they qualify. Use the canonical names in `CONTEXT.md`.

3. **talk-normal** (`~/.agents/skills/talk-normal/SKILL.md`) - short sentences, one
   idea per sentence, plain verbs, present tense. **No em dashes; use a plain
   hyphen.** No analogies. No praise and no commentary on the RFC as writing.

## Ground rules

- Cite `file:line` for every claim about code. A claim without a citation will be
  discarded.
- Label each finding: **spec defect** (the RFC says something false or
  self-contradictory), **evidence gap** (a claim the measurements do not support),
  or **design disagreement** (coherent, and you would decide differently).
- Rank findings blocking, major, minor.
- Do not propose replacement prose. Say what is wrong and what would have to change.
- Say explicitly, for each of B1, B2, M1, M2, M3, M4 from the earlier review,
  whether version 3 actually fixed it, partly fixed it, or claimed a fix that does
  not hold.
- Five questions were decided by the repository owner and are recorded in the RFC's
  Decisions section: the restricted API, the verb name `run`, no default
  `--timeout`, the provisional Run Document, and keeping the verb on the speed
  motivation. You MAY argue one is wrong, but label it a design disagreement and
  note that it is already decided.
- If a section is sound, say so in one line. Do not pad.

## Output

You have read-only access on purpose: you are reviewing an artifact, not editing
one. Do not attempt to write, edit or create any file.

Return the complete review as your response, in Markdown. The caller writes it to
disk. Structure it as:

1. **Verdict**: accept, accept with changes, or reject. One paragraph.
2. **Did version 3 fix the earlier findings?** A table, one row per earlier
   finding, with your judgment and the citation behind it.
3. **New findings**: blocking, then major, then minor. Each with the label, the
   location, the citation, and what would have to change.
4. **The eight questions**, answered in order.
5. **Citation audit** of the version 3 citations listed above.
6. **What is sound**: one line per section you found no fault with.

## requirements

- The complete review is returned as the response, in Markdown, and no file is written.
- Every finding carries a `file:line` citation.
- Every finding is labelled spec defect, evidence gap, or design disagreement.
- Every finding is ranked blocking, major, or minor.
- Each of B1, B2, M1, M2, M3 and M4 from the earlier review gets an explicit
  judgment of fixed, partly fixed, or not fixed.
- All eight attack questions are answered, or marked unsettled with the evidence
  that would settle them.
- A citation audit covering the version 3 citations is present.
- An overall verdict of accept, accept with changes, or reject is stated.
