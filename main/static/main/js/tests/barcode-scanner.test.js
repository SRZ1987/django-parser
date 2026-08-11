import assert from "node:assert/strict";
import test from "node:test";

import { BarcodeScannerController } from "../barcode-scanner.js";

function createClassList() {
    const values = new Set();
    return {
        add: (value) => values.add(value),
        remove: (value) => values.delete(value),
        contains: (value) => values.has(value),
    };
}

function createFixture(overrides = {}) {
    const inputEvents = [];
    const input = {
        value: "",
        dispatchEvent: (event) => inputEvents.push(event.type),
    };
    const form = {
        submitCount: 0,
        querySelector: () => input,
        requestSubmit() {
            this.submitCount += 1;
        },
    };
    const triggerAttributes = new Map();
    const trigger = {
        focusCount: 0,
        closest: () => form,
        focus() {
            this.focusCount += 1;
        },
        setAttribute(name, value) {
            triggerAttributes.set(name, value);
        },
    };
    const modal = {
        hidden: true,
        dataset: {
            zxingUrl: "/static/main/vendor/zxing-browser-0.2.1.min.js",
            statusScanning: "Наведите камеру на штрихкод",
            statusRecognized: "Штрихкод распознан",
            statusNotFound: "Не удалось распознать",
            statusCameraDenied: "Нет доступа к камере",
        },
        querySelectorAll: () => [],
    };
    const video = {
        srcObject: null,
        pauseCount: 0,
        async play() {},
        pause() {
            this.pauseCount += 1;
        },
    };
    const status = { textContent: "" };
    const documentRef = {
        activeElement: null,
        body: { classList: createClassList() },
        addEventListener() {},
        removeEventListener() {},
    };
    const elements = {
        modal,
        video,
        status,
        fileInput: { value: "", files: [], click() {}, addEventListener() {} },
        photoButton: { addEventListener() {} },
        retryButton: { focus() {}, addEventListener() {} },
        closeButtons: [],
        triggers: [trigger],
    };
    const dependencies = {
        documentRef,
        completionDelay: 0,
        scanDelay: 1,
        EventClass: Event,
        ...overrides,
    };
    const controller = new BarcodeScannerController(elements, dependencies);

    return {
        controller,
        documentRef,
        form,
        input,
        inputEvents,
        modal,
        status,
        trigger,
        triggerAttributes,
        video,
    };
}

function prepareRecognizedBarcode(fixture) {
    fixture.controller.isOpen = true;
    fixture.controller.sessionId = 1;
    fixture.controller.activeTrigger = fixture.trigger;
    fixture.controller.form = fixture.form;
    fixture.controller.searchInput = fixture.input;
}

test("recognized barcode is inserted into the search input", async () => {
    const fixture = createFixture();
    prepareRecognizedBarcode(fixture);

    await fixture.controller.completeBarcode("4006381333931", 1);

    assert.equal(fixture.input.value, "4006381333931");
    assert.deepEqual(fixture.inputEvents, ["input", "change"]);
    assert.equal(fixture.status.textContent, "Штрихкод распознан");
});

test("recognized barcode submits the existing search form", async () => {
    const fixture = createFixture();
    prepareRecognizedBarcode(fixture);

    await fixture.controller.completeBarcode("96385074", 1);

    assert.equal(fixture.form.submitCount, 1);
    assert.equal(fixture.modal.hidden, true);
});

test("closing the modal stops every camera track and restores focus", async () => {
    const fixture = createFixture();
    const tracks = [
        { stopped: false, stop() { this.stopped = true; } },
        { stopped: false, stop() { this.stopped = true; } },
    ];
    prepareRecognizedBarcode(fixture);
    fixture.controller.stream = { getTracks: () => tracks };
    fixture.video.srcObject = fixture.controller.stream;
    fixture.controller.zxingControls = { stop() { throw new Error("decoder cleanup failed"); } };
    fixture.controller.zxingReader = { reset() { throw new Error("reader cleanup failed"); } };

    await fixture.controller.close();

    assert.equal(tracks.every((track) => track.stopped), true);
    assert.equal(fixture.video.srcObject, null);
    assert.equal(fixture.trigger.focusCount, 1);
    assert.equal(fixture.triggerAttributes.get("aria-expanded"), "false");
});

test("camera permission denial produces a clear state without submitting", async () => {
    const denied = new Error("Permission denied");
    denied.name = "NotAllowedError";
    const fixture = createFixture({
        mediaDevices: { getUserMedia: async () => { throw denied; } },
        BarcodeDetectorClass: undefined,
    });

    await fixture.controller.open(fixture.trigger);

    assert.equal(fixture.status.textContent, "Нет доступа к камере");
    assert.equal(fixture.modal.dataset.state, "error");
    assert.equal(fixture.form.submitCount, 0);
    assert.equal(fixture.controller.isOpen, true);
});

test("ZXing fallback scans when BarcodeDetector is unavailable", async () => {
    const track = { stopped: false, stop() { this.stopped = true; } };
    const controls = { stopCount: 0, stop() { this.stopCount += 1; } };
    let fallbackLoads = 0;
    let decoderCalls = 0;

    class FakeReader {
        async decodeFromVideoElement(video, callback) {
            decoderCalls += 1;
            callback({
                getText: () => "012345678905",
                getBarcodeFormat: () => 12,
            });
            return controls;
        }
    }

    const fixture = createFixture({
        mediaDevices: {
            getUserMedia: async () => ({ getTracks: () => [track] }),
        },
        BarcodeDetectorClass: undefined,
        loadFallback: async () => {
            fallbackLoads += 1;
            return {
                BarcodeFormat: { 12: "UPC_A" },
                BrowserMultiFormatOneDReader: FakeReader,
            };
        },
    });

    await fixture.controller.open(fixture.trigger);
    await fixture.controller.pendingCompletion;

    assert.equal(fallbackLoads, 1);
    assert.equal(decoderCalls, 1);
    assert.equal(fixture.input.value, "012345678905");
    assert.equal(fixture.form.submitCount, 1);
    assert.equal(track.stopped, true);
    assert.ok(controls.stopCount >= 1);
});
