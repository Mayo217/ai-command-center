# Security

The Command Center runs on your own computer and is built so that nothing outside it can drive it.

- The web server binds to `127.0.0.1` only.
- It rejects any request whose `Host` header is not `127.0.0.1`, `localhost` or `[::1]` on its port.
  This blocks DNS-rebinding attacks.
- Writes (POST/DELETE) must be `Content-Type: application/json`, and any `Origin` header must be the
  app's own. A website open in your browser therefore cannot change your ledger, settings, skills or
  CLAUDE.md files.
- No dependencies outside the Python standard library.
- Your Claude login token is read only for the optional usage refresh (off by default). It is sent
  only to Anthropic's API, and never stored, logged or returned.
- Team files from a shared folder are treated as data. Files over 5 MB are skipped, only http(s)
  links are kept, and with `"auto_add": false` unknown senders are held. Anyone who can write to the
  shared folder can add entries, so share that folder only with your team.

Found a problem? Please open a GitHub security advisory ("Report a vulnerability" on the Security tab)
rather than a public issue.
