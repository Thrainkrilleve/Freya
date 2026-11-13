"""
Freya across more than one machine (proposed NEW-DB to NEW-DM,
`docs/design/worker-nodes.md`).

This first part finds what a machine could lend and keeps the owner's
decisions about it. It opens no network connection and sends no work
anywhere. Core does not import it yet.
"""
