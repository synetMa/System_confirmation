import logging

from odoo import SUPERUSER_ID, api
from odoo.tools import float_compare


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    parcels = env["system.confirmation.colis"].with_context(active_test=False).search([])
    move_model = env["stock.move"]
    for parcel in parcels:
        if parcel.colis_state == "draft":
            parcel.stock_state = "pending"
            continue
        moves = move_model.search(
            [
                ("origin", "=", parcel.name),
                ("state", "=", "done"),
                ("company_id", "=", parcel.company_id.id),
            ]
        )
        demand = {}
        for line in parcel.items_line_ids.filtered(
            lambda item: item.product_id.type == "consu"
        ):
            quantity = line.uom_id._compute_quantity(
                line.quantity, line.product_id.uom_id
            )
            demand[line.product_id] = demand.get(line.product_id, 0.0) + quantity
        moved = {}
        for move in moves:
            quantity = move.product_uom._compute_quantity(
                move.product_uom_qty, move.product_id.uom_id
            )
            moved[move.product_id] = moved.get(move.product_id, 0.0) + quantity
        quantities_match = demand and set(demand) == set(moved) and all(
            float_compare(
                moved[product],
                quantity,
                precision_rounding=product.uom_id.rounding,
            )
            == 0
            for product, quantity in demand.items()
        )
        if quantities_match:
            parcel.write(
                {"outbound_move_ids": [(6, 0, moves.ids)], "stock_state": "done"}
            )
        elif demand:
            parcel.write(
                {
                    "stock_state": "error",
                    "stock_error": "Historical parcel has no linked outbound stock move; review required.",
                }
            )
        else:
            parcel.stock_state = "done"

    package_model = env["track.package"].with_context(active_test=False)
    for package in package_model.search([("colis_id", "=", False)]):
        parcel = parcels.filtered(lambda item: item.tracking_number == package.barcode)[:1]
        if parcel:
            package.colis_id = parcel
    _logger.info("Backfilled workflow traceability for %s parcels", len(parcels))
