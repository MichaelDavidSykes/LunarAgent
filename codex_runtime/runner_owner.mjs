import fs from "node:fs";

export function processStartTicks(pid) {
  try {
    const stat = fs.readFileSync(`/proc/${Number(pid)}/stat`, "utf8");
    const fields = stat.slice(stat.lastIndexOf(")") + 1).trim().split(/\s+/);
    return String(fields[19] || "");
  } catch {
    return "";
  }
}

export function runnerProcessMatches(runnerPid, expectedStartTicks = "") {
  try {
    process.kill(runnerPid, 0);
  } catch {
    return false;
  }
  const currentStartTicks = processStartTicks(runnerPid);
  return (
    !expectedStartTicks ||
    !currentStartTicks ||
    currentStartTicks === expectedStartTicks
  );
}

export function monitorRunnerLifetime({
  runnerPid,
  runnerStartTicks = "",
  intervalMs = 100,
  onOwnerExit,
}) {
  if (!Number.isSafeInteger(runnerPid) || runnerPid <= 1) {
    throw new Error("A valid runner process id is required");
  }
  if (typeof onOwnerExit !== "function") {
    throw new Error("A runner exit callback is required");
  }
  const timer = setInterval(() => {
    if (!runnerProcessMatches(runnerPid, runnerStartTicks)) {
      clearInterval(timer);
      onOwnerExit();
    }
  }, Math.max(20, Number(intervalMs) || 100));
  timer.unref();
  return () => clearInterval(timer);
}
