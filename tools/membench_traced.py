"""``python -m astrameter.main`` under tracemalloc, for ``membench.py --snap-at``.

Each SIGUSR2 writes ``snap-<n>.pickle`` into ``$TRACE_DIR``; diff two with
``tracemalloc.Snapshot.load(b).compare_to(tracemalloc.Snapshot.load(a), "lineno")``.
Tracing slows the process several-fold, so read growth from it, not totals.
"""

import gc
import os
import signal
import tracemalloc

tracemalloc.start(int(os.environ.get("TRACE_FRAMES", "12")))
_count = 0


def _dump(*_: object) -> None:
    global _count
    _count += 1
    gc.collect()
    path = os.path.join(os.environ.get("TRACE_DIR", "."), f"snap-{_count}.pickle")
    tracemalloc.take_snapshot().dump(path)


signal.signal(signal.SIGUSR2, _dump)

from astrameter.main import main  # noqa: E402

main()
