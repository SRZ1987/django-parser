import assert from "node:assert/strict";
import test from "node:test";

import { ShoppingListLiveController } from "../shopping-list-live.js";

function buildDocument({ liveRegion = null } = {}) {
    const count = { textContent: "(0)" };
    const toast = {
        dataset: { errorMessage: "Error", clearConfirm: "Clear?" },
        classList: { toggle: () => {} },
        hidden: true,
        textContent: "",
    };
    return {
        count,
        toast,
        querySelector: (selector) => {
            if (selector === "[data-site-toast]") return toast;
            if (selector === "[data-shopping-list-live]") return liveRegion;
            return null;
        },
        querySelectorAll: (selector) => selector === "[data-shopping-list-count]" ? [count] : [],
        createElement: () => ({ className: "", textContent: "" }),
        dispatchEvent: () => {},
        addEventListener: () => {},
    };
}

function buildForm(action = "add") {
    const submitter = {
        disabled: false,
        isConnected: true,
        setAttribute: () => {},
        removeAttribute: () => {},
    };
    return {
        action: `/my-list/${action}/1/`,
        dataset: { listAction: action },
        matches: () => true,
        closest: function () { return this; },
        querySelector: () => submitter,
        replaceWith: function (value) { this.replacement = value; },
        submitter,
    };
}

test("adding a product updates the list count and button without navigation", async () => {
    const documentRef = buildDocument();
    const form = buildForm("add");
    const controller = new ShoppingListLiveController({
        documentRef,
        fetchRef: async () => ({
            ok: true,
            json: async () => ({ item_count: 1, in_list_label: "In list", message: "Added" }),
        }),
        formDataFactory: () => ({}),
    });
    const event = { target: form, defaultPrevented: false, preventDefault: () => {} };

    await controller.handleSubmit(event);

    assert.equal(documentRef.count.textContent, "(1)");
    assert.equal(form.replacement.textContent, "✓ In list");
    assert.equal(documentRef.toast.textContent, "Added");
});

test("a shopping list fragment replaces only the live region", () => {
    const liveRegion = { innerHTML: "old" };
    const documentRef = buildDocument({ liveRegion });
    const controller = new ShoppingListLiveController({ documentRef, fetchRef: async () => {} });

    controller.applyPayload({ item_count: 0, shopping_list_html: "<section>Empty</section>" }, buildForm("remove"), liveRegion);

    assert.equal(liveRegion.innerHTML, "<section>Empty</section>");
    assert.equal(documentRef.count.textContent, "(0)");
});
