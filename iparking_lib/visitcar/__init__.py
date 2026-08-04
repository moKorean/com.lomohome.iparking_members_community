"""The one Homey driver: `visitcar`. Imports the `homey` SDK, so on-device only.

Kept out of `iparking_lib/iparking/` on purpose — that package must stay free of
`import homey` (acceptance criterion 1), which is what makes the whole client testable
off-device. Everything here is the thin Homey-facing layer on top of it.
"""
