/** @odoo-module **/

import { Component, onMounted, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class CreationProcess extends Component {
    static template = "order_creation_process.main_view";

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.searchTimer = null;
        this.searchVersion = 0;
        this.state = useState({
            tab: "create", step: 1, stepDirection: "next", origin: "", cities: [], products: [], cart: [],
            query: "", amount: "", customer: this.emptyCustomer(), orders: [], page: 0, hasMore: false,
            editingId: false, editingName: "", editCustomer: this.emptyCustomer(),
            editItems: [], originalItems: [], busy: false, navigationVisible: false,
        });
        onWillStart(async () => {
            try {
                const [options, products, orders] = await Promise.all([
                    this.orm.call("system.confirmation.colis", "creation_options", []),
                    this.orm.call("system.confirmation.colis", "creation_products", [""]),
                    this.orm.call("system.confirmation.colis", "creation_orders", []),
                ]);
                const counts = new Map();
                for (const city of options.cities) {
                    const name = this.normalizeCity(city.name);
                    counts.set(name, (counts.get(name) || 0) + 1);
                }
                const cities = options.cities.map((city) => ({
                    ...city,
                    label: counts.get(this.normalizeCity(city.name)) > 1
                        ? `${city.name} (${city.code} · #${city.id})`
                        : city.name,
                }));
                Object.assign(this.state, {
                    origin: options.origin, cities, products,
                    orders: orders.orders, hasMore: orders.has_more,
                });
            } catch (error) {
                this.notifyError(error);
            }
        });
        onMounted(() => document.body.classList.add("o_order_creator_fullscreen"));
        onWillUnmount(() => {
            clearTimeout(this.searchTimer);
            document.body.classList.remove("o_order_creator_fullscreen");
        });
    }

    emptyCustomer() {
        return { name: "", phone: "", address: "", city: "", cityText: "" };
    }

    normalizeCity(value) {
        return (value || "").trim().toLocaleLowerCase();
    }

    onCityInput(customer, event) {
        customer.cityText = event.target.value;
        const match = this.state.cities.find(
            (city) => this.normalizeCity(city.label) === this.normalizeCity(customer.cityText)
        );
        customer.city = match ? String(match.id) : "";
    }

    notifyError(error) {
        this.notification.add(
            error?.data?.message || error?.message || "Une erreur s'est produite.",
            { type: "danger" }
        );
    }

    onProductImageError(event) {
        event.target.hidden = true;
    }

    toggleNavigation() {
        this.state.navigationVisible = !this.state.navigationVisible;
        document.body.classList.toggle("o_order_creator_fullscreen", !this.state.navigationVisible);
    }

    stateClass(state) {
        if (state === "delivered") return "o_order_creator__badge--success";
        if (["confirmed", "PICKED_UP", "PICKED_UP_RAMASSEUR", "Recu", "Expedie",
            "EN_VOYAGE", "Distribution", "CLIENT_INTERESE", "RELENCE_NEW_CLIENT"].includes(state)) {
            return "o_order_creator__badge--info";
        }
        if (["Pending", "WAITING_PICKUP", "Encours", "POSTPONED", "PROGRAMMER", "LATER",
            "NOANSWER", "DEUX", "TROIS", "QUATRE", "WAIT_RELANCE", "RETURNED_CLIENT"].includes(state)) {
            return "o_order_creator__badge--warning";
        }
        if (["Retourne", "UNREACHABLE", "INJOI", "ERR", "OUT_OF_AREA", "REFUSE", "DOUBLE"].includes(state)) {
            return "o_order_creator__badge--danger";
        }
        return "o_order_creator__badge--neutral";
    }

    onSearch(event) {
        this.state.query = event.target.value;
        clearTimeout(this.searchTimer);
        const version = ++this.searchVersion;
        this.searchTimer = setTimeout(async () => {
            try {
                const products = await this.orm.call(
                    "system.confirmation.colis", "creation_products", [this.state.query]
                );
                if (version === this.searchVersion) {
                    this.state.products = products;
                }
            } catch (error) {
                this.notifyError(error);
            }
        }, 300);
    }

    addItem(product) {
        const item = this.state.cart.find((line) => line.id === product.id);
        if (item) {
            item.quantity++;
        } else {
            this.state.cart.push({ ...product, quantity: 1 });
        }
    }

    removeItem(product) {
        const item = this.state.cart.find((line) => line.id === product.id);
        if (item && --item.quantity <= 0) {
            this.state.cart = this.state.cart.filter((line) => line.id !== product.id);
        }
    }

    nextStep() {
        if (!this.state.cart.length) {
            this.notification.add("Ajoutez au moins un produit.", { type: "warning" });
            return;
        }
        this.state.stepDirection = "next";
        this.state.step = Math.min(3, this.state.step + 1);
    }

    previousStep() {
        this.state.stepDirection = "back";
        this.state.step = Math.max(1, this.state.step - 1);
    }

    async refreshOrders() {
        const result = await this.orm.call(
            "system.confirmation.colis", "creation_orders", [this.state.page * 50]
        );
        this.state.orders = result.orders;
        this.state.hasMore = result.has_more;
    }

    async createOrder() {
        if (this.state.busy) return;
        const { customer, amount, cart } = this.state;
        if (customer.cityText && !customer.city) {
            this.notification.add("Choisissez une ville dans la liste.", { type: "warning" });
            return;
        }
        if (!customer.name.trim() || !customer.phone.trim() || !customer.address.trim() ||
            !customer.city || amount === "" || !Number.isFinite(Number(amount)) || Number(amount) < 0 || !cart.length) {
            this.notification.add("Complétez les informations du client, le montant et les produits.", { type: "warning" });
            return;
        }
        this.state.busy = true;
        try {
            const order = await this.orm.call("system.confirmation.colis", "create_draft_order", [{
                client: customer, amount: Number(amount),
                items: cart.map((item) => ({ product_id: item.id, quantity: item.quantity })),
            }]);
            this.state.cart = [];
            this.state.customer = this.emptyCustomer();
            this.state.amount = "";
            this.state.step = 1;
            this.state.tab = "orders";
            this.state.page = 0;
            this.notification.add(`Commande ${order.name} créée en brouillon.`, { type: "success" });
            await this.refreshOrders();
        } catch (error) {
            this.notifyError(error);
        } finally {
            this.state.busy = false;
        }
    }

    editOrder(order) {
        this.state.editingId = order.id;
        const city = this.state.cities.find((item) => item.id === order.client_city);
        this.state.editCustomer = {
            name: order.client_name, phone: order.client_phone,
            address: order.client_address, city: String(order.client_city || ""),
            cityText: city ? city.label : "",
        };
        this.state.tab = "edit";
    }

    async editProducts(order) {
        if (this.state.busy) return;
        this.state.busy = true;
        try {
            const lines = await this.orm.call(
                "system.confirmation.colis", "creation_order_lines", [order.id]
            );
            this.state.editingId = order.id;
            this.state.editingName = order.name;
            this.state.originalItems = lines.map((line) => ({
                line_id: line.line_id, product_id: line.product_id, quantity: line.quantity,
            }));
            this.state.editItems = lines.map((line) => ({ ...line, key: `line-${line.line_id}` }));
            this.state.tab = "editProducts";
        } catch (error) {
            this.notifyError(error);
        } finally {
            this.state.busy = false;
        }
    }

    addEditProduct(product) {
        const line = this.state.editItems.find((item) => item.product_id === product.id);
        if (line) {
            line.quantity = Number(line.quantity) + 1;
        } else {
            this.state.editItems.push({
                key: `new-${product.id}`, line_id: false, product_id: product.id,
                name: product.name, default_code: product.default_code, quantity: 1,
            });
        }
    }

    changeEditQuantity(line, delta) {
        const quantity = Number(line.quantity) + delta;
        if (quantity <= 0) {
            this.removeEditProduct(line);
        } else {
            line.quantity = quantity;
        }
    }

    removeEditProduct(line) {
        this.state.editItems = this.state.editItems.filter((item) => item.key !== line.key);
    }

    async saveProducts() {
        if (this.state.busy) return;
        if (this.state.editItems.some((line) => !Number.isFinite(Number(line.quantity)) ||
            Number(line.quantity) <= 0)) {
            this.notification.add("Saisissez une quantité positive pour chaque produit.", { type: "warning" });
            return;
        }
        this.state.busy = true;
        try {
            await this.orm.call("system.confirmation.colis", "update_draft_products", [
                this.state.editingId,
                this.state.editItems.map((line) => ({
                    line_id: line.line_id, product_id: line.product_id,
                    quantity: Number(line.quantity),
                })),
                this.state.originalItems,
            ]);
            this.cancelEdit();
            this.notification.add("Produits enregistrés.", { type: "success" });
            await this.refreshOrders();
        } catch (error) {
            this.notifyError(error);
        } finally {
            this.state.busy = false;
        }
    }

    cancelEdit() {
        this.state.editCustomer = this.emptyCustomer();
        this.state.editItems = [];
        this.state.originalItems = [];
        this.state.editingId = false;
        this.state.tab = "orders";
    }

    async saveCustomer() {
        if (this.state.busy) return;
        if (!this.state.editCustomer.city) {
            this.notification.add("Choisissez une ville dans la liste.", { type: "warning" });
            return;
        }
        this.state.busy = true;
        try {
            await this.orm.call("system.confirmation.colis", "update_draft_customer", [
                this.state.editingId, this.state.editCustomer,
            ]);
            this.cancelEdit();
            this.notification.add("Informations du client enregistrées.", { type: "success" });
            await this.refreshOrders();
        } catch (error) {
            this.notifyError(error);
        } finally {
            this.state.busy = false;
        }
    }

    async showOrders() {
        this.state.tab = "orders";
        try {
            await this.refreshOrders();
        } catch (error) {
            this.notifyError(error);
        }
    }

    async changePage(delta) {
        this.state.page = Math.max(0, this.state.page + delta);
        try {
            await this.refreshOrders();
        } catch (error) {
            this.state.page -= delta;
            this.notifyError(error);
        }
    }

    showCreate() {
        this.state.tab = "create";
    }
}

registry.category("actions").add("order_creation_process", CreationProcess);
