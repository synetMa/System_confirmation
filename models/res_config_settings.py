from odoo import fields, models



class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    quicklivraison_api_key = fields.Char(
        string="QuickLivraison API Key",
        config_parameter="system_confirmation.quicklivraison_api_key",
        help="API key used to send colis to QuickLivraison.",
    )
    quicklivraison_district_id = fields.Char(
        string="QuickLivraison District ID",
        config_parameter="system_confirmation.quicklivraison_district_id",
        help="District ID sent to QuickLivraison. Defaults to 43 when empty.",
    )
    system_confirmation_api_token = fields.Char(
        string="External API Token",
        config_parameter="system_confirmation.api_token",
        help="Secret token that external callers must send (field 'token') "
        "to the /create_package route.",
    )
    system_confirmation_allowed_location_id = fields.Many2one(
        "stock.location",
        string="Stock Location",
        config_parameter="system_confirmation.location_id",
        help="Stock location used for creating packages.",
    )
    system_confirmation_open_api_key = fields.Char(
            string="Openai api key",
            config_parameter="openai.api_key",
            help="Set your api key from openai here",
        )
    system_confirmation_quicklivraison_logo = fields.Binary(
        related="company_id.system_confirmation_quicklivraison_logo",
        readonly=False,
        string="Logo de Quicklivraison",
    )