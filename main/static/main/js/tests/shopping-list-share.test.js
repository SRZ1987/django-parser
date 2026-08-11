import assert from "node:assert/strict";
import test from "node:test";

import { bindPriceAlertAutosave, ShoppingListShareController } from "../shopping-list-share.js";

test("native share sends the public shopping list URL", async () => {
    let payload = null;
    const statusElement = { textContent: "" };
    const controller = new ShoppingListShareController({
        navigatorRef: { share: async (value) => { payload = value; } },
        statusElement,
    });

    const result = await controller.share("https://example.com/shared-list/token/");

    assert.equal(result, true);
    assert.equal(payload.url, "https://example.com/shared-list/token/");
    assert.equal(payload.title, "Tannenberg shopping plan");
    assert.equal(statusElement.textContent, "Plan shared");
});

test("share falls back to copying when Web Share API is unavailable", async () => {
    let copied = "";
    const statusElement = { textContent: "" };
    const controller = new ShoppingListShareController({
        navigatorRef: { clipboard: { writeText: async (value) => { copied = value; } } },
        statusElement,
    });

    const result = await controller.share("https://example.com/shared-list/token/");

    assert.equal(result, true);
    assert.equal(copied, "https://example.com/shared-list/token/");
    assert.equal(statusElement.textContent, "Link copied");
});

test("copy failure produces an accessible error state", async () => {
    const statusElement = { textContent: "" };
    const controller = new ShoppingListShareController({
        navigatorRef: { clipboard: { writeText: async () => { throw new Error("denied"); } } },
        statusElement,
    });

    const result = await controller.copy("https://example.com/shared-list/token/");

    assert.equal(result, false);
    assert.equal(statusElement.textContent, "Could not copy the link");
});

test("localized share messages are used", async () => {
    let payload = null;
    const statusElement = { textContent: "" };
    const controller = new ShoppingListShareController({
        navigatorRef: { share: async (value) => { payload = value; } },
        statusElement,
        messages: {
            title: "План покупок Tannenberg",
            text: "План покупок по магазинам",
            shared: "План отправлен",
        },
    });

    await controller.share("https://example.com/shared-list/token/");

    assert.equal(payload.title, "План покупок Tannenberg");
    assert.equal(payload.text, "План покупок по магазинам");
    assert.equal(statusElement.textContent, "План отправлен");
});

test("price alert switch submits its form immediately", () => {
    let changeHandler = null;
    let submissions = 0;
    const toggle = {
        addEventListener: (name, handler) => {
            if (name === "change") {
                changeHandler = handler;
            }
        },
    };
    const form = {
        querySelector: () => toggle,
        requestSubmit: () => { submissions += 1; },
    };

    assert.equal(bindPriceAlertAutosave(form), true);
    changeHandler();
    assert.equal(submissions, 1);
});
