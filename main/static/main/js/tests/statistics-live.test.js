import assert from "node:assert/strict";
import test from "node:test";

import { StatisticsLiveController } from "../statistics-live.js";

test("statistics refresh replaces dashboard content without reloading", async () => {
    const root = { dataset: { statisticsUrl: "/statistics/data/" }, innerHTML: "old" };
    const controller = new StatisticsLiveController({
        root,
        documentRef: { hidden: false },
        fetchRef: async (url) => ({
            ok: url === "/statistics/data/",
            json: async () => ({ html: "<section>fresh</section>", updated_at: "2026-08-12T10:00:00" }),
        }),
    });

    assert.equal(await controller.refresh(), true);
    assert.equal(root.innerHTML, "<section>fresh</section>");
    assert.equal(root.dataset.updatedAt, "2026-08-12T10:00:00");
});

test("statistics refresh pauses while the page is hidden", async () => {
    let requests = 0;
    const controller = new StatisticsLiveController({
        root: { dataset: { statisticsUrl: "/statistics/data/" } },
        documentRef: { hidden: true },
        fetchRef: async () => { requests += 1; },
    });

    assert.equal(await controller.refresh(), false);
    assert.equal(requests, 0);
});
