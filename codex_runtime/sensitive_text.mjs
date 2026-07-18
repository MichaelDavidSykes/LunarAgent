const REDACTED = "[redacted]";

const PRIVATE_KEY_BLOCK =
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|$)/g;
const SENSITIVE_HEADER =
  /(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]*/gi;
const SENSITIVE_ASSIGNMENT =
  /(\b(?:authorization|proxy-authorization|cookie|set-cookie|password|passwd|secret|client[_-]?secret|api[_-]?key|access[_-]?token|refresh[_-]?token|session[_-]?token|delegated[_-]?token|bearer[_-]?token|private[_-]?key)\b\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)/gi;
const AUTHORIZATION_VALUE =
  /\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}/gi;
const CREDENTIAL_URL =
  /\b((?:https?|mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis):\/\/)[^/\s:@]+:[^@\s/]+@/gi;
const OPENAI_KEY = /\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{12,}\b/g;
const GITHUB_TOKEN =
  /\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/g;
const SLACK_TOKEN = /\bxox[baprs]-[A-Za-z0-9-]{16,}\b/g;
const GOOGLE_API_KEY = /\bAIza[0-9A-Za-z_-]{30,}\b/g;
const AWS_ACCESS_KEY = /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g;
const JSON_WEB_TOKEN =
  /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g;

export function redactSensitiveTextWithCount(value) {
  let text = String(value ?? "");
  let count = 0;

  const replace = (pattern, replacement) => {
    text = text.replace(pattern, (...args) => {
      count += 1;
      return typeof replacement === "function" ? replacement(...args) : replacement;
    });
  };

  replace(PRIVATE_KEY_BLOCK, "[redacted private key]");
  replace(SENSITIVE_HEADER, (_match, prefix) => `${prefix}${REDACTED}`);
  replace(AUTHORIZATION_VALUE, (_match, scheme) => `${scheme} ${REDACTED}`);
  replace(SENSITIVE_ASSIGNMENT, (_match, prefix) => `${prefix}${REDACTED}`);
  replace(CREDENTIAL_URL, (_match, scheme) => `${scheme}${REDACTED}@`);
  replace(OPENAI_KEY, REDACTED);
  replace(GITHUB_TOKEN, REDACTED);
  replace(SLACK_TOKEN, REDACTED);
  replace(GOOGLE_API_KEY, REDACTED);
  replace(AWS_ACCESS_KEY, REDACTED);
  replace(JSON_WEB_TOKEN, REDACTED);

  return { text, count };
}

export function redactSensitiveText(value) {
  return redactSensitiveTextWithCount(value).text;
}
