# Importing the phase modules registers their handlers with the runner.
from app.services.indexing import (  # noqa: F401
    embed_phase,
    fetch_phase,
    graph_phase,
    parse_phase,
)
