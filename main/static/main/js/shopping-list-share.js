const DEFAULT_MESSAGES = {
    title: "Tannenberg shopping plan",
    text: "Shopping plan by store",
    shared: "Plan shared",
    copied: "Link copied",
    shareError: "Could not share the plan",
    copyError: "Could not copy the link",
    clearConfirm: "Remove all products from the list?",
};

export class ShoppingListShareController {
    constructor({ navigatorRef = globalThis.navigator, statusElement = null, messages = {} } = {}) {
        this.navigator = navigatorRef;
        this.statusElement = statusElement;
        this.messages = {
            ...DEFAULT_MESSAGES,
            ...Object.fromEntries(Object.entries(messages).filter(([, value]) => value)),
        };
    }

    setStatus(message) {
        if (this.statusElement) {
            this.statusElement.textContent = message;
        }
    }

    async copy(url) {
        if (!this.navigator?.clipboard?.writeText) {
            this.setStatus(this.messages.copyError);
            return false;
        }

        try {
            await this.navigator.clipboard.writeText(url);
            this.setStatus(this.messages.copied);
            return true;
        } catch (_error) {
            this.setStatus(this.messages.copyError);
            return false;
        }
    }

    async share(url) {
        if (!this.navigator?.share) {
            return this.copy(url);
        }

        try {
            await this.navigator.share({
                title: this.messages.title,
                text: this.messages.text,
                url,
            });
            this.setStatus(this.messages.shared);
            return true;
        } catch (error) {
            if (error?.name !== "AbortError") {
                this.setStatus(this.messages.shareError);
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

    const root = documentRef.querySelector(".shopping-list-actions");
    const messages = root?.dataset || {};
    const controller = new ShoppingListShareController({
        navigatorRef,
        statusElement: documentRef.querySelector("[data-share-status]"),
        messages: {
            title: messages.shareTitle,
            text: messages.shareText,
            shared: messages.statusShared,
            copied: messages.statusCopied,
            shareError: messages.statusShareError,
            copyError: messages.statusCopyError,
            clearConfirm: messages.clearConfirm,
        },
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
            if (!globalThis.confirm(controller.messages.clearConfirm)) {
                event.preventDefault();
            }
        });
    }
    return controller;
}

if (typeof document !== "undefined") {
    initializeShoppingListShare();
    document.addEventListener("shopping-list:updated", () => initializeShoppingListShare());
}
