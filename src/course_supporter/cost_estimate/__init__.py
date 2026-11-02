"""Shadow price estimates of actions (BIL-D).

Before an action runs — a review of a submission on the new path, the
processing of an author's material, the preparation of a task — its expected
cost E is computed and written beside it (``action_cost_estimates``), so that
the calibration report can compare it with the call register. Shadow mode:
nothing is charged from it and nobody is shown it.

* :mod:`.core` — pricing one step, peak hours, units and their rounding.
* :mod:`.actions` — the estimate of each kind of action.
* :mod:`.settings` — ``config/cost_estimate.yaml``.
* :mod:`.shadow` — the write, which never breaks the action.
* :mod:`.hooks` — the one-line calls at the hook sites.
* :mod:`.report` — estimate against fact, for the calibration report.
"""
