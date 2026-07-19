import assert from "node:assert/strict";
import test from "node:test";

import {
  createQuietActivityPulse,
  QUIET_ACTIVITY_DEFAULTS,
  streamWithQuietActivity,
} from "../codex_runtime/quiet_activity.mjs";

function timerHarness(overrides = {}) {
  let currentTime = 0;
  let timerCallback = null;
  let cleared = 0;
  let unrefCalls = 0;
  const pulses = [];
  const controller = createQuietActivityPulse({
    onPulse: (pulse) => pulses.push(pulse),
    now: () => currentTime,
    setTimer: (callback) => {
      timerCallback = callback;
      return {
        unref() {
          unrefCalls += 1;
        },
      };
    },
    clearTimer: () => {
      cleared += 1;
    },
    ...overrides,
  });

  return {
    controller,
    pulses,
    setTime(value) {
      currentTime = value;
    },
    check() {
      timerCallback?.();
    },
    get cleared() {
      return cleared;
    },
    get unrefCalls() {
      return unrefCalls;
    },
  };
}

test("quiet activity emits only after the bounded silence threshold", () => {
  const harness = timerHarness();
  assert.equal(harness.unrefCalls, 1);

  harness.setTime(QUIET_ACTIVITY_DEFAULTS.quietAfterMs - 1);
  harness.check();
  assert.deepEqual(harness.pulses, []);

  harness.setTime(QUIET_ACTIVITY_DEFAULTS.quietAfterMs);
  harness.check();
  assert.deepEqual(harness.pulses, [{
    pulseNumber: 1,
    quietForMs: QUIET_ACTIVITY_DEFAULTS.quietAfterMs,
    elapsedMs: QUIET_ACTIVITY_DEFAULTS.quietAfterMs,
  }]);
});

test("real SDK activity resets silence and pulses cannot spam", () => {
  const harness = timerHarness({
    quietAfterMs: 10,
    minPulseIntervalMs: 20,
  });

  harness.setTime(10);
  harness.check();
  harness.setTime(29);
  harness.check();
  assert.equal(harness.pulses.length, 1);

  harness.setTime(30);
  harness.controller.touch();
  harness.setTime(39);
  harness.check();
  assert.equal(harness.pulses.length, 1);

  harness.setTime(40);
  harness.check();
  assert.deepEqual(harness.pulses[1], {
    pulseNumber: 2,
    quietForMs: 10,
    elapsedMs: 40,
  });
});

test("pulse count and timer cleanup remain bounded and idempotent", () => {
  const harness = timerHarness({
    quietAfterMs: 1,
    minPulseIntervalMs: 1,
    maxPulses: 2,
  });

  harness.setTime(1);
  harness.check();
  harness.setTime(2);
  harness.check();
  harness.setTime(3);
  harness.check();

  assert.equal(harness.controller.pulseCount, 2);
  assert.equal(harness.controller.stopped, true);
  assert.equal(harness.cleared, 1);
  harness.controller.stop();
  assert.equal(harness.cleared, 1);
});

test("supplemental callback or timer failures never fail the turn", () => {
  let callbackAttempts = 0;
  const callbackFailure = timerHarness({
    quietAfterMs: 1,
    onPulse: () => {
      callbackAttempts += 1;
      throw new Error("supplemental failure");
    },
  });
  callbackFailure.setTime(1);
  assert.doesNotThrow(() => callbackFailure.check());
  assert.equal(callbackAttempts, 1);

  assert.doesNotThrow(() => createQuietActivityPulse({
    onPulse() {},
    setTimer() {
      throw new Error("timer unavailable");
    },
  }));
});

test("stream wrapper touches each event and always stops on failure", async () => {
  let touches = 0;
  let stops = 0;
  const controller = {
    touch() {
      touches += 1;
    },
    stop() {
      stops += 1;
    },
  };
  async function* failingEvents() {
    yield { type: "turn.started" };
    yield { type: "item.started" };
    throw new Error("stream failed");
  }

  const received = [];
  await assert.rejects(async () => {
    for await (const sdkEvent of streamWithQuietActivity(
      failingEvents(),
      controller,
    )) {
      received.push(sdkEvent.type);
    }
  }, /stream failed/);

  assert.deepEqual(received, ["turn.started", "item.started"]);
  assert.equal(touches, 2);
  assert.equal(stops, 1);
});

test("abort stops quiet activity immediately", async () => {
  const abortController = new AbortController();
  let stopped = 0;
  const controller = {
    touch() {},
    stop() {
      stopped += 1;
    },
  };
  async function* events() {
    abortController.abort();
    yield { type: "turn.started" };
  }

  const received = [];
  for await (const sdkEvent of streamWithQuietActivity(
    events(),
    controller,
    abortController.signal,
  )) {
    received.push(sdkEvent.type);
  }

  assert.deepEqual(received, ["turn.started"]);
  assert.equal(stopped, 2);
});
