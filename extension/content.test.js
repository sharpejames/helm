// Property-based tests for content script pure functions
// Uses fast-check for property-based testing

const fc = require("fast-check");
const { computeResizedDimensions, clampFps, buildFrameMessage } = require("./content-utils.js");

// ── Property 2: Frame resize preserves aspect ratio with max dimension ───────
// **Validates: Requirements 2.6**
describe("Property 2: Frame resize preserves aspect ratio with max dimension", () => {
  test("resized dimensions satisfy max(outW, outH) <= 1024 and aspect ratio preserved", () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 10000 }),
        fc.integer({ min: 1, max: 10000 }),
        (width, height) => {
          const { width: outW, height: outH } = computeResizedDimensions(width, height);

          // Max dimension must be <= 512
          expect(Math.max(outW, outH)).toBeLessThanOrEqual(512);

          // Dimensions must be positive
          expect(outW).toBeGreaterThan(0);
          expect(outH).toBeGreaterThan(0);

          // If no resize was needed, dimensions should be unchanged
          if (width <= 512 && height <= 512) {
            expect(outW).toBe(width);
            expect(outH).toBe(height);
          } else {
            // Verify the resize used the correct scale factor
            const scale = 512 / Math.max(width, height);
            expect(outW).toBe(Math.max(1, Math.round(width * scale)));
            expect(outH).toBe(Math.max(1, Math.round(height * scale)));
          }
        }
      ),
      { numRuns: 200 }
    );
  });

  test("no resize needed when both dimensions <= 512", () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 512 }),
        fc.integer({ min: 1, max: 512 }),
        (width, height) => {
          const { width: outW, height: outH } = computeResizedDimensions(width, height);
          expect(outW).toBe(width);
          expect(outH).toBe(height);
        }
      ),
      { numRuns: 100 }
    );
  });
});

// ── Property 3: Frame message JSON structure ─────────────────────────────────
// **Validates: Requirements 3.2**
describe("Property 3: Frame message JSON structure", () => {
  test("message has type 'frameData', non-empty frame, and positive timestamp", () => {
    fc.assert(
      fc.property(
        fc.base64String({ minLength: 4 }),
        fc.double({ min: 0.001, max: 2000000000, noNaN: true }),
        (base64Str, timestamp) => {
          const msg = buildFrameMessage(base64Str, timestamp);

          expect(msg.type).toBe("frameData");
          expect(typeof msg.frame).toBe("string");
          expect(msg.frame.length).toBeGreaterThan(0);
          expect(typeof msg.timestamp).toBe("number");
          expect(msg.timestamp).toBeGreaterThan(0);
        }
      ),
      { numRuns: 200 }
    );
  });
});

// ── Property 13: Numeric configuration range clamping ────────────────────────
// **Validates: Requirements 2.3, 8.5**
describe("Property 13: Numeric configuration range clamping", () => {
  test("clamped value is always within [0.5, 2.0]", () => {
    fc.assert(
      fc.property(
        fc.double({ min: -1000, max: 1000, noNaN: true }),
        (value) => {
          const clamped = clampFps(value);
          expect(clamped).toBeGreaterThanOrEqual(0.5);
          expect(clamped).toBeLessThanOrEqual(2.0);
        }
      ),
      { numRuns: 200 }
    );
  });

  test("values within range are unchanged", () => {
    fc.assert(
      fc.property(
        fc.double({ min: 0.5, max: 2.0, noNaN: true }),
        (value) => {
          expect(clampFps(value)).toBe(value);
        }
      ),
      { numRuns: 100 }
    );
  });

  test("values below 0.5 become 0.5", () => {
    fc.assert(
      fc.property(
        fc.double({ min: -1000, max: 0.4999, noNaN: true }),
        (value) => {
          expect(clampFps(value)).toBe(0.5);
        }
      ),
      { numRuns: 100 }
    );
  });

  test("values above 2.0 become 2.0", () => {
    fc.assert(
      fc.property(
        fc.double({ min: 2.0001, max: 1000, noNaN: true }),
        (value) => {
          expect(clampFps(value)).toBe(2.0);
        }
      ),
      { numRuns: 100 }
    );
  });
});
