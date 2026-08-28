"""One-Click Marketing: a distilled reference implementation of a closed marketing loop.

Two loops share one spine:

    organic:  generate -> evaluate (gate) -> approve -> publish -> collect -> learn -> generate
    paid:     generate creative -> evaluate (gate) -> sign approval -> distribute
              -> collect -> learn

Nothing publishes and nothing spends without a human approval that is
cryptographically bound to the exact content being approved.

Every transport in this repository is a dry-run stub. See README for what is distilled
from a production system versus what is illustrative scaffolding.
"""

__version__ = "0.1.0"
