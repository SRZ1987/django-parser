document.addEventListener("DOMContentLoaded", () => {
    const carousel = document.querySelector("[data-price-carousel]");
    if (!carousel) {
        return;
    }

    const viewport = carousel.querySelector("[data-carousel-viewport]");
    const previous = carousel.querySelector("[data-carousel-previous]");
    const next = carousel.querySelector("[data-carousel-next]");
    const firstCard = carousel.querySelector(".price-comparison-card");
    if (!viewport || !previous || !next || !firstCard) {
        return;
    }

    const scrollByCard = (direction) => {
        const gap = Number.parseFloat(window.getComputedStyle(viewport.firstElementChild).columnGap) || 12;
        viewport.scrollBy({
            left: direction * (firstCard.getBoundingClientRect().width + gap),
            behavior: "smooth",
        });
    };

    previous.addEventListener("click", () => scrollByCard(-1));
    next.addEventListener("click", () => scrollByCard(1));
});
