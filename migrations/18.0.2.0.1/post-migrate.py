import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    lines = env["system.confirmation.colis.lines"].with_context(active_test=False)
    last_id = 0
    count = 0
    while batch := lines.search([("id", ">", last_id)], order="id", limit=1000):
        last_id = batch[-1].id
        count += len(batch)
        for name in ("shipping_cost", "cost_of_packaging"):
            env.add_to_compute(lines._fields[name], batch)
        env.flush_all()

    _logger.info("Recomputed reporting cost shares for %s parcel lines", count)
