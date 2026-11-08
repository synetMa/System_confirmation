import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Recompute shipping_cost / cost_of_packaging on the colis lines.

    In 18.0.1.0.1 those two fields changed from a plain related field (each
    line copied the whole colis value -> double counting in the pivot) to a
    computed share of the colis value. The stored values are stale, so force
    a full recompute here.
    """
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    Model = env["system.confirmation.colis.lines"]
    lines = Model.search([])
    if not lines:
        return

    for name in ("shipping_cost", "cost_of_packaging"):
        env.add_to_compute(Model._fields[name], lines)
    env.flush_all()

    _logger.info(
        "system_confirmation: colis costs shared over %s lines", len(lines)
    )
