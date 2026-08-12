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
        elements: [
            { name: "csrfmiddlewaretoken", value: "csrf-token", type: "hidden", disabled: false },
        ],
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
        fetchRef: async (_url, options) => {
            assert.equal(options.body, "csrfmiddlewaretoken=csrf-token");
            assert.equal(options.headers["Content-Type"], "application/x-www-form-urlencoded;charset=UTF-8");
            return {
                ok: true,
                json: async () => ({ item_count: 1, in_list_label: "In list", message: "Added" }),
            };
        },
    });
    const event = { target: form, defaultPrevented: false, preventDefault: () => {} };

    await controller.handleSubmit(event);

    assert.equal(documentRef.count.textContent, "(1)");
    assert.equal(form.replacement.textContent, "✓ In list");
    assert.equal(documentRef.toast.textContent, "Added");
});

test("a live request failure submits the original Django form", async () => {
    const documentRef = buildDocument();
    const form = buildForm("add");
    let fallbackForm = null;
    const controller = new ShoppingListLiveController({
        documentRef,
        fetchRef: async () => { throw new Error("Network unavailable"); },
        submitFallback: (submittedForm) => { fallbackForm = submittedForm; },
    });
    const event = { target: form, defaultPrevented: false, preventDefault: () => {} };

    await controller.handleSubmit(event);

    assert.equal(fallbackForm, form);
    assert.equal(documentRef.toast.textContent, "Error");
});

test("a shopping list fragment replaces only the live region", () => {
    const liveRegion = { innerHTML: "old" };
    const documentRef = buildDocument({ liveRegion });
    const controller = new ShoppingListLiveController({ documentRef, fetchRef: async () => {} });

    controller.applyPayload({ item_count: 0, shopping_list_html: "<section>Empty</section>" }, buildForm("remove"), liveRegion);

    assert.equal(liveRegion.innerHTML, "<section>Empty</section>");
    assert.equal(documentRef.count.textContent, "(0)");
});
