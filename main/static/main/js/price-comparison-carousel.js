document.addEventListener("DOMContentLoaded", () => {
    const carousel = document.querySelector("[data-price-carousel]");
    if (!carousel) {
        return;
    }

    const viewport = carousel.querySelector("[data-carousel-viewport]");
    const refreshUrl = carousel.dataset.refreshUrl;
    const desktopQuery = window.matchMedia("(min-width: 1025px)");
    const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (!viewport) {
        return;
    }

    const REFRESH_INTERVAL_MS = 30 * 60 * 1000;
    let timer = null;
    let refreshTimer = null;
    let refreshInFlight = false;

    const scrollToNextCard = () => {
        const track = viewport.firstElementChild;
        const firstCard = track?.querySelector(".price-comparison-card");
        if (!track || !firstCard) {
            return;
        }

        const gap = Number.parseFloat(window.getComputedStyle(track).columnGap) || 12;
        const step = firstCard.getBoundingClientRect().width + gap;
        const end = viewport.scrollWidth - viewport.clientWidth;

        if (viewport.scrollLeft + step >= end - 2) {
            viewport.scrollTo({ left: 0, behavior: "smooth" });
            return;
        }

        viewport.scrollBy({ left: step, behavior: "smooth" });
    };

    const stop = () => {
        if (timer !== null) {
            window.clearInterval(timer);
            timer = null;
        }
    };

    const start = () => {
        stop();
        if (!desktopQuery.matches || reducedMotionQuery.matches || document.hidden) {
            return;
        }
        timer = window.setInterval(scrollToNextCard, 6500);
    };

    const refreshCards = async () => {
        if (!refreshUrl || refreshInFlight || !desktopQuery.matches) {
            return;
        }

        refreshInFlight = true;
        try {
            const response = await fetch(refreshUrl, {
                credentials: "same-origin",
                cache: "no-store",
                headers: { "X-Requested-With": "XMLHttpRequest" },
            });
            if (!response.ok) {
                throw new Error(`Carousel refresh failed with HTTP ${response.status}`);
            }

            const documentFragment = new DOMParser().parseFromString(
                await response.text(),
                "text/html",
            );
            const nextTrack = documentFragment.querySelector(".price-carousel-track");
            if (!nextTrack || !nextTrack.querySelector(".price-comparison-card")) {
                return;
            }

            stop();
            viewport.replaceChildren(nextTrack);
            viewport.scrollTo({ left: 0, behavior: "auto" });
            start();
        } catch (error) {
            console.warn("Price comparison carousel could not be refreshed.", error);
        } finally {
            refreshInFlight = false;
        }
    };

    const scheduleRefresh = () => {
        if (refreshTimer !== null) {
            window.clearTimeout(refreshTimer);
        }
        const delay = REFRESH_INTERVAL_MS - (Date.now() % REFRESH_INTERVAL_MS) + 1500;
        refreshTimer = window.setTimeout(async () => {
            await refreshCards();
            scheduleRefresh();
        }, delay);
    };

    carousel.addEventListener("mouseenter", stop);
    carousel.addEventListener("mouseleave", start);
    carousel.addEventListener("focusin", stop);
    carousel.addEventListener("focusout", (event) => {
        if (!carousel.contains(event.relatedTarget)) {
            start();
        }
    });
    viewport.addEventListener("touchstart", stop, { passive: true });
    viewport.addEventListener("touchend", start, { passive: true });
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) {
            refreshCards();
        }
        start();
    });
    desktopQuery.addEventListener("change", () => {
        if (desktopQuery.matches) {
            refreshCards();
        }
        start();
    });
    reducedMotionQuery.addEventListener("change", start);

    start();
    scheduleRefresh();
});
