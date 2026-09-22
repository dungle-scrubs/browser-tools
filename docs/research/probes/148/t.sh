#!/bin/bash
# Transcript runner: logs the exact command line, its stdout+stderr, and exit code.
# Usage: bash t.sh '<label>' '<command line>'
# The command line is run verbatim through bash -c, so the transcript is exact.
LOG=/Users/kevin/dev/browser-tools/.scratch/retire-axi/work/148/transcript.log
label="$1"
cmd="$2"
{
  printf '\n### %s\n' "$label"
  printf '$ %s\n' "$cmd"
} | tee -a "$LOG"
out=$(cd /Users/kevin/dev/browser-tools/.scratch/retire-axi/work/148 && bash -c "$cmd" 2>&1); rc=$?
{
  printf '%s\n' "$out"
  printf '[exit %d]\n' "$rc"
} | tee -a "$LOG"
exit $rc
