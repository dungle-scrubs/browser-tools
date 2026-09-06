# browser-tools

browser-tools gives CLI users and agents direct control of browser instances
through Chrome DevTools Protocol (CDP).

## Language

**Browser Instance**: A running browser tracked by a registry name and debug port.

**Named Profile**: A persistent, login-bearing browser data directory selected by a Profile Name.

**Profile Name**: A validated name for one Named Profile within the Profile Root.

**Profile Root**: The directory containing this user's Named Profiles.

**Page Target**: A browser page identified by its CDP target ID and URL.

**Target Slot**: The optional target-ID prefix or URL substring selecting a Page Target for an invocation.

**Curated Verb**: A CLI action that combines CDP operations into a browser task.

**Handler Tool**: A registered CDP action available through the `tool` verb.

**CDPRuntime**: The owner of one browser connection, its isolated page session, and its frame and capture state.

**One-Shot Session**: A page session lasting for one CLI operation.

**Capture**: A foreground recording that owns received screencast frames until it writes their artifacts.

**Interstitial**: An anti-bot challenge page that may interrupt navigation.

**Interstitial Detection**: The policy that identifies Interstitials and retries eligible challenges.

## Relationships

- A **Profile Root** contains zero or more **Named Profiles**.
- A **Browser Instance** may use one **Named Profile** and contains zero or more **Page Targets**.
- A **Target Slot** resolves to one **Page Target** or produces an ambiguity or missing-target error.
- A **CDPRuntime** owns at most one active **Capture**.
- A **Curated Verb** uses a **CDPRuntime** or a **One-Shot Session**.

## Example dialogue

> **Dev:** "Does stopping a Browser Instance remove its Named Profile?"
> **Domain expert:** "No. The Named Profile preserves browser data for a later launch."
> **Dev:** "Can another invocation stop a Capture?"
> **Domain expert:** "Send a signal to the owning process. That process writes its frames before exiting."

## Flagged ambiguities

- "Session" alone is ambiguous. Use **Browser Instance**, **CDPRuntime**, or **One-Shot Session**.
- A **Target Slot** selects a page for one invocation; it is not persistent active-page state.
