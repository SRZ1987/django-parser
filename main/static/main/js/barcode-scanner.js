const SUPPORTED_FORMATS = new Set(["ean_8", "ean_13", "upc_a", "upc_e"]);
const SUPPORTED_LENGTHS = new Set([8, 12, 13]);

const STATUS = {
    scanning: "Наведите камеру на штрихкод",
    recognized: "Штрихкод распознан",
    notFound: "Не удалось распознать",
    cameraDenied: "Нет доступа к камере",
};

let zxingLoadPromise = null;

export function loadZxingBrowser(scriptUrl, documentRef = globalThis.document) {
    if (globalThis.ZXingBrowser) {
        return Promise.resolve(globalThis.ZXingBrowser);
    }

    if (zxingLoadPromise) {
        return zxingLoadPromise;
    }

    zxingLoadPromise = new Promise((resolve, reject) => {
        if (!documentRef || !scriptUrl) {
            reject(new Error("ZXing browser bundle is not configured."));
            return;
        }

        const existingScript = documentRef.querySelector("script[data-zxing-browser]");
        const script = existingScript || documentRef.createElement("script");

        const handleLoad = () => {
            if (globalThis.ZXingBrowser) {
                resolve(globalThis.ZXingBrowser);
                return;
            }
            reject(new Error("ZXing browser bundle did not initialize."));
        };

        const handleError = () => reject(new Error("ZXing browser bundle could not be loaded."));
        script.addEventListener("load", handleLoad, { once: true });
        script.addEventListener("error", handleError, { once: true });

        if (!existingScript) {
            script.src = scriptUrl;
            script.async = true;
            script.dataset.zxingBrowser = "true";
            documentRef.head.appendChild(script);
        }
    }).catch((error) => {
        zxingLoadPromise = null;
        throw error;
    });

    return zxingLoadPromise;
}

function hasOwn(object, key) {
    return Object.prototype.hasOwnProperty.call(object, key);
}

function normalizeFormat(format) {
    return String(format || "")
        .trim()
        .toLowerCase()
        .replaceAll("-", "_");
}

function stopStream(stream) {
    if (!stream || typeof stream.getTracks !== "function") {
        return;
    }
    stream.getTracks().forEach((track) => track.stop());
}

export class BarcodeScannerController {
    constructor(elements, dependencies = {}) {
        this.modal = elements.modal;
        this.video = elements.video;
        this.status = elements.status;
        this.fileInput = elements.fileInput;
        this.photoButton = elements.photoButton;
        this.retryButton = elements.retryButton;
        this.closeButtons = elements.closeButtons || [];
        this.triggers = elements.triggers || [];

        this.document = dependencies.documentRef || globalThis.document;
        this.mediaDevices = hasOwn(dependencies, "mediaDevices")
            ? dependencies.mediaDevices
            : globalThis.navigator?.mediaDevices;
        this.BarcodeDetectorClass = hasOwn(dependencies, "BarcodeDetectorClass")
            ? dependencies.BarcodeDetectorClass
            : globalThis.BarcodeDetector;
        this.createImageBitmap = hasOwn(dependencies, "createImageBitmap")
            ? dependencies.createImageBitmap
            : globalThis.createImageBitmap?.bind(globalThis);
        this.urlApi = dependencies.urlApi || globalThis.URL;
        this.EventClass = dependencies.EventClass || globalThis.Event;
        this.loadFallback = dependencies.loadFallback || (() => loadZxingBrowser(this.modal.dataset.zxingUrl, this.document));
        this.setTimer = dependencies.setTimeout || globalThis.setTimeout.bind(globalThis);
        this.clearTimer = dependencies.clearTimeout || globalThis.clearTimeout.bind(globalThis);
        this.scanDelay = dependencies.scanDelay ?? 180;
        this.completionDelay = dependencies.completionDelay ?? 220;

        this.activeTrigger = null;
        this.form = null;
        this.searchInput = null;
        this.stream = null;
        this.nativeDetector = null;
        this.nativeScanTimer = null;
        this.nativeDetectionInFlight = false;
        this.nativeErrors = 0;
        this.zxingReader = null;
        this.zxingControls = null;
        this.decoderMode = null;
        this.sessionId = 0;
        this.isOpen = false;
        this.isCompleting = false;
        this.isImageProcessing = false;
        this.pendingCompletion = null;

        this.handleKeydown = this.handleKeydown.bind(this);
    }

