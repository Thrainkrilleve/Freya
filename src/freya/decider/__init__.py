"""
Freya's local decider: typed decisions with probabilities.

A proposal that is not adopted (docs/design/local-decider.md, NEW-CI to
NEW-CU). This package holds the contract, the registered decision
points, a decider made of the rules Freya uses today, and the
capability that offers it through the broker. Nothing in Freya calls
it yet, and the control plane does not register it.
"""
