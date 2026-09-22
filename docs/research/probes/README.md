# Probes behind RFC-05

Every "Measured:" figure in `docs/rfc/05_retire-chrome-devtools-axi.rfc.md`
comes from one of these. They are the scripts as they were run, not a tidied
rewrite, so a reviewer can re-run the measurement rather than take the number.

They were written against a working checkout and carry absolute paths and a
virtualenv interpreter. Expect to adjust the path at the top of a script
before running it. They are evidence, not a test suite, and nothing in CI
runs them.

| Directory | What it measured | RFC section |
|---|---|---|
| `148/` | Which axi verbs have a raw CDP form and which need curation. Shell scripts against a live `bt` instance, one per verb group. `b10`-`b12` are the emulation-override probes that found five of six overrides revert. `b14`-`b17` are the dialog hang. | Design 1, 4, 5 |
| `151/` | Lighthouse against a `bt` instance. `wrongport.sh` shows a wrong `--port` exiting 0 with a report from a browser Lighthouse launched itself. `focus-sample.sh` samples the frontmost window during a run. | Design 5 |
| `153/` | Two instances in one directory. The two registries show the `-NN` suffix. | Motivation |
| `154/` | Tracing and heap. `exp-a*-steps.txt` are the step lists that fail in both transfer modes. `exp-b.py` is the single-invocation capture in both modes. `enginetest/` runs the real DevTools engine over a captured trace: `insights.mjs` for the Insight Set, `inp.mjs` for the interaction case, `coldstart.mjs` for the cost figures. | Design 2, 3 |
| `156/` | The dialog policy and `Input.insertText`. `exp-dialog.py` runs the control and both candidate shapes. `exp-type.py` round-trips multi-line prose. | Design 1, 4 |

## Running the engine probes

`154/enginetest/` needs Node and a trace file.

```sh
cd docs/research/probes/154/enginetest
npm ci
node insights.mjs /path/to/trace.json
```

`npm ci`, never `npm install`: the engine declares its own dependencies as
`latest`, so a bare install resolves whatever they published today. The
committed `package-lock.json` is what makes the run reproducible, and it is
the same requirement the RFC places on the shipped extra.

## What is not here

Captured traces, Lighthouse reports and heap snapshots. They are large,
they are outputs rather than probes, and every one is reproducible by running
the script that made it.

Process listings taken during the Lighthouse runs. They are the direct
evidence that a wrong `--port` makes chrome-launcher start its own browser,
and they are a full snapshot of one machine's processes, so they are not
committed. `wrongport.sh` reproduces the finding on the reviewer's own
machine.
