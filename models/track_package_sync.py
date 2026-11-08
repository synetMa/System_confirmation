from odoo import api, fields, models


class TrackPackageSync(models.Model):
    _inherit = "track.package"

    colis_id = fields.Many2one(
        "system.confirmation.colis", string="Odoo Parcel", readonly=True, index=True
    )

    def _sync_colis_delivery_status(self, previous_states=None):
        colis_model = self.env["system.confirmation.colis"].sudo().with_context(active_test=False)
        previous_states = previous_states or {}
        for package in self:
            colis = package.colis_id or colis_model.search(
                [("tracking_number", "=", package.barcode)], limit=1
            )
            if colis:
                if package.colis_id != colis:
                    package.sudo().colis_id = colis
                colis.delivery_status = package.state
                if (
                    package.state == "Retournee"
                    and previous_states.get(package.id) != "Retournee"
                ):
                    colis._create_automatic_return("tracking", tracking_package=package)

    @api.model_create_multi
    def create(self, vals_list):
        packages = super().create(vals_list)
        packages._sync_colis_delivery_status()
        return packages

    def write(self, vals):
        previous_states = {package.id: package.state for package in self}
        res = super().write(vals)
        if "state" in vals:
            self._sync_colis_delivery_status(previous_states)
        return res
