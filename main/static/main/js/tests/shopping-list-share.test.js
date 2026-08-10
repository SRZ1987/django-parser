import assert from "node:assert/strict";
import test from "node:test";

import { ShoppingListShareController } from "../shopping-list-share.js";

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
    assert.equal(payload.title, "План покупок Tannenberg");
    assert.equal(statusElement.textContent, "План отправлен");
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
    assert.equal(statusElement.textContent, "Ссылка скопирована");
});

test("copy failure produces an accessible error state", async () => {
    const statusElement = { textContent: "" };
    const controller = new ShoppingListShareController({
        navigatorRef: { clipboard: { writeText: async () => { throw new Error("denied"); } } },
        statusElement,
    });

    const result = await controller.copy("https://example.com/shared-list/token/");

    assert.equal(result, false);
    assert.equal(statusElement.textContent, "Не удалось скопировать ссылку");
});