    bind() {
        this.triggers.forEach((trigger) => {
            trigger.addEventListener("click", () => void this.open(trigger));
        });
        this.closeButtons.forEach((button) => {
            button.addEventListener("click", () => void this.close());
        });
        this.retryButton.addEventListener("click", () => void this.retry());
        this.photoButton.addEventListener("click", () => this.fileInput.click());
        this.fileInput.addEventListener("change", () => {
            const [file] = this.fileInput.files || [];
            if (file) {
                void this.scanImage(file);
            }
        });
    }

    async open(trigger) {
        if (this.isOpen || this.isImageProcessing) {
            return;
        }

        const form = trigger.closest?.("[data-barcode-search-form]") || trigger.form;
        const searchInput = form?.querySelector?.("[data-barcode-search-input]");
        if (!form || !searchInput) {
            return;
        }

        this.activeTrigger = trigger;
        this.form = form;
        this.searchInput = searchInput;
        this.isOpen = true;
        this.isCompleting = false;
        const sessionId = ++this.sessionId;

        this.modal.hidden = false;
        this.modal.dataset.state = "scanning";
        this.activeTrigger.setAttribute?.("aria-expanded", "true");
        this.document?.body?.classList?.add("is-barcode-scanner-open");
        this.document?.addEventListener?.("keydown", this.handleKeydown);
        this.setStatus(STATUS.scanning, "scanning");
        this.retryButton.focus?.();

        await this.startCamera(sessionId);
    }

    async retry() {
        if (!this.isOpen || this.isCompleting || this.isImageProcessing) {
            return;
        }

        const sessionId = ++this.sessionId;
        await this.stopRecognition();
        if (!this.isCurrentSession(sessionId)) {
            return;
        }
        this.setStatus(STATUS.scanning, "scanning");
        await this.startCamera(sessionId);
    }

    async startCamera(sessionId) {
        if (!this.mediaDevices || typeof this.mediaDevices.getUserMedia !== "function") {
            this.setStatus(STATUS.cameraDenied, "error");
            return;
        }

        try {
            const stream = await this.mediaDevices.getUserMedia({
                audio: false,
                video: {
                    facingMode: { ideal: "environment" },
                    width: { ideal: 1280 },
                    height: { ideal: 720 },
                },
            });

            if (!this.isCurrentSession(sessionId)) {
                stopStream(stream);
                return;
            }

            this.stream = stream;
            this.video.srcObject = stream;
            await this.video.play?.();

            if (!this.isCurrentSession(sessionId)) {
                await this.stopRecognition();
                return;
            }

            const detector = await this.createNativeBarcodeDetector();
            if (detector) {
                this.decoderMode = "native";
                this.nativeDetector = detector;
                this.scheduleNativeScan(sessionId);
                return;
            }

            await this.startZxingVideoScan(sessionId);
        } catch (error) {
            if (!this.isCurrentSession(sessionId)) {
                return;
            }
            await this.stopRecognition();
            this.setStatus(STATUS.cameraDenied, "error");
        }
    }

    async createNativeBarcodeDetector() {
        if (typeof this.BarcodeDetectorClass !== "function") {
            return null;
        }

        let formats = Array.from(SUPPORTED_FORMATS);
        if (typeof this.BarcodeDetectorClass.getSupportedFormats === "function") {
            try {
                const supported = await this.BarcodeDetectorClass.getSupportedFormats();
                formats = formats.filter((format) => supported.includes(format));
            } catch (error) {
                return null;
            }
        }

        if (!formats.length) {
            return null;
        }

        try {
            return new this.BarcodeDetectorClass({ formats });
        } catch (error) {
            return null;
        }
    }

    scheduleNativeScan(sessionId) {
        if (!this.isCurrentSession(sessionId) || this.decoderMode !== "native") {
            return;
        }
        this.nativeScanTimer = this.setTimer(() => void this.scanNativeFrame(sessionId), this.scanDelay);
    }

