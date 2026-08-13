document.addEventListener("DOMContentLoaded", () => {
    const carousel = document.querySelector("[data-price-carousel]");
    if (!carousel) {
        return;
    }

    const viewport = carousel.querySelector("[data-carousel-viewport]");
    const firstCard = carousel.querySelector(".price-comparison-card");
    const desktopQuery = window.matchMedia("(min-width: 1025px)");
    const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (!viewport || !firstCard) {
        return;
    }

    let timer = null;

    const scrollToNextCard = () => {
        const gap = Number.parseFloat(window.getComputedStyle(viewport.firstElementChild).columnGap) || 12;
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
    document.addEventListener("visibilitychange", start);
    desktopQuery.addEventListener("change", start);
    reducedMotionQuery.addEventListener("change", start);

    start();
});
