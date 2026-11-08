import json
import logging
import re
import unicodedata
from openai import OpenAI
from odoo import http
from odoo.http import request
import qrcode
import base64
from io import BytesIO

_logger = logging.getLogger(__name__)


class CustomPageController(http.Controller):

    @http.route(
        "/create_package",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def create_package(self, **kwargs):
        # 1. Authenticate without exposing tokens in logs.
        expected_token = (
            request.env["ir.config_parameter"]
            .sudo()
            .get_param("system_confirmation.api_token")
        )

        if not expected_token or kwargs.get("token") != expected_token:
            _logger.warning("Rejected /create_package request: invalid token")
            return request.make_json_response(
                {"error": "Invalid or missing token"},
                status=403,
            )

        # 2. Read and validate input.
        telephone = (kwargs.get("telephone") or "").strip()
        name = (kwargs.get("name") or "").strip()
        adresse = (kwargs.get("adresse") or "").strip()
        ville_input = (kwargs.get("ville") or "").strip()
        shipping_cost = kwargs.get("shipping_cost")

        if not telephone:
            return request.make_json_response(
                {"error": "Phone number is required"},
                status=400,
            )

        if not name or not adresse:
            return request.make_json_response(
                {"error": "Name and address are required"},
                status=400,
            )

        Client = request.env["system.confirmation.client"].sudo()
        City = request.env["system.confirmation.villes"].sudo()

        # 3. Find existing client.
        client = Client.search(
            [("telephone", "=", telephone)],
            limit=1,
        )

        # Preserve existing values if the request sends empty fields.
        name = name or (client.name if client else "")
        adresse = adresse or (client.adresse if client else "")

        # 4. Resolve the city.
        # Priority:
        #   a. Existing client's city
        #   b. Explicit QuickLivraison city ID
        #   c. Explicit city name or alias
        #   d. City found inside the address
        #   e. OpenAI Tool fallback (corrects typos & searches Odoo DB)
        city = client.ville if client and client.ville else City.browse()
        detection_status = "existing_client" if city else "not_found"

        if not city and ville_input:
            if ville_input.isdigit():
                city = City.search(
                    [("code", "=", int(ville_input))],
                    limit=1,
                )
                if city:
                    detection_status = "provided_id"

            if not city:
                city, detection_status = self.resolve_city(ville_input)

        if not city:
            city, detection_status = self.resolve_city(adresse)

        if not city:
            return request.make_json_response(
                {
                    "error": "City could not be determined",
                    "status": detection_status,
                    "adresse": adresse,
                    "ville_received": ville_input,
                    "message": "Please add a city alias or correct the address.",
                },
                status=422,
            )

        # 5. Parse and validate package fields BEFORE writing the client.
        try:
            items = json.loads(kwargs.get("items_line_ids") or "[]")
        except (TypeError, ValueError):
            return request.make_json_response(
                {"error": "items_line_ids must be valid JSON"},
                status=400,
            )

        if not isinstance(items, list):
            return request.make_json_response(
                {"error": "items_line_ids must be a JSON list"},
                status=400,
            )

        origin = kwargs.get("origin")
        if not origin:
            return request.make_json_response(
                {"error": "Origin is required"},
                status=400,
            )

        origin_match = re.match(r"\s*(\d+)", str(origin))
        if not origin_match:
            return request.make_json_response(
                {"error": "origin must be a valid integer", "origin_received": origin},
                status=400,
            )
        origin_id = int(origin_match.group(1))

        try:
            amount = float(kwargs.get("amount") or 0.0)
            shipping_cost = float(kwargs.get("shipping_cost") or 0.0)
        except (ValueError, TypeError):
            return request.make_json_response(
                {"error": "amount and shipping_cost must be valid numbers"},
                status=400,
            )

        # 6. Create or update the client.
        values = {
            "name": name,
            "telephone": telephone,
            "adresse": adresse,
            "ville": city.id,
        }

        try:
            with request.env.cr.savepoint():
                if client:
                    client.write(values)
                else:
                    client = Client.create(values)
                    qr = qrcode.QRCode(
                        version=1,
                        error_correction=qrcode.constants.ERROR_CORRECT_L,
                        box_size=10,
                        border=4,
                    )
                    qr.add_data(client.telephone)
                    qr.make(fit=True)

                    qr_buffer = BytesIO()
                    qr.make_image(
                        fill_color="black",
                        back_color="white",
                    ).save(qr_buffer, format="PNG")

                    client.write(
                        {"qrcode": base64.b64encode(qr_buffer.getvalue())}
                    )

                # Client and parcel changes roll back together on any line failure.
                colis = (
                    request.env["system.confirmation.colis"]
                    .sudo()
                    .create_package(
                        {
                            "client": client.id,
                            "items_line_ids": items,
                            "origin": origin_id,
                            "date": kwargs.get("date"),
                            "amount": amount,
                            "shipping_cost": shipping_cost,
                            "client_adresse": adresse,
                            "client_ville": city.id,
                        }
                    )
                )

            return request.make_json_response(
                {
                    "success": True,
                    "colis": colis.name,
                    "colis_id": colis.id,
                    "client_id": client.id,
                    "ville": city.name,
                    "ville_code": city.code,
                    "city_detection": detection_status,
                }
            )

        except Exception:
            _logger.exception("Failed to create package")
            return request.make_json_response(
                {"error": "Failed to create package"},
                status=500,
            )


    @staticmethod
    def normalize_address(value):
        """Normalize French accents, Arabic characters, punctuation, and whitespace."""
        value = (value or "").lower().strip()
        
        # Normalization للفرنسية (إزالة Diacritics)
        value = unicodedata.normalize("NFKD", value)
        value = "".join(char for char in value if not unicodedata.combining(char))
        
        # Normalization للعربية (توحيد الألف والهمزات)
        value = re.sub(r"[آأإء]", "ا", value)
        value = re.sub(r"ى", "ي", value)
        value = re.sub(r"ؤ", "و", value)
        value = re.sub(r"ئ", "ي", value)
        
        # إزالة الفواصل والرموز
        value = re.sub(r"['\-_]", " ", value)
        value = re.sub(r"[^\w\s]", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def resolve_city(self, value):
        """
        Resolve a city name, alias, or a city mentioned
        inside a longer delivery address.
        """
        City = request.env["system.confirmation.villes"].sudo()

        normalized_value = self.normalize_address(value)
        if not normalized_value:
            return City.browse(), "not_found"

        # Step 1: Prefer exact city-name or keyword matches.
        exact_matches = City.search([])

        for city in exact_matches:
            aliases = [city.name]
            aliases.extend(city.keyword_ids.mapped("name"))

            for alias in aliases:
                if self.normalize_address(alias) == normalized_value:
                    return city, "exact_match"

        # Step 2: Search for city names and aliases inside the address using Regex.
        matches = []

        for city in exact_matches:
            aliases = [city.name]
            aliases.extend(city.keyword_ids.mapped("name"))

            for alias in aliases:
                normalized_alias = self.normalize_address(alias)

                if not normalized_alias:
                    continue

                pattern = r"(?<!\w)" + re.escape(normalized_alias) + r"(?!\w)"

                if re.search(pattern, normalized_value):
                    matches.append(
                        {
                            "city": city,
                            "alias": normalized_alias,
                            "length": len(normalized_alias),
                        }
                    )

        if matches:
            matches.sort(
                key=lambda item: item["length"],
                reverse=True,
            )

            longest_length = matches[0]["length"]
            best_matches = [
                item for item in matches if item["length"] == longest_length
            ]
            city_ids = {item["city"].id for item in best_matches}

            if len(city_ids) == 1:
                return best_matches[0]["city"], "matched"

        # Step 3: LAST OPTION - Fallback to OpenAI Function Calling / Tool
        return self._resolve_city_openai(value)

    def _resolve_city_openai(self, address_text):
        """
        Fallback city resolution using OpenAI Tool Calling.
        Extracts canonical city for Odoo lookup and raw address keyword for auto-learning.
        """
        City = request.env["system.confirmation.villes"].sudo()
        Keyword = request.env["system.confirmation.keyword"].sudo()

        api_key = request.env["ir.config_parameter"].sudo().get_param("openai.api_key")
        if not api_key:
            _logger.warning("OpenAI API key missing from ir.config_parameter")
            return City.browse(), "not_found"

        client = OpenAI(api_key=api_key)

        # Tool definition requesting BOTH canonical city and raw address keyword
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_city_in_db",
                    "description": "Search the Odoo database system.confirmation.villes for a city and register raw address keywords.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "candidate_city": {
                                "type": "string",
                                "description": "The corrected, canonical Moroccan city name in French (e.g. 'Casablanca', 'Fès', 'Dar Bouazza').",
                            },
                            "candidate_keyword": {
                                "type": "string",
                                "description": "The exact city/neighborhood keyword as written or extracted directly from the user's raw address (e.g., 'derbouaza', 'faas', 'casa').",
                            },
                        },
                        "required": ["candidate_city", "candidate_keyword"],
                    },
                },
            }
        ]

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a Moroccan delivery address parser. "
                    "Addresses and city names can be written in Arabic, French, or transliterated Darija "
                    "(e.g., 'آسفي' / 'Safi', 'الجديدة' / 'El Jadida', 'الدار البيضاء' / 'Casablanca'). "
                    "IMPORTANT: Always prioritize the explicitly named city or province in the address "
                    "(e.g. 'آسفي') over street or avenue names (e.g. 'شارع إدريس بناصر'), as street names can exist in multiple cities. "
                    "Identify the canonical Moroccan city name in French ('candidate_city', e.g. 'Safi') "
                    "and extract the city word as written in the raw input ('candidate_keyword', e.g. 'آسفي'). "
                    "Call 'search_city_in_db' with these parameters."
                ),
            },
            {
                "role": "user",
                "content": f"Extract the city from this address: '{address_text}'",
            },
        ]

        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )

            response_message = response.choices[0].message

            if response_message.tool_calls:
                for tool_call in response_message.tool_calls:
                    if tool_call.function.name == "search_city_in_db":
                        args = json.loads(tool_call.function.arguments)
                        
                        candidate_city = args.get("candidate_city", "").strip()
                        candidate_keyword = args.get("candidate_keyword", "").strip() or candidate_city

                        if not candidate_city:
                            continue

                        normalized_city = self.normalize_address(candidate_city)
                        matched_city = City.browse()

                        # 1. Search DB for the canonical city name or existing keywords
                        all_cities = City.search([])
                        for city in all_cities:
                            aliases = [city.name] + city.keyword_ids.mapped("name")
                            if any(self.normalize_address(alias) == normalized_city for alias in aliases):
                                matched_city = city
                                break

                        # 2. ILIKE query fallback on Odoo model if step 1 didn't match
                        if not matched_city:
                            matched_city = City.search([
                                "|",
                                ("name", "=ilike", f"%{candidate_city}%"),
                                ("keyword_ids.name", "=ilike", f"%{candidate_city}%"),
                            ], limit=1)

                        # 3. Save the RAW address keyword to Odoo database
                        if matched_city and candidate_keyword:
                            # Check if this exact raw keyword already exists for this city
                            existing_keyword = Keyword.search([
                                ("name", "=ilike", candidate_keyword),
                                ("ville_id", "=", matched_city.id),
                            ], limit=1)

                            # Create the keyword if it's new and not identical to city name
                            if not existing_keyword and matched_city.name.lower() != candidate_keyword.lower():
                                new_kw = Keyword.create({
                                    "name": candidate_keyword,
                                    "ville_id": matched_city.id,
                                })
                                _logger.info(
                                    "Learned new address keyword '%s' for city '%s' (ID: %s)",
                                    new_kw.name,
                                    matched_city.name,
                                    matched_city.id,
                                )

                            return matched_city, "openai_matched"

        except Exception as e:
            _logger.exception("OpenAI city lookup failed: %s", str(e))

        return City.browse(), "not_found"
