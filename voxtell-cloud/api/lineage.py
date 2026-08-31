"""Per-tenant keying material for the QA lineage HMACs.

Why the server issues the key
-----------------------------
The plugin turns a DICOM series UID into an opaque key so the wire never carries
an identifier (see ``Services/LineageKeys.cs``). That key has to be **the same on
every workstation in a clinic**, or run 2 on a different machine would compute a
different key and never find run 1's baseline. A locally generated secret cannot
satisfy that without a sync mechanism we would then have to secure; deriving it
from one server-side master secret does, with no storage and no distribution
step.

What this does and does not protect
-----------------------------------
Stated plainly, because it would be easy to overstate:

* It stops identifiers reaching us. That is the actual goal, and it works: we
  receive a 256-bit tag and never the UID behind it.
* It does **not** protect against us. We hold the master secret, so we could
  recompute a tenant's key -- but we are never sent a UID to apply it to, so
  there is nothing to recompute *from*.
* Derivation is deterministic rather than stored, so a tenant's key survives a
  database restore and needs no migration. The flip side: rotating
  ``VOXTELL_LINEAGE_SECRET`` orphans every existing baseline at once, because
  every series key changes. That is a deliberate, documented cost, not routine
  hygiene.

Per-tenant rather than global so that one tenant's keys cannot be used to probe
whether another tenant holds a given series -- the same cross-tenant read
primitive that volume dedup is scoped per-user to avoid.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid

from .config import settings

# Domain separator, so this key cannot collide with any other use of the master
# secret that might be added later.
_LABEL = b"voxtell/lineage/tenant/v1"


def lineage_secret_for(user_id: uuid.UUID | str) -> str | None:
    """Hex keying material for one tenant, or ``None`` when the feature is off.

    ``None`` is a real answer, not a failure: a deployment with no master secret
    provisioned has QA lineage switched off, and the plugin responds by sending no
    lineage keys and recording no baselines. Falling back to a fixed default would
    make every deployment share a key, which is worse than the feature being
    unavailable.
    """
    master = (settings.VOXTELL_LINEAGE_SECRET or "").strip()
    if not master:
        return None

    message = _LABEL + b":" + str(user_id).encode("utf-8")
    return hmac.new(master.encode("utf-8"), message, hashlib.sha256).hexdigest()
