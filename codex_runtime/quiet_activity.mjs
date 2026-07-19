export const QUIET_ACTIVITY_DEFAULTS = Object.freeze({
  quietAfterMs: 12_000,
  minPulseIntervalMs: 20_000,
  checkIntervalMs: 1_000,
  maxPulses: 24,
});

function positiveInteger(value, fallback) {
  const candidate = Number(value);
  return Number.isFinite(candidate) && candidate >= 1
    ? Math.floor(candidate)
    : fallback;
}

function clockValue(now, fallback) {
  try {
    const value = Number(now());
    return Number.isFinite(value) ? value : fallback;
  } catch {
    return fallback;
  }
}

export function createQuietActivityPulse({
  onPulse,
  now = Date.now,
  setTimer = setInterval,
  clearTimer = clearInterval,
  quietAfterMs = QUIET_ACTIVITY_DEFAULTS.quietAfterMs,
  minPulseIntervalMs = QUIET_ACTIVITY_DEFAULTS.minPulseIntervalMs,
  checkIntervalMs = QUIET_ACTIVITY_DEFAULTS.checkIntervalMs,
  maxPulses = QUIET_ACTIVITY_DEFAULTS.maxPulses,
} = {}) {
  if (typeof onPulse !== "function") {
    throw new TypeError("Quiet activity requires an onPulse callback.");
  }

  const quietThreshold = positiveInteger(
    quietAfterMs,
    QUIET_ACTIVITY_DEFAULTS.quietAfterMs,
  );
  const pulseInterval = positiveInteger(
    minPulseIntervalMs,
    QUIET_ACTIVITY_DEFAULTS.minPulseIntervalMs,
  );
  const timerInterval = positiveInteger(
    checkIntervalMs,
    QUIET_ACTIVITY_DEFAULTS.checkIntervalMs,
  );
  const pulseLimit = positiveInteger(
    maxPulses,
    QUIET_ACTIVITY_DEFAULTS.maxPulses,
  );
  const startedAt = clockValue(now, 0);
  let lastActivityAt = startedAt;
  let lastPulseAt = null;
  let pulses = 0;
  let stopped = false;
  let timer = null;

  const stop = () => {
    if (stopped) return;
    stopped = true;
    if (timer !== null) {
      clearTimer(timer);
      timer = null;
    }
  };

  const touch = () => {
    if (stopped) return;
    const current = clockValue(now, lastActivityAt);
    lastActivityAt = Math.max(lastActivityAt, current);
  };

  const check = () => {
    if (stopped || pulses >= pulseLimit) {
      stop();
      return;
    }
    const current = clockValue(now, lastActivityAt);
    const quietForMs = Math.max(0, current - lastActivityAt);
    if (quietForMs < quietThreshold) return;
    if (
      lastPulseAt !== null
      && Math.max(0, current - lastPulseAt) < pulseInterval
    ) {
      return;
    }

    lastPulseAt = current;
    pulses += 1;
    try {
      onPulse({
        pulseNumber: pulses,
        quietForMs,
        elapsedMs: Math.max(0, current - startedAt),
      });
    } catch {
      // Supplemental activity must never interrupt the actual Codex turn.
    }
    if (pulses >= pulseLimit) stop();
  };

  try {
    timer = setTimer(check, timerInterval);
    if (typeof timer?.unref === "function") timer.unref();
  } catch {
    stopped = true;
  }

  return {
    touch,
    stop,
    get pulseCount() {
      return pulses;
    },
    get stopped() {
      return stopped;
    },
  };
}

export async function* streamWithQuietActivity(events, controller, signal = null) {
  const stopOnAbort = () => controller.stop();
  if (signal?.aborted) {
    controller.stop();
  } else {
    signal?.addEventListener?.("abort", stopOnAbort, { once: true });
  }

  try {
    for await (const sdkEvent of events) {
      controller.touch();
      yield sdkEvent;
    }
  } finally {
    signal?.removeEventListener?.("abort", stopOnAbort);
    controller.stop();
  }
}
