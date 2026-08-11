document.addEventListener("DOMContentLoaded", () => {
    const form = document.querySelector("[data-search-form]");
    const input = document.querySelector("[data-suggestions-input]");
    const panel = document.querySelector("[data-suggestions-panel]");

    if (!form || !input || !panel) {
        return;
    }

    const suggestionsUrl = input.dataset.suggestionsUrl;
    let debounceTimer = null;
    let abortController = null;
    let activeIndex = -1;
    let suggestions = [];

    const closePanel = () => {
        panel.hidden = true;
        panel.replaceChildren();
        input.setAttribute("aria-expanded", "false");
        activeIndex = -1;
        suggestions = [];
    };

    const currentPrice = (item) => item.sale_price || item.price || "";

    const submitSearch = (value) => {
        const query = value.trim();
        if (!query) {
            return;
        }

        const url = new URL(form.action, window.location.origin);
        url.searchParams.set("q", query);
        window.location.href = url.toString();
    };

    const openSuggestion = (item) => {
        if (item.detail_url) {
            window.location.href = item.detail_url;
            return;
        }

        submitSearch(item.name || input.value);
    };

    const setActiveItem = (nextIndex) => {
        const items = Array.from(panel.querySelectorAll(".suggestion-item"));
        if (!items.length) {
            activeIndex = -1;
            return;
        }

        activeIndex = (nextIndex + items.length) % items.length;
        items.forEach((item, index) => {
            item.classList.toggle("is-active", index === activeIndex);
            item.setAttribute("aria-selected", String(index === activeIndex));
        });
        items[activeIndex].scrollIntoView({ block: "nearest" });
    };

    const createSuggestion = (item, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "suggestion-item";
        button.setAttribute("role", "option");
        button.setAttribute("aria-selected", "false");

        const imageBox = document.createElement("span");
        imageBox.className = "suggestion-image";

        if (item.image_url) {
            const image = document.createElement("img");
            image.src = item.image_url;
            image.alt = item.name || "";
            image.loading = "lazy";
            imageBox.appendChild(image);
        } else {
            imageBox.textContent = panel.dataset.noImage || "No image";
        }

        const content = document.createElement("span");
        content.className = "suggestion-content";

        const title = document.createElement("span");
        title.className = "suggestion-title";
        title.textContent = item.name || "";

        const meta = document.createElement("span");
        meta.className = "suggestion-meta";
        meta.textContent = [item.shop, item.category].filter(Boolean).join(" · ");

        content.append(title, meta);

        const price = document.createElement("span");
        price.className = "suggestion-price";

        const mainPrice = document.createElement("strong");
        mainPrice.textContent = currentPrice(item)
            ? `${currentPrice(item)} ${item.currency || "EUR"}`
            : (panel.dataset.priceUnavailable || "Price not specified");
        price.appendChild(mainPrice);

        if (item.sale_price && item.price) {
            const oldPrice = document.createElement("span");
            oldPrice.className = "suggestion-old-price";
            oldPrice.textContent = `${item.price} ${item.currency || "EUR"}`;
            price.appendChild(oldPrice);
        }

        button.append(imageBox, content, price);
        button.addEventListener("mouseenter", () => setActiveItem(index));
        button.addEventListener("click", () => openSuggestion(item));

        return button;
    };

    const renderSuggestions = (items) => {
        panel.replaceChildren();
        suggestions = items;

        if (!items.length) {
            closePanel();
            return;
        }

        const fragment = document.createDocumentFragment();
        items.forEach((item, index) => {
            fragment.appendChild(createSuggestion(item, index));
        });
        panel.appendChild(fragment);
        panel.hidden = false;
        input.setAttribute("aria-expanded", "true");
        activeIndex = -1;
    };

    const fetchSuggestions = async () => {
        const query = input.value.trim();

        if (abortController) {
            abortController.abort();
        }

        if (query.length < 2) {
            closePanel();
            return;
        }

        abortController = new AbortController();
        const url = new URL(suggestionsUrl, window.location.origin);
        url.searchParams.set("q", query);

        try {
            const response = await fetch(url, { signal: abortController.signal });
            if (!response.ok) {
                closePanel();
                return;
            }

            const data = await response.json();
            renderSuggestions(Array.isArray(data.results) ? data.results : []);
        } catch (error) {
            if (error.name !== "AbortError") {
                closePanel();
            }
        }
    };

    input.addEventListener("input", () => {
        window.clearTimeout(debounceTimer);
        debounceTimer = window.setTimeout(fetchSuggestions, 300);
    });

    input.addEventListener("keydown", (event) => {
        if (panel.hidden) {
            return;
        }

        if (event.key === "ArrowDown") {
            event.preventDefault();
            setActiveItem(activeIndex + 1);
        }

        if (event.key === "ArrowUp") {
            event.preventDefault();
            setActiveItem(activeIndex - 1);
        }

        if (event.key === "Enter" && activeIndex >= 0 && suggestions[activeIndex]) {
            event.preventDefault();
            openSuggestion(suggestions[activeIndex]);
        }

        if (event.key === "Escape") {
            closePanel();
        }
    });

    document.addEventListener("click", (event) => {
        if (!form.contains(event.target)) {
            closePanel();
        }
    });
});
