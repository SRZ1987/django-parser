export class StatisticsLiveController {
    constructor({ root, fetchRef = globalThis.fetch, documentRef = globalThis.document, intervalMs = 20000 } = {}) {
        this.root = root;
        this.fetch = fetchRef;
        this.document = documentRef;
        this.intervalMs = intervalMs;
        this.timer = null;
        this.refreshing = false;
    }

    async refresh() {
        if (!this.root || !this.fetch || this.refreshing || this.document?.hidden) {
            return false;
        }
        this.refreshing = true;
        try {
            const response = await this.fetch(this.root.dataset.statisticsUrl, {
                credentials: "same-origin",
                headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
            });
            if (!response.ok) {
                return false;
            }
            const payload = await response.json();
            if (typeof payload.html === "string") {
                this.root.innerHTML = payload.html;
                this.root.dataset.updatedAt = payload.updated_at || "";
                return true;
            }
        } catch (_error) {
            return false;
        } finally {
            this.refreshing = false;
        }
        return false;
    }

    start() {
        if (!this.root || this.timer) {
            return false;
        }
        this.timer = setInterval(() => this.refresh(), this.intervalMs);
        this.document?.addEventListener?.("visibilitychange", () => {
            if (!this.document.hidden) {
                this.refresh();
            }
        });
        return true;
    }

    stop() {
        clearInterval(this.timer);
        this.timer = null;
    }
}

if (typeof document !== "undefined") {
    const root = document.querySelector("[data-statistics-live]");
    if (root) {
        new StatisticsLiveController({ root }).start();
    }
}
