"""Aggregate metadata for Alembic autogenerate.

Importing every module's ``models`` registers its mapped classes onto ``Base.metadata``.
Alembic's env imports ``target_metadata`` from here. Add new model modules as they land.
"""

from __future__ import annotations

# Core infrastructure tables (not owned by a feature module): the outbox spine + idempotency
# ledger. Imported for mapper registration so their tables live on Base.metadata too.
from relay.core import idempotency as _idempotency  # noqa: F401
from relay.core import outbox as _outbox  # noqa: F401
from relay.core.base_model import Base

# Import for side effects (mapper registration). Keep alphabetized.
from relay.modules.ai import models as _ai  # noqa: F401
from relay.modules.automation import models as _automation  # noqa: F401
from relay.modules.billing import models as _billing  # noqa: F401
from relay.modules.channels import models as _channels  # noqa: F401
from relay.modules.crm import models as _crm  # noqa: F401
from relay.modules.identity import models as _identity  # noqa: F401
from relay.modules.knowledge import models as _knowledge  # noqa: F401
from relay.modules.messaging import models as _messaging  # noqa: F401
from relay.modules.outbound import models as _outbound  # noqa: F401
from relay.modules.platform import models as _platform  # noqa: F401
from relay.modules.reporting import models as _reporting  # noqa: F401
from relay.modules.tickets import models as _tickets  # noqa: F401

target_metadata = Base.metadata
