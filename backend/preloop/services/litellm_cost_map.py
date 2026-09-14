"""Pin LiteLLM to its bundled price map before the library is imported.

Importing litellm without ``LITELLM_LOCAL_MODEL_COST_MAP=true`` fetches the
GitHub price map and also parses the bundled backup JSON to count models, so
the process briefly holds two copies of a ~1.3MiB catalog. Preloop vendors
its own snapshot via ``model_price_catalog.load_catalog`` and live-looks-up
unknown models, so the remote fetch is redundant.

Call :func:`pin_local_litellm_cost_map` (or import this module) before
``import litellm``. ``setdefault`` leaves an operator override in place.
"""

from __future__ import annotations

import os

_LOCAL_COST_MAP_ENV = "LITELLM_LOCAL_MODEL_COST_MAP"


def pin_local_litellm_cost_map() -> None:
    """Default LiteLLM to the bundled map unless the operator already chose."""
    os.environ.setdefault(_LOCAL_COST_MAP_ENV, "true")


pin_local_litellm_cost_map()
