export class ShoppingListLiveController {
    constructor({
        documentRef = globalThis.document,
        fetchRef = globalThis.fetch,
        formDataFactory = (form) => new FormData(form),
    } = {}) {
        this.document = documentRef;
        this.fetch = fetchRef;
        this.formDataFactory = formDataFactory;
        this.toastTimer = null;
        this.handleSubmit = this.handleSubmit.bind(this);
    }

    initialize() {
        if (!this.document?.addEventListener || !this.fetch) {
            return false;
        }
        this.document.addEventListener("submit", this.handleSubmit);
        return true;
    }

    async handleSubmit(event) {
        const form = event.target?.closest?.("form[data-list-action]") || event.target;
        if (!form?.matches?.("form[data-list-action]") || event.defaultPrevented) {
            return;
        }

        const toast = this.document.querySelector("[data-site-toast]");
        event.preventDefault();
        const submitter = event.submitter || form.querySelector("button[type='submit']");
        if (submitter) {
            submitter.disabled = true;
            submitter.setAttribute("aria-busy", "true");
        }

        try {
            const liveRegion = this.document.querySelector("[data-shopping-list-live]");
            const response = await this.fetch(form.action, {
                method: "POST",
                body: this.formDataFactory(form),
                credentials: "same-origin",
                headers: {
                    Accept: "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-Shopping-List-Fragment": liveRegion ? "1" : "0",
                },
            });
            if (!response.ok) {
                throw new Error(`Shopping list request failed: ${response.status}`);
            }
            const payload = await response.json();
            this.applyPayload(payload, form, liveRegion);
        } catch (_error) {
            this.showToast(toast?.dataset.errorMessage || "Could not complete the action. Please try again.", true);
        } finally {
            if (submitter?.isConnected) {
                submitter.disabled = false;
                submitter.removeAttribute("aria-busy");
            }
        }
    }

    applyPayload(payload, form, liveRegion) {
        this.updateCount(payload.item_count);
        if (liveRegion && typeof payload.shopping_list_html === "string") {
            liveRegion.innerHTML = payload.shopping_list_html;
            this.document.dispatchEvent(new CustomEvent("shopping-list:updated"));
        } else if (form.dataset.listAction === "add") {
            const state = this.document.createElement("span");
            state.className = "list-state";
            state.textContent = `✓ ${payload.in_list_label || "In list"}`;
            form.replaceWith(state);
        }
        this.showToast(payload.message || "");
    }

    updateCount(count) {
        if (!Number.isInteger(count)) {
            return;
        }
        for (const element of this.document.querySelectorAll("[data-shopping-list-count]")) {
            element.textContent = `(${count})`;
        }
    }

    showToast(message, isError = false) {
        const toast = this.document.querySelector("[data-site-toast]");
        if (!toast || !message) {
            return;
        }
        toast.textContent = message;
        toast.hidden = false;
        toast.classList.toggle("is-error", isError);
        clearTimeout(this.toastTimer);
        this.toastTimer = setTimeout(() => {
            toast.hidden = true;
        }, 3200);
    }
}

if (typeof document !== "undefined") {
    new ShoppingListLiveController().initialize();
}
