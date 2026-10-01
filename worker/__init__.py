"""The out-of-process Playwright worker (see ``pw_worker``).

A package only so the host can import ``pw_worker``'s stdlib-only helpers (the URL fence, the
JS it injects) and the suite can drive it in-process with a fake browser. In production the
module is run as a SCRIPT by a separate interpreter — never imported with playwright.
"""
