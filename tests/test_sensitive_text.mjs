import assert from "node:assert/strict";
import test from "node:test";

import {
  redactSensitiveText,
  redactSensitiveTextWithCount,
} from "../codex_runtime/sensitive_text.mjs";

test("redacts assignment, authorization, provider-token, and credential URL forms", () => {
  const sample = [
    "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz",
    "Authorization: Bearer delegated-read-only-token",
    "github_pat_abcdefghijklmnopqrstuvwxyz123456",
    "postgres://user:password@db.example.test/reports",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEifQ.signaturevalue",
  ].join("\n");

  const { text, count } = redactSensitiveTextWithCount(sample);

  assert.ok(count >= 5);
  assert.doesNotMatch(text, /sk-proj-/);
  assert.doesNotMatch(text, /delegated-read-only-token/);
  assert.doesNotMatch(text, /github_pat_/);
  assert.doesNotMatch(text, /user:password/);
  assert.doesNotMatch(text, /eyJhbGci/);
  assert.match(text, /Authorization: \[redacted\]/);
});

test("redacts complete private-key blocks without damaging ordinary evidence", () => {
  const value = [
    "Observed actor: Example Group",
    "-----BEGIN PRIVATE KEY-----",
    "sensitive-material",
    "-----END PRIVATE KEY-----",
    "Public source: https://www.cisa.gov/news-events",
  ].join("\n");

  const text = redactSensitiveText(value);

  assert.match(text, /Observed actor: Example Group/);
  assert.match(text, /\[redacted private key\]/);
  assert.doesNotMatch(text, /sensitive-material/);
  assert.match(text, /https:\/\/www\.cisa\.gov\/news-events/);
});
