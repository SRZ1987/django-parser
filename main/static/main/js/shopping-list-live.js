export class ShoppingListLiveController {
    constructor({
        documentRef = globalThis.document,
        fetchRef = globalThis.fetch,
        formEncoder = encodeForm,
    } = {}) {
        this.document = documentRef;
        this.fetch = fetchRef;
        this.formEncoder = formEncoder;
        this.toastTimer = null;
        this.pendingForms = new WeakSet();
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

        event.preventDefault();
        if (this.pendingForms.has(form)) {
            return;
        }
        this.pendingForms.add(form);

        const submitter = event.submitter || form.querySelector("button[type='submit']");
        if (submitter) {
            submitter.disabled = true;
            submitter.setAttribute("aria-busy", "true");
        }

        const liveRegion = this.document.querySelector("[data-shopping-list-live]");
        let payload;
        try {
            const response = await this.fetch(form.action, {
                method: "POST",
                body: this.formEncoder(form),
                credentials: "same-origin",
                headers: {
                    Accept: "application/json",
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-Shopping-List-Fragment": liveRegion ? "1" : "0",
                },
            });
            if (!response.ok) {
                throw new Error(`Shopping list request failed: ${response.status}`);
            }
            payload = await response.json();
        } catch (error) {
            console.error("Shopping list request failed.", error);
            const toast = this.document.querySelector("[data-site-toast]");
            this.showToast(toast?.dataset.errorMessage || "Could not complete the action. Please try again.", true);
            return;
        } finally {
            this.pendingForms.delete(form);
            if (submitter?.isConnected) {
                submitter.disabled = false;
                submitter.removeAttribute("aria-busy");
            }
        }

        try {
            this.applyPayload(payload, form, liveRegion);
        } catch (error) {
            // The server has already committed the action. Never replay this POST.
            console.error("Shopping list was updated, but the page could not be refreshed.", error);
            this.updateCount(payload.item_count);
            this.showToast(payload.message || "Shopping list updated.");
        }
    }

    applyPayload(payload, form, liveRegion) {
        this.updateCount(payload.item_count);
        if (liveRegion && typeof payload.shopping_list_html === "string") {
            liveRegion.innerHTML = payload.shopping_list_html;
            this.notifyUpdated();
        } else if (form.dataset.listAction === "add") {
            const state = this.document.createElement("span");
            state.className = "list-state";
            state.textContent = `\u2713 ${payload.in_list_label || "In list"}`;
            if (typeof form.replaceWith === "function") {
                form.replaceWith(state);
            } else {
                form.parentNode?.replaceChild?.(state, form);
            }
        }
        this.showToast(payload.message || "");
    }

    notifyUpdated() {
        try {
            const CustomEventClass = this.document.defaultView?.CustomEvent || globalThis.CustomEvent;
            if (typeof CustomEventClass === "function") {
                this.document.dispatchEvent(new CustomEventClass("shopping-list:updated"));
            }
        } catch (error) {
            console.warn("Shopping list controls could not be reinitialized.", error);
        }
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
        toast.classList?.toggle?.("is-error", isError);
        clearTimeout(this.toastTimer);
        this.toastTimer = setTimeout(() => {
            toast.hidden = true;
        }, 3200);
    }
}

export function encodeForm(form) {
    const pairs = [];
    for (const field of form.elements || []) {
        if (!field.name || field.disabled) {
            continue;
        }
        if ((field.type === "checkbox" || field.type === "radio") && !field.checked) {
            continue;
        }
        if (field.tagName === "SELECT" && field.multiple) {
            for (const option of field.options) {
                if (option.selected) {
                    pairs.push(`${encodeURIComponent(field.name)}=${encodeURIComponent(option.value)}`);
                }
            }
            continue;
        }
        pairs.push(`${encodeURIComponent(field.name)}=${encodeURIComponent(field.value ?? "")}`);
    }
    return pairs.join("&");
}

if (typeof document !== "undefined") {
    new ShoppingListLiveController().initialize();
}
