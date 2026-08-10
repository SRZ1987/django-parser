window.addEventListener("load", () => {
    if (new URLSearchParams(window.location.search).has("preview")) {
        return;
    }
    window.setTimeout(() => window.print(), 250);
});