    async scanNativeFrame(sessionId) {
        if (!this.isCurrentSession(sessionId) || this.decoderMode !== "native" || this.nativeDetectionInFlight) {
            return;
        }

        this.nativeDetectionInFlight = true;
        try {
            const barcodes = await this.nativeDetector.detect(this.video);
            const detected = barcodes.find((barcode) => this.isSupportedBarcode(barcode.rawValue, barcode.format));
            this.nativeErrors = 0;
            if (detected) {
                this.pendingCompletion = this.completeBarcode(detected.rawValue, sessionId);
                await this.pendingCompletion;
                return;
            }
        } catch (error) {
            this.nativeErrors += 1;
            if (this.nativeErrors >= 3 && this.isCurrentSession(sessionId)) {
                this.decoderMode = null;
                this.nativeDetector = null;
                this.nativeDetectionInFlight = false;
                await this.startZxingVideoScan(sessionId);
                return;
            }
        } finally {
            this.nativeDetectionInFlight = false;
        }

        this.scheduleNativeScan(sessionId);
    }

    async startZxingVideoScan(sessionId) {
        if (!this.isCurrentSession(sessionId) || this.decoderMode === "zxing") {
            return;
        }

        this.decoderMode = "zxing-loading";
        try {
            const library = await this.loadFallback();
            if (!this.isCurrentSession(sessionId)) {
                return;
            }

            const Reader = library.BrowserMultiFormatOneDReader || library.BrowserMultiFormatReader;
            if (typeof Reader !== "function") {
                throw new Error("ZXing reader is unavailable.");
            }

            this.decoderMode = "zxing";
            this.zxingReader = new Reader(undefined, {
                delayBetweenScanAttempts: this.scanDelay,
                delayBetweenScanSuccess: this.scanDelay,
            });

            const controls = await this.zxingReader.decodeFromVideoElement(
                this.video,
                (result) => {
                    if (!result || !this.isCurrentSession(sessionId) || this.isCompleting) {
                        return;
                    }

                    const value = result.getText?.() || result.text || "";
                    const formatCode = result.getBarcodeFormat?.();
                    const format = library.BarcodeFormat?.[formatCode] || formatCode;
                    if (this.isSupportedBarcode(value, format)) {
                        this.pendingCompletion = this.completeBarcode(value, sessionId);
                    }
                },
            );

            if (!this.isCurrentSession(sessionId)) {
                await Promise.resolve(controls.stop?.());
                return;
            }
            this.zxingControls = controls;
        } catch (error) {
            if (this.isCurrentSession(sessionId)) {
                await this.stopRecognition();
                this.setStatus(STATUS.notFound, "error");
            }
        }
    }

    async scanImage(file) {
        if (!this.isOpen || this.isCompleting || this.isImageProcessing) {
            return;
        }

        this.isImageProcessing = true;
        const sessionId = ++this.sessionId;
        await this.stopRecognition();
        this.setStatus(STATUS.scanning, "scanning");

        try {
            let detected = await this.scanImageNatively(file);
            if (!detected) {
                detected = await this.scanImageWithZxing(file);
            }

            if (!this.isCurrentSession(sessionId)) {
                return;
            }
            if (!detected || !this.isSupportedBarcode(detected.value, detected.format)) {
                this.setStatus(STATUS.notFound, "error");
                return;
            }

            this.pendingCompletion = this.completeBarcode(detected.value, sessionId);
            await this.pendingCompletion;
        } catch (error) {
            if (this.isCurrentSession(sessionId)) {
                this.setStatus(STATUS.notFound, "error");
            }
        } finally {
            this.fileInput.value = "";
            this.isImageProcessing = false;
        }
    }

    async scanImageNatively(file) {
        const detector = await this.createNativeBarcodeDetector();
        if (!detector || typeof this.createImageBitmap !== "function") {
            return null;
        }

        const bitmap = await this.createImageBitmap(file);
        try {
            const results = await detector.detect(bitmap);
            const barcode = results.find((item) => this.isSupportedBarcode(item.rawValue, item.format));
            return barcode ? { value: barcode.rawValue, format: barcode.format } : null;
        } finally {
            bitmap.close?.();
        }
    }

    async scanImageWithZxing(file) {
        const library = await this.loadFallback();
        const Reader = library.BrowserMultiFormatOneDReader || library.BrowserMultiFormatReader;
        const reader = new Reader();
        const objectUrl = this.urlApi.createObjectURL(file);

        try {
            const result = await reader.decodeFromImageUrl(objectUrl);
            const formatCode = result.getBarcodeFormat?.();
            return {
                value: result.getText?.() || result.text || "",
                format: library.BarcodeFormat?.[formatCode] || formatCode,
            };
        } finally {
            this.urlApi.revokeObjectURL(objectUrl);
            reader.reset?.();
        }
    }

