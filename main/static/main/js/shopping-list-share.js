export class ShoppingListShareController {
    constructor({ navigatorRef = globalThis.navigator, statusElement = null } = {}) {
        this.navigator = navigatorRef;
        this.statusElement = statusElement;
    }

    setStatus(message) {
        if (this.statusElement) {
            this.statusElement.textContent = message;
        }
    }

    async copy(url) {
        if (!this.navigator?.clipboard?.writeText) {
            this.setStatus("Не удалось скопировать ссылку");
            return false;
        }

        try {
            await this.navigator.clipboard.writeText(url);
            this.setStatus("Ссылка скопирована");
            return true;
        } catch (_error) {
            this.setStatus("Не удалось скопировать ссылку");
            return false;
        }
    }

    async share(url) {
        if (!this.navigator?.share) {
            return this.copy(url);
        }

        try {
            await this.navigator.share({
                title: "План покупок Tannenberg",
                text: "План покупок по магазинам",
                url,
            });
            this.setStatus("План отправлен");
            return true;
        } catch (error) {
            if (error?.name !== "AbortError") {
                this.setStatus("Не удалось отправить план");
            }
            return false;
        }
    }
}

export function bindPriceAlertAutosave(form) {
    const toggle = form?.querySelector?.("[data-price-alert-toggle]");
    if (!toggle || typeof form.requestSubmit !== "function") {
        return false;
    }

    toggle.addEventListener("change", () => form.requestSubmit());
    return true;
}

function closeShareMenu(control) {
    const menu = control.closest?.("[data-share-menu]");
    if (menu) {
        menu.open = false;
    }
}

export function initializeShoppingListShare(documentRef = globalThis.document, navigatorRef = globalThis.navigator) {
    if (!documentRef) {
        return null;
    }

    const controller = new ShoppingListShareController({
        navigatorRef,
        statusElement: documentRef.querySelector("[data-share-status]"),
    });

    for (const button of documentRef.querySelectorAll("[data-share-plan]")) {
        button.addEventListener("click", async () => {
            await controller.share(button.dataset.shareUrl);
            closeShareMenu(button);
        });
    }
    for (const button of documentRef.querySelectorAll("[data-copy-plan]")) {
        button.addEventListener("click", async () => {
            await controller.copy(button.dataset.shareUrl);
            closeShareMenu(button);
        });
    }
    for (const link of documentRef.querySelectorAll("[data-share-menu] a")) {
        link.addEventListener("click", () => closeShareMenu(link));
    }
    for (const form of documentRef.querySelectorAll("[data-price-alert-form]")) {
        bindPriceAlertAutosave(form);
    }
    for (const form of documentRef.querySelectorAll("[data-clear-list-form]")) {
        form.addEventListener("submit", (event) => {
            if (!globalThis.confirm("Удалить все товары из списка?")) {
                event.preventDefault();
            }
        });
    }

    return controller;
}

if (typeof document !== "undefined") {
    initializeShoppingListShare();
}
