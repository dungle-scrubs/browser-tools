#!/bin/bash
# Run Lighthouse against the lh-test profile instance while sampling that
# instance's tab list, so the Lighthouse tab is observed inside that browser.
cd /Users/kevin/dev/browser-tools/.scratch/retire-axi/work/151 || exit 1
: > profile-tabs.txt
(
  i=0
  while [ "$i" -lt 60 ]; do
    bt status 151-04 >> profile-tabs.txt 2>&1
    i=$((i+1))
    sleep 0.5
  done
) &
sampler=$!
npx -y lighthouse@latest https://httpbin.org/cookies --port=9234 --output=json \
  --output-path=./reports/profile-httpbin.json > lh-profile.stdout.txt 2> lh-profile.stderr.txt
echo "lighthouse exit=$?"
kill "$sampler" 2>/dev/null
wait "$sampler" 2>/dev/null
echo "distinct tab urls seen in instance 151-04 during the run:"
grep '"url"' profile-tabs.txt | sed 's/^ *//' | sort | uniq -c
echo "chrome-launcher lines:"
grep "ChromeLauncher" lh-profile.stderr.txt | head -3
