"""Service interface for the `reporting` module.

This is the ONLY surface other modules may import (plus `events`). Reaching into
another module's `models`/`router` is forbidden and enforced by import-linter
(see .importlinter). Modules otherwise communicate via domain events on the outbox.
"""

from __future__ import annotations
