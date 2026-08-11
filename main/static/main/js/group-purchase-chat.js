const chat = document.querySelector("[data-group-chat]");

if (chat) {
    const messagesContainer = chat.querySelector("[data-chat-messages]");
    const endpoint = chat.dataset.messagesUrl;
    const existingMessages = messagesContainer.querySelectorAll("[data-message-id]");
    let lastMessageId = existingMessages.length
        ? Number(existingMessages[existingMessages.length - 1].dataset.messageId)
        : 0;
    let requestInProgress = false;

    const appendMessage = (message) => {
        if (messagesContainer.querySelector(`[data-message-id="${message.id}"]`)) {
            return;
        }
        messagesContainer.querySelector("[data-chat-empty]")?.remove();

        const article = document.createElement("article");
        article.className = `group-chat-message${message.is_own ? " is-own" : ""}`;
        article.dataset.messageId = String(message.id);

        const meta = document.createElement("div");
        const sender = document.createElement("strong");
        sender.textContent = message.sender;
        const time = document.createElement("time");
        time.textContent = message.created_at;
        meta.append(sender, time);

        const body = document.createElement("p");
        body.textContent = message.body;
        article.append(meta, body);
        messagesContainer.append(article);
        lastMessageId = Math.max(lastMessageId, Number(message.id));
        messagesContainer.scrollTop = messagesContainer.scrollHeight;
    };

    const pollMessages = async () => {
        if (requestInProgress || document.hidden) {
            return;
        }
        requestInProgress = true;
        try {
            const response = await fetch(`${endpoint}?after=${lastMessageId}`, {
                headers: { Accept: "application/json" },
                credentials: "same-origin",
            });
            if (!response.ok) {
                return;
            }
            const payload = await response.json();
            payload.messages.forEach(appendMessage);
        } catch (_error) {
            // A temporary network error must not interrupt the chat form.
        } finally {
            requestInProgress = false;
        }
    };

    messagesContainer.scrollTop = messagesContainer.scrollHeight;
    window.setInterval(pollMessages, 5000);
    document.addEventListener("visibilitychange", pollMessages);
}
