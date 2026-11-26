# Parcel stock reservations

Active draft parcels reserve quantities of tracked products for other parcels
sharing the configured stock location, including across companies. Availability
is checked at `system_confirmation.location_id`, excluding Odoo stock
reservations and quantities in other active drafts. Cancelling or archiving a
draft releases its parcel reservation; restoring it checks availability again.
Untracked products are not subject to this check.

The `/create_package` endpoint returns HTTP 409 when a product cannot be
reserved: `{"error": "product_reserved", "message": "..."}`. The request's
customer and parcel changes are rolled back. The order-creation interface and
the standard parcel form display the reservation error instead of saving.

Draft parcel reservations are calculated from parcel lines; they are not Odoo
stock moves or quant reservations. Other inventory workflows can still consume
that stock, so confirmation checks availability again.