    isSupportedBarcode(value, format) {
        const barcode = String(value || "").trim();
        const normalizedFormat = normalizeFormat(format);
        return (
            /^\d+$/.test(barcode)
            && SUPPORTED_LENGTHS.has(barcode.length)
            && SUPPORTED_FORMATS.has(normalizedFormat)
        );
    }

    async completeBarcode(value, sessionId = this.sessionId) {
        if (!this.isCurrentSession(sessionId) || this.isCompleting) {
            return;
        }

        this.isCompleting = true;
        this.searchInput.value = String(value).trim();
        if (typeof this.searchInput.dispatchEvent === "function" && typeof this.EventClass === "function") {
            this.searchInput.dispatchEvent(new this.EventClass("input", { bubbles: true }));
            this.searchInput.dispatchEvent(new this.EventClass("change", { bubbles: true }));
        }
        this.setStatus(STATUS.recognized, "success");
        await this.stopRecognition();

        if (this.completionDelay > 0) {
            await new Promise((resolve) => this.setTimer(resolve, this.completionDelay));
        }
        if (!this.isCurrentSession(sessionId)) {
            return;
        }

        const form = this.form;
        await this.close();
        if (typeof form.requestSubmit === "function") {
            form.requestSubmit();
        } else {
            form.submit?.();
        }
    }

    async close() {
        if (!this.isOpen) {
            return;
        }

        this.isOpen = false;
        ++this.sessionId;
        await this.stopRecognition();

        this.modal.hidden = true;
        this.document?.body?.classList?.remove("is-barcode-scanner-open");
        this.document?.removeEventListener?.("keydown", this.handleKeydown);
        this.activeTrigger?.setAttribute?.("aria-expanded", "false");
        this.activeTrigger?.focus?.();
        this.isCompleting = false;
    }

    async stopRecognition() {
        this.decoderMode = null;
        this.nativeDetector = null;
        this.nativeDetectionInFlight = false;
        if (this.nativeScanTimer !== null) {
            this.clearTimer(this.nativeScanTimer);
            this.nativeScanTimer = null;
        }

        const zxingControls = this.zxingControls;
        const zxingReader = this.zxingReader;
        this.zxingControls = null;
        this.zxingReader = null;

        try {
            if (zxingControls?.stop) {
                await Promise.resolve(zxingControls.stop());
            }
        } catch (error) {
            // Camera tracks still need to be released when decoder cleanup fails.
        } finally {
            try {
                zxingReader?.reset?.();
            } catch (error) {
                // Stream cleanup below is the required final safeguard.
            }

            stopStream(this.stream);
            this.stream = null;
            this.video.pause?.();
            this.video.srcObject = null;
        }
    }

    setStatus(message, state) {
        this.status.textContent = message;
        this.modal.dataset.state = state;
    }

    isCurrentSession(sessionId) {
        return this.isOpen && sessionId === this.sessionId;
    }

    handleKeydown(event) {
        if (event.key === "Escape") {
            event.preventDefault();
            void this.close();
            return;
        }

        if (event.key !== "Tab") {
            return;
        }

        const focusable = Array.from(
            this.modal.querySelectorAll("button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex='-1'])"),
        ).filter((element) => !element.hidden);
        if (!focusable.length) {
            return;
        }

        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && this.document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && this.document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    }
}

export function initializeBarcodeScanner(documentRef = globalThis.document, dependencies = {}) {
    const modal = documentRef?.querySelector?.("[data-barcode-scanner-modal]");
    const triggers = Array.from(documentRef?.querySelectorAll?.("[data-barcode-scanner-trigger]") || []);
    if (!modal || !triggers.length) {
        return null;
    }

    const controller = new BarcodeScannerController(
        {
            modal,
            video: modal.querySelector("[data-barcode-scanner-video]"),
            status: modal.querySelector("[data-barcode-scanner-status]"),
            fileInput: modal.querySelector("[data-barcode-scanner-file]"),
            photoButton: modal.querySelector("[data-barcode-scanner-photo]"),
            retryButton: modal.querySelector("[data-barcode-scanner-retry]"),
            closeButtons: Array.from(modal.querySelectorAll("[data-barcode-scanner-close]")),
            triggers,
        },
        { ...dependencies, documentRef },
    );
    controller.bind();
    return controller;
}

if (typeof document !== "undefined") {
    initializeBarcodeScanner(document);
}
