"""The `homey`-free client core.

No module in this package may import the `homey` SDK — this is the part of the app
that runs under plain CPython in the test suite. `iparking_lib/visitcar/` is where
the Homey-facing code lives.

Acceptance criterion 1 is a *literal* grep over this directory for the SDK's import
statement, asserting no output. A docstring that quoted that statement — or that quoted
the grep pattern itself — would fail the check while breaking nothing, so every module in
here refers to the rule in prose instead. Do not "helpfully" rewrite these mentions into
the code form they describe.
"""
