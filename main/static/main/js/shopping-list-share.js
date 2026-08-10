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
                title: "План покупок Price Compare",
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

export function initializeShoppingListShare(documentRef = globalThis.document, navigatorRef = globalThis.navigator) {
    if (!documentRef) {
        return null;
    }

    const controller = new ShoppingListShareController({
        navigatorRef,
        statusElement: documentRef.querySelector("[data-share-status]"),
    });

    for (const button of documentRef.querySelectorAll("[data-share-plan]")) {
        button.addEventListener("click", () => controller.share(button.dataset.shareUrl));
    }
    for (const button of documentRef.querySelectorAll("[data-copy-plan]")) {
        button.addEventListener("click", () => controller.copy(button.dataset.shareUrl));
    }

    return controller;
}

if (typeof document !== "undefined") {
    initializeShoppingListShare();
}
